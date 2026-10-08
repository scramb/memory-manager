# SPDX-License-Identifier: AGPL-3.0-only
"""Versioned SQL migrations for the Postgres index.

Migration files live in `migrations/NNNN_<slug>.sql`, are loaded via
`importlib.resources` (so they ship inside the installed package, not read
relative to the current working directory) and applied in filename order.
`migrations/postgres/NNNN_<slug>.sql` holds Postgres-mode-only migrations
(ADR-0016, #220): loaded, applied and recorded in `schema_migrations`
exactly like every other migration, but only when `migrate()` is called
with `backend="postgres"` - for `backend="git"` (the default - every
caller that never needs them keeps calling bare `migrate(conn)`) they are
not even read. This is what keeps the Git backend's own `chunks`
(migration `0001_index_schema.sql`, `vector` column, no partitioning)
"exactly as it is today" (ADR-0016 Consequences) even though both backends
share the rest of this migration chain: a Postgres-mode-only migration
simply never runs against a Git-backend database, migrated or not.

`migrate()` takes a Postgres advisory transaction lock for its whole run, so
two processes calling it concurrently never apply the same migration twice -
one blocks until the other's transaction (and therefore its lock) is
released. Each migration file then runs in its own nested transaction
(a savepoint under that lock-holding transaction): a failing migration rolls
back on its own without releasing the lock or losing track of migrations
already recorded in this run.

The embedding dimension pin (#220, PLAN O23) rides in the same lock: once
every migration has been applied, and only for `backend="postgres"`,
`migrate()` records `embedding_dimensions` (default 1024 when `None`) into
`embedding_dimension` (`migrations/postgres/0012_vector_layout.sql`) if no
row exists yet, or refuses to continue with `EmbeddingDimensionPinError` if
one does and disagrees with what was just passed in. Folding this into the
same transaction/lock `migrate()` already holds means two replicas racing
to be the first to pin a dimension serialize on the same advisory lock
every concurrent `migrate()` call already does, rather than racing a
second, separate check.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable

import asyncpg

from memory_manager.config import EmbeddingDimensionPinError

__all__ = ["POSTGRES_VECTOR_LAYOUT_VERSION", "migrate"]

_MIGRATIONS_PACKAGE = "memory_manager.db.migrations"
_POSTGRES_ONLY_SUBDIR = "postgres"

# Fixed, deterministic advisory lock key for schema migrations. Any process
# migrating this database takes the same key, so concurrent `migrate()`
# calls serialize against each other regardless of which migration files
# they know about.
_LOCK_KEY = zlib.crc32(b"memory_manager:schema_migrations")

#: Callers may pass this backend to run only the common migration chain
#: (every `*.sql` file directly under `migrations/`) - the default, and the
#: only option every pre-existing caller/test still implicitly uses.
_BACKEND_GIT = "git"
#: Callers pass this to additionally run `migrations/postgres/` (ADR-0016,
#: #220) and pin `EMBEDDING_DIMENSIONS`.
_BACKEND_POSTGRES = "postgres"
_BACKENDS = (_BACKEND_GIT, _BACKEND_POSTGRES)

#: The version `index/indexer.py` checks for (`schema_migrations`) to tell
#: whether a given database has the ADR-0016 `chunks` layout - kept here,
#: next to the file it names, rather than hard-coded a second time there.
POSTGRES_VECTOR_LAYOUT_VERSION = "0012_vector_layout"

# Postgres' own default for a 1024-dim `text-embedding-3-large` request
# (PLAN O23, ADR-0016 Context): what a fresh Postgres-mode database pins to
# when no `EMBEDDING_DIMENSIONS` is configured at all for whichever process
# happens to run the first `migrate(..., backend="postgres")`.
_DEFAULT_PINNED_DIMENSIONS = 1024

_CREATE_SCHEMA_MIGRATIONS_SQL = """
create table if not exists schema_migrations (
    version text primary key,
    applied_at timestamptz not null default now()
);
"""


@dataclass(frozen=True)
class _Migration:
    version: str
    sql: str


def _migrations_from(directory: Traversable) -> list[_Migration]:
    return [
        _Migration(version=entry.name.removesuffix(".sql"), sql=entry.read_text(encoding="utf-8"))
        for entry in directory.iterdir()
        if entry.name.endswith(".sql")
    ]


def _load_migrations(backend: str) -> list[_Migration]:
    """Read every `*.sql` file under `migrations/`, sorted by filename.

    For `backend="postgres"`, additionally reads `migrations/postgres/` and
    merges the two by version before sorting - `"0012_..."` always sorts
    after `"0011_..."` regardless of which directory it came from.
    """
    migrations_dir = resources.files(_MIGRATIONS_PACKAGE)
    migrations = _migrations_from(migrations_dir)
    if backend == _BACKEND_POSTGRES:
        migrations += _migrations_from(migrations_dir / _POSTGRES_ONLY_SUBDIR)
    migrations.sort(key=lambda migration: migration.version)
    return migrations


async def migrate(
    conn: asyncpg.Connection,
    *,
    backend: str = _BACKEND_GIT,
    embedding_dimensions: int | None = None,
) -> list[str]:
    """Apply every migration not yet recorded on `conn`, in order.

    `backend` (`"git"`, the default, or `"postgres"`) selects which
    migrations run - see this module's own docstring. Every caller that
    opens a Postgres-mode database (`app.py`'s `"postgres"` branch,
    `cli.py`'s `reindex`/`worker`/`export`/`migrate git-to-postgres`) passes
    `backend="postgres"` explicitly; callers that only ever need the common
    chain (most tests, the Git backend's own optional derived index,
    `eval`) keep calling this with no `backend` at all.

    `embedding_dimensions` (ignored for `backend="git"`) is PLAN O23's
    dimension pin: `None` pins `1024` the first time a `"postgres"` call
    ever applies `migrations/postgres/0012_vector_layout.sql`'s
    `embedding_dimension` table; a later call with a different, non-`None`
    value raises `EmbeddingDimensionPinError` instead of silently
    overwriting what the first call pinned.

    Returns the versions applied in this call (empty if the schema was
    already current). Safe to call concurrently from multiple processes.
    """
    if backend not in _BACKENDS:
        raise ValueError(f"backend must be one of {_BACKENDS}, got {backend!r}")

    applied: list[str] = []
    async with conn.transaction():
        # The lock must be held before `schema_migrations` is created too:
        # plain `create table if not exists` has a race window between two
        # concurrent sessions (both see it missing, both try to create it).
        await conn.execute("select pg_advisory_xact_lock($1)", _LOCK_KEY)
        await conn.execute(_CREATE_SCHEMA_MIGRATIONS_SQL)

        rows = await conn.fetch("select version from schema_migrations")
        already_applied = {row["version"] for row in rows}

        for migration in _load_migrations(backend):
            if migration.version in already_applied:
                continue
            async with conn.transaction():
                await conn.execute(migration.sql)
                await conn.execute(
                    "insert into schema_migrations (version) values ($1)", migration.version
                )
            applied.append(migration.version)

        if backend == _BACKEND_POSTGRES:
            await _pin_embedding_dimensions(conn, embedding_dimensions)

    return applied


async def _pin_embedding_dimensions(conn: asyncpg.Connection, dimensions: int | None) -> None:
    """Record or check `embedding_dimension` (`migrations/postgres/0012_vector_layout.sql`).

    Runs inside `migrate()`'s own lock-holding transaction, after every
    migration (including `0012_vector_layout.sql` itself, on a fresh
    database) has applied. `dimensions=None` (`EMBEDDING_DIMENSIONS` unset)
    pins the default instead of leaving the pin table empty - a later,
    explicit value is then checked against that default like any other.
    """
    pinned = await conn.fetchval("select dimension from embedding_dimension")
    if pinned is None:
        await conn.execute(
            "insert into embedding_dimension (dimension) values ($1)",
            dimensions if dimensions is not None else _DEFAULT_PINNED_DIMENSIONS,
        )
        return
    if dimensions is not None and dimensions != pinned:
        raise EmbeddingDimensionPinError(
            f"EMBEDDING_DIMENSIONS={dimensions} does not match the dimension this "
            f"Postgres backend was first migrated with ({pinned}, PLAN O23: pinned at "
            "first migrate, immutable afterwards) - reindex into a new column/table "
            "to change it (ADR-0016; not done by this process)"
        )
