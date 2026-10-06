# SPDX-License-Identifier: AGPL-3.0-only
"""Integration tests for `open_services`'s sync-hook wiring (#33).

The bug this guards against: before the fix, a human's commit picked up by a
write's own pre-write `repo.sync()` never reached the Postgres index unless the
poll loop's own timer happened to sync again later - the pre-write sync's
`ChangeSet` went nowhere. Now every sync the write queue's consumer runs
(standalone or pre-write) is handed to `WriteQueue.add_sync_hook`'s subscribers
(`app._index_sync_hook`), so the index is in step with a human's note before the
write that happened to pick it up even finishes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from git_fixtures import human_commit

from memory_manager.app import open_services
from memory_manager.queue import WriteRequest
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import new_ulid

_NOW = datetime(2025, 6, 1, tzinfo=UTC)
_HUMAN_PATH = "personal/fact/human.md"


def _note_bytes(**overrides: object) -> bytes:
    defaults: dict[str, object] = {
        "id": new_ulid(_NOW),
        "title": "A note",
        "description": "A description.",
        "type": "fact",
        "created": _NOW,
        "updated": _NOW,
        "body": "Body.\n",
    }
    defaults.update(overrides)
    return serialize(Note(**defaults))  # type: ignore[arg-type]


async def test_a_human_commit_ahead_of_a_write_reaches_the_index(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    environ = {
        "VAULT_REMOTE": str(bare_remote),
        "VAULT_DIR": str(tmp_path / "vault"),
        "DATABASE_URL": test_database_url,
    }
    async with open_services(environ) as services:
        human_commit(bare_remote, _HUMAN_PATH, _note_bytes(title="From a human"))

        assert services.pool is not None
        indexed_before = await services.pool.fetchval(
            "select count(*) from notes where path = $1", _HUMAN_PATH
        )
        assert indexed_before == 0

        # This write never touches `_HUMAN_PATH` - its own pre-write sync is what
        # fast-forwards the human commit above before the write commits.
        await services.queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/unrelated.md",
                client="human",
                if_version="new",
                content=_note_bytes(id=new_ulid(_NOW), title="Unrelated"),
            )
        )

        indexed_after = await services.pool.fetchval(
            "select count(*) from notes where path = $1", _HUMAN_PATH
        )
        assert indexed_after == 1


async def test_trigger_sync_indexes_a_human_commit_without_any_write(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    """`Services.trigger_sync` (the HTTP webhook's seam, `http.py`) is `queue.sync` -
    calling it alone, with no write in flight, must reach the index too.
    """
    environ = {
        "VAULT_REMOTE": str(bare_remote),
        "VAULT_DIR": str(tmp_path / "vault"),
        "DATABASE_URL": test_database_url,
    }
    async with open_services(environ) as services:
        human_commit(bare_remote, _HUMAN_PATH, _note_bytes(title="From a human"))

        assert services.trigger_sync is not None
        change_set = await services.trigger_sync()
        assert change_set.added == (_HUMAN_PATH,)

        assert services.pool is not None
        indexed = await services.pool.fetchval(
            "select count(*) from notes where path = $1", _HUMAN_PATH
        )
        assert indexed == 1
