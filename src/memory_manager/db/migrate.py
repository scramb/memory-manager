# SPDX-License-Identifier: AGPL-3.0-only
"""Versioned SQL migrations for the Postgres index.

Migration files live in `migrations/NNNN_<slug>.sql`, are loaded via
`importlib.resources` (so they ship inside the installed package, not read
relative to the current working directory) and applied in filename order.

`migrate()` takes a Postgres advisory transaction lock for its whole run, so
two processes calling it concurrently never apply the same migration twice -
one blocks until the other's transaction (and therefore its lock) is
released. Each migration file then runs in its own nested transaction
(a savepoint under that lock-holding transaction): a failing migration rolls
back on its own without releasing the lock or losing track of migrations
already recorded in this run.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from importlib import resources

import asyncpg

__all__ = ["migrate"]

_MIGRATIONS_PACKAGE = "memory_manager.db.migrations"

# Fixed, deterministic advisory lock key for schema migrations. Any process
# migrating this database takes the same key, so concurrent `migrate()`
# calls serialize against each other regardless of which migration files
# they know about.
_LOCK_KEY = zlib.crc32(b"memory_manager:schema_migrations")

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


def _load_migrations() -> list[_Migration]:
    """Read every `*.sql` file under `migrations/`, sorted by filename."""
    migrations_dir = resources.files(_MIGRATIONS_PACKAGE)
    migrations = [
        _Migration(version=entry.name.removesuffix(".sql"), sql=entry.read_text(encoding="utf-8"))
        for entry in migrations_dir.iterdir()
        if entry.name.endswith(".sql")
    ]
    migrations.sort(key=lambda migration: migration.version)
    return migrations


async def migrate(conn: asyncpg.Connection) -> list[str]:
    """Apply every migration not yet recorded on `conn`, in order.

    Returns the versions applied in this call (empty if the schema was
    already current). Safe to call concurrently from multiple processes.
    """
    applied: list[str] = []
    async with conn.transaction():
        # The lock must be held before `schema_migrations` is created too:
        # plain `create table if not exists` has a race window between two
        # concurrent sessions (both see it missing, both try to create it).
        await conn.execute("select pg_advisory_xact_lock($1)", _LOCK_KEY)
        await conn.execute(_CREATE_SCHEMA_MIGRATIONS_SQL)

        rows = await conn.fetch("select version from schema_migrations")
        already_applied = {row["version"] for row in rows}

        for migration in _load_migrations():
            if migration.version in already_applied:
                continue
            async with conn.transaction():
                await conn.execute(migration.sql)
                await conn.execute(
                    "insert into schema_migrations (version) values ($1)", migration.version
                )
            applied.append(migration.version)

    return applied
