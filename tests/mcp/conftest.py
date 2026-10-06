# SPDX-License-Identifier: AGPL-3.0-only
"""Fixtures for MCP tool tests: a `Services` built against a seeded vault.

`services` has no Postgres index wired in (`pool`/`indexer`/`provider` are
`None`) - `memory_index`/`memory_read` never touch Postgres, so the read
tools are exercised the same way whether or not `DATABASE_URL` is set. The
seeded notes' paths are deterministic (see `SEEDED_NOTES` below); tests read
their content straight off `services.vault_root` rather than importing
`SEEDED_NOTES` from here - a bare `from conftest import ...` is ambiguous
once more than one directory under `tests/` has its own `conftest.py`
(`pyproject.toml`'s `mypy_path` comment), so this module's data stays
private to the `services` fixture.

`human_commit`/`human_delete`/`human_rename` are re-exported for the same
reason `tests/vault/conftest.py` re-exports them: at runtime, `tests/`'s and
this module's `conftest.py` both end up importable under the bare name
`conftest`, and whichever one Python's import system resolves first for a
given run is the one a plain `from conftest import human_commit` (used by
`tests/test_queue*.py`, `tests/vault/test_*.py`) actually gets - so every
`conftest.py` that could win that race must carry the same superset.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest_asyncio
from git_fixtures import (
    bare_remote,
    human_commit,
    human_delete,
    human_rename,
    seed_notes,
    vault_config,
)

from memory_manager.app import Services, open_services
from memory_manager.config import VaultConfig
from memory_manager.queue import WriteQueue
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.repo import Repo
from memory_manager.vault.ulid import new_ulid

__all__ = [
    "bare_remote",
    "human_commit",
    "human_delete",
    "human_rename",
    "seed_notes",
    "services",
    "services_with_db",
    "vault_config",
]

_NOW = datetime(2025, 6, 1, tzinfo=UTC)


@dataclass(frozen=True)
class SeededNote:
    """One note this package's `services` fixture seeds the vault with."""

    path: str
    id: str
    title: str
    content: bytes


def _seeded_note(
    *,
    namespace: str,
    note_type: str,
    slug: str,
    title: str,
    description: str,
    tags: tuple[str, ...] = (),
    body: str = "Body.\n",
    archived: bool = False,
) -> SeededNote:
    note = Note(
        id=new_ulid(_NOW),
        title=title,
        description=description,
        type=note_type,
        created=_NOW,
        updated=_NOW,
        body=body,
        tags=tags,
    )
    prefix = "_archive/" if archived else ""
    path = f"{prefix}{namespace}/{note_type}/{slug}.md"
    return SeededNote(path=path, id=note.id, title=title, content=serialize(note))


# A broken, non-note file at a note-shaped path: `memory_index` reports it as
# a warning entry instead of dropping or failing on it; `memory_read` reports
# it as a per-item error.
BROKEN_NOTE_PATH = "personal/fact/broken.md"
_BROKEN_NOTE_CONTENT = b"this is not a note\n"

SEEDED_NOTES: tuple[SeededNote, ...] = (
    _seeded_note(
        namespace="personal",
        note_type="fact",
        slug="favorite-color",
        title="Favorite color",
        description="The user's favorite color.",
        tags=("color", "preference"),
        body="Blue.\n",
    ),
    _seeded_note(
        namespace="personal",
        note_type="project",
        slug="memory-manager",
        title="memory-manager",
        description="Building a self-hosted long-term memory server.",
        tags=("code",),
        body="A Git-backed vault with a derived Postgres index.\n",
    ),
    _seeded_note(
        namespace="work",
        note_type="reference",
        slug="deploy-notes",
        title="Deploy notes",
        description="How to deploy the service.",
        body="See the runbook.\n",
    ),
    _seeded_note(
        namespace="personal",
        note_type="fact",
        slug="retired-fact",
        title="Retired fact",
        description="A fact that was archived.",
        body="No longer true.\n",
        archived=True,
    ),
)


@pytest_asyncio.fixture
async def services(vault_config: VaultConfig, bare_remote: Path) -> AsyncIterator[Services]:
    """A `Services` against a vault seeded with `SEEDED_NOTES` plus one broken file."""
    notes = {note.path: note.content for note in SEEDED_NOTES}
    notes[BROKEN_NOTE_PATH] = _BROKEN_NOTE_CONTENT
    seed_notes(bare_remote, notes)

    repo = Repo(vault_config)
    await asyncio.to_thread(repo.ensure_clone)
    await asyncio.to_thread(repo.sync)

    queue = WriteQueue(repo)
    await queue.start()
    try:
        yield Services(
            repo=repo,
            queue=queue,
            vault_root=vault_config.dir,
            pool=None,
            indexer=None,
            provider=None,
        )
    finally:
        await queue.stop()


@pytest_asyncio.fixture
async def services_with_db(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> AsyncIterator[Services]:
    """A `Services` seeded like `services`, but with a real Postgres index behind it.

    Goes through `open_services` (not a hand-assembled `Services` like
    `services` above) so the startup reindex actually runs, exercising
    `memory_search`'s `hybrid`/`fulltext` modes the same way a real
    `DATABASE_URL`-configured process would. `test_database_url` comes from
    the root `tests/conftest.py`, available here without import (see this
    module's docstring on why that import would be ambiguous).
    """
    notes = {note.path: note.content for note in SEEDED_NOTES}
    notes[BROKEN_NOTE_PATH] = _BROKEN_NOTE_CONTENT
    seed_notes(bare_remote, notes)

    environ = {
        "VAULT_REMOTE": str(bare_remote),
        "VAULT_DIR": str(tmp_path / "db-vault"),
        "VAULT_BRANCH": "main",
        "DATABASE_URL": test_database_url,
    }
    async with open_services(environ) as services:
        yield services
