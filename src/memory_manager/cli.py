# SPDX-License-Identifier: AGPL-3.0-only
"""The `memory-manager` command-line entry point (#26).

`reindex` and `doctor` exist so far. Other subcommands (vault sync, search,
...) are added as their own tasks wire the server together (M2/M4).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg

from memory_manager.config import EmbeddingConfig, EmbeddingConfigError
from memory_manager.db.migrate import migrate
from memory_manager.doctor import DoctorReport, run_doctor
from memory_manager.index.embeddings import provider_from_config
from memory_manager.index.indexer import Indexer, IndexStats

__all__ = ["main"]


class _MissingEnvironment(RuntimeError):
    """A required environment variable is not set."""


def main(argv: list[str] | None = None) -> int:
    """Parse `argv` (`sys.argv[1:]` if omitted) and run the requested command."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _run_doctor_command(args.vault)

    if args.command != "reindex":
        parser.print_help()
        return 1

    try:
        database_url = _require_env("DATABASE_URL")
        vault_dir = Path(_require_env("VAULT_DIR"))
        embedding_config = EmbeddingConfig.from_env(dict(os.environ))
    except (_MissingEnvironment, EmbeddingConfigError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    return asyncio.run(_reindex(database_url, vault_dir, embedding_config, full=args.full))


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
    doctor_parser = subparsers.add_parser(
        "doctor", help="check every note in the vault against ADR-0005"
    )
    doctor_parser.add_argument(
        "--vault",
        default=os.environ.get("VAULT_DIR"),
        help="path to the vault root (defaults to $VAULT_DIR)",
    )
    return parser


def _run_doctor_command(vault: str | None) -> int:
    if not vault:
        print("--vault is required (or set VAULT_DIR)", file=sys.stderr)
        return 2
    report = run_doctor(Path(vault))
    _print_doctor_report(report)
    return 1 if report.errors else 0


def _print_doctor_report(report: DoctorReport) -> None:
    for error in report.errors:
        print(f"ERROR: {error}")
    for warning in report.warnings:
        print(f"WARNING: {warning}")
    print(f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)")


async def _reindex(
    database_url: str, vault_dir: Path, embedding_config: EmbeddingConfig, *, full: bool
) -> int:
    # A plain connection for the migration, not one from the pool below:
    # `migrate` takes an `asyncpg.Connection`, not a pool's connection proxy.
    migration_conn = await asyncpg.connect(database_url)
    try:
        await migrate(migration_conn)
    finally:
        await migration_conn.close()

    provider = provider_from_config(embedding_config)
    pool = await asyncpg.create_pool(database_url)
    try:
        stats = await Indexer(pool, vault_dir, provider).reindex(full=full)
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
