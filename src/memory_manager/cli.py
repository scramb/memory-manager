# SPDX-License-Identifier: AGPL-3.0-only
"""The `memory-manager` command-line entry point (#26).

Only `reindex` exists so far. Other subcommands (vault sync, search, ...)
are added as their own tasks wire the server together (M2/M4).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg

from memory_manager.db.migrate import migrate
from memory_manager.index.indexer import Indexer, IndexStats

__all__ = ["main"]


class _MissingEnvironment(RuntimeError):
    """A required environment variable is not set."""


def main(argv: list[str] | None = None) -> int:
    """Parse `argv` (`sys.argv[1:]` if omitted) and run the requested command."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command != "reindex":
        parser.print_help()
        return 1

    try:
        database_url = _require_env("DATABASE_URL")
        vault_dir = Path(_require_env("VAULT_DIR"))
    except _MissingEnvironment as exc:
        print(str(exc), file=sys.stderr)
        return 2

    return asyncio.run(_reindex(database_url, vault_dir, full=args.full))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memory-manager")
    subparsers = parser.add_subparsers(dest="command")
    reindex_parser = subparsers.add_parser(
        "reindex", help="bring the Postgres index in step with the vault"
    )
    reindex_parser.add_argument(
        "--full",
        action="store_true",
        help="also drop stale rows and recompute every link (full rebuild)",
    )
    return parser


async def _reindex(database_url: str, vault_dir: Path, *, full: bool) -> int:
    # A plain connection for the migration, not one from the pool below:
    # `migrate` takes an `asyncpg.Connection`, not a pool's connection proxy.
    migration_conn = await asyncpg.connect(database_url)
    try:
        await migrate(migration_conn)
    finally:
        await migration_conn.close()

    pool = await asyncpg.create_pool(database_url)
    try:
        stats = await Indexer(pool, vault_dir).reindex(full=full)
    finally:
        await pool.close()

    _print_stats(stats)
    return 1 if stats.failed else 0


def _print_stats(stats: IndexStats) -> None:
    print(
        f"indexed={stats.indexed} unchanged={stats.unchanged} "
        f"deleted={stats.deleted} failed={stats.failed}"
    )


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise _MissingEnvironment(f"{name} is required but not set")
    return value


if __name__ == "__main__":
    sys.exit(main())
