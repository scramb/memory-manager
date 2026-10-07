# SPDX-License-Identifier: AGPL-3.0-only
"""Fixtures for MCP tool tests: a `Services` built against a seeded vault.

`services` has no Postgres index wired in (`pool`/`indexer`/`provider` are
`None`) - `memory_index`/`memory_read` never touch Postgres, so the read
tools are exercised the same way whether or not `DATABASE_URL` is set. The
seeded notes' paths are deterministic (see `SEEDED_NOTES` below); tests read
their content straight off the `vault_root` fixture below rather than
importing `SEEDED_NOTES` from here - a bare `from conftest import ...` is
ambiguous once more than one directory under `tests/` has its own
`conftest.py` (`pyproject.toml`'s `mypy_path` comment), so this module's
data stays private to the `services` fixture.

`vault_root` is `services.vault_root`, narrowed non-`None`: every fixture
in this module builds a `"git"`-backed `Services` except
`services_with_postgres_backend`, which has none (`Services.vault_root` is
`Path | None` since WP-18/ADR-0007 §2 added the `postgres` backend) - a
plain pytest fixture, not a bare helper function, for the same "no
unambiguous import" reason `SEEDED_NOTES` stays private to this module:
pytest injects fixtures by name without an import at all.

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
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from git_fixtures import (
    bare_remote,
    human_commit,
    human_delete,
    human_rename,
    seed_notes,
    vault_config,
)
from mcp.server.auth.provider import AccessToken

from memory_manager.app import Services, open_services
from memory_manager.config import VaultConfig
from memory_manager.db import rls
from memory_manager.queue import WriteQueue
from memory_manager.storage.git import GitBackend
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.repo import Repo
from memory_manager.vault.ulid import new_ulid

__all__ = [
    "bare_remote",
    "human_commit",
    "human_delete",
    "human_rename",
    "postgres_backend_principal",
    "seed_notes",
    "services",
    "services_with_db",
    "services_with_postgres_backend",
    "vault_config",
    "vault_root",
]

#: The principal `services_with_postgres_backend`'s own namespace registry row
#: is seeded for (`"personal"`, matching every test in this package that writes
#: through `memory_write` without an explicit namespace). Private to this
#: module, like `SEEDED_NOTES` above - `postgres_backend_principal` below is
#: how a test actually gets it onto the current request.
_POSTGRES_BACKEND_OID = "oid-mcp-postgres-tests"


@pytest.fixture
def postgres_backend_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `db.rls.current_principal()` resolve to `services_with_postgres_backend`'s
    seeded personal namespace, for a test that calls a tool through it.

    A plain pytest fixture, not a bare helper function, for the same "no
    unambiguous import" reason `vault_root` above is one: pytest injects
    fixtures by name without an import at all. Monkeypatches
    `db.rls.get_access_token` directly (the same technique
    `tests/auth/test_limits_audit.py` uses for `mcp/server.py`'s own bound
    `current_access_token` import) - the in-memory `mcp.Client` these tests
    drive `build_server(services)` through never runs the real HTTP
    transport's `AuthContextMiddleware`, so there is no bearer token to
    carry claims for `db.rls`'s accessor to read otherwise.
    """
    token = AccessToken(
        token="mm_x",  # noqa: S106 - a fake test token, not a credential
        client_id="static:test",
        scopes=[],
        claims={"oid": _POSTGRES_BACKEND_OID, "roles": ["Memory.User"]},
    )
    monkeypatch.setattr(rls, "get_access_token", lambda: token)


async def _seed_personal_namespace(database_url: str, *, oid: str, alias: str) -> None:
    """Seed `namespaces`/`users` rows so `oid`'s own namespace `alias` is
    readable/writable under RLS (`mm_readable_ns`/`mm_writable_ns`,
    `migrations/0005_rls.sql`) - connects as the test database's owner
    (`mm`), which carries no RLS on these two membership tables at all.
    """
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            "insert into users (oid, tid, display_name) values ($1, 'tenant-test', $1)", oid
        )
        await conn.execute(
            "insert into namespaces (kind, external_key, alias) values ('user', $1, $2)",
            oid,
            alias,
        )
    finally:
        await conn.close()


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
            storage=GitBackend(queue, repo, vault_config.dir),
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


@pytest_asyncio.fixture
async def services_with_postgres_backend(
    admin_database_url: str, test_database_url: str
) -> AsyncIterator[Services]:
    """A `Services` against the `postgres` backend (ADR-0007 §2, WP-18) - no vault,
    no clone, nothing seeded. Tests write their own fixture notes through
    `memory_write`, the same way `services_with_db`'s own "with database" tests in
    `tests/mcp/test_search_tool.py` do, since there is no vault here to seed through
    `seed_notes` at all.

    Request path (ADR-0008 addendum, #116): this builds and tears down its own
    disposable app role (`open_services` requires `DATABASE_APP_ROLE`, and
    grants it the content-table privileges it needs at startup), and seeds one
    `namespaces` registry row for `_POSTGRES_BACKEND_OID`'s personal namespace,
    aliased `"personal"` - every test built on this fixture writes there.
    Tests still need `as_principal` (above) before calling a tool, since
    there is no bearer token here to carry a principal otherwise.
    """
    role = f"mm_test_app_{secrets.token_hex(8)}"
    admin_conn = await asyncpg.connect(admin_database_url)
    try:
        await admin_conn.execute(f'create role "{role}" nologin nosuperuser nobypassrls')
    finally:
        await admin_conn.close()

    environ = {
        "STORAGE_BACKEND": "postgres",
        "DATABASE_URL": test_database_url,
        "DATABASE_APP_ROLE": role,
    }
    try:
        async with open_services(environ) as services:
            await _seed_personal_namespace(
                test_database_url, oid=_POSTGRES_BACKEND_OID, alias="personal"
            )
            yield services
    finally:
        # See `tests/test_app.py`'s identical fixture for why `drop owned by`
        # against `test_database_url` has to run before the cluster-wide
        # `DROP ROLE` below.
        owned_conn: asyncpg.Connection | None
        try:
            owned_conn = await asyncpg.connect(test_database_url)
        except asyncpg.PostgresError:
            owned_conn = None
        if owned_conn is not None:
            try:
                await owned_conn.execute(f'drop owned by "{role}"')
            finally:
                await owned_conn.close()
        admin_conn = await asyncpg.connect(admin_database_url)
        try:
            await admin_conn.execute(f'drop role if exists "{role}"')
        finally:
            await admin_conn.close()


@pytest.fixture
def vault_root(services: Services) -> Path:
    """`services.vault_root`, narrowed non-`None` for this module's `"git"`-backed
    `services` fixture (`Services.vault_root` is `Path | None` since WP-18/ADR-0007
    §2 added the `postgres` backend, which has none) - the one place that mypy
    fallout is absorbed, instead of a bare assert scattered across every test that
    reads the vault's working copy directly.
    """
    assert services.vault_root is not None
    return services.vault_root
