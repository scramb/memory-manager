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

import pytest
from git_fixtures import human_commit

from memory_manager.app import open_services
from memory_manager.config import StorageConfigError
from memory_manager.queue import WriteRequest
from memory_manager.storage.base import VersionConflict
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
        assert services.queue is not None
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


async def test_an_unknown_storage_backend_fails_before_the_vault_dir_is_created(
    bare_remote: Path, tmp_path: Path
) -> None:
    vault_dir = tmp_path / "vault"
    environ = {
        "VAULT_REMOTE": str(bare_remote),
        "VAULT_DIR": str(vault_dir),
        "STORAGE_BACKEND": "bogus",
    }

    with pytest.raises(StorageConfigError):
        async with open_services(environ):
            pass

    assert not vault_dir.exists()


async def test_postgres_backend_without_database_url_fails_before_the_vault_dir_is_created(
    bare_remote: Path, tmp_path: Path
) -> None:
    vault_dir = tmp_path / "vault"
    environ = {
        "VAULT_REMOTE": str(bare_remote),
        "VAULT_DIR": str(vault_dir),
        "STORAGE_BACKEND": "postgres",
    }

    with pytest.raises(StorageConfigError):
        async with open_services(environ):
            pass

    assert not vault_dir.exists()


# --- The `postgres` backend (ADR-0007 §2, WP-18) -----------------------------


async def test_postgres_backend_opens_with_no_vault_env_at_all(test_database_url: str) -> None:
    """No `VAULT_*` variable - proof this mode never clones anything (ADR-0007 §2).

    `services.indexer` is set despite there being no vault at all (ADR-0007
    §4, WP-18/#98): it indexes from `vault_notes`, not a working copy.
    """
    environ = {"STORAGE_BACKEND": "postgres", "DATABASE_URL": test_database_url}

    async with open_services(environ) as services:
        assert services.repo is None
        assert services.queue is None
        assert services.vault_root is None
        assert services.indexer is not None
        assert services.provider is None  # no EMBEDDING_* set
        assert services.trigger_sync is None
        assert services.pool is not None

        result = await services.storage.write(
            "personal/fact/new.md",
            _note_bytes(title="New"),
            if_version="new",
            client="ci",
        )
        assert result.version


async def test_postgres_backend_writes_get_exactly_one_audit_row_each_including_rejected(
    test_database_url: str,
) -> None:
    environ = {"STORAGE_BACKEND": "postgres", "DATABASE_URL": test_database_url}
    path = "personal/fact/new.md"

    async with open_services(environ) as services:
        assert services.pool is not None

        result = await services.storage.write(
            path, _note_bytes(title="New"), if_version="new", client="ci"
        )

        with pytest.raises(VersionConflict):
            await services.storage.write(
                path, _note_bytes(title="Again"), if_version="new", client="ci"
            )

        rows = await services.pool.fetch("select * from audit_log order by id")

    assert len(rows) == 2
    assert rows[0]["op"] == "write"
    assert rows[0]["outcome"] == "ok"
    assert rows[0]["commit_sha"] == result.commit
    assert rows[1]["op"] == "write"
    assert rows[1]["outcome"] == "conflict"
