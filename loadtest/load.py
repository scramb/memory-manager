# SPDX-License-Identifier: AGPL-3.0-only
"""Bulk-load a synthetic vault (`loadtest.generate`, #107) into Postgres and
prepare k6's side inputs (#108).

Usage::

    python -m loadtest.load --vault ./loadtest-vault \\
        --admin-url postgresql://mm:mm@localhost:55432/mm \\
        --context-out ./loadtest-vault/k6-context.json --base-url http://127.0.0.1:18080/mcp

Two parts, deliberately split so a test can exercise the pure one without a
database:

- `build_rows` walks `<vault>/vault/**.md`, sorted, and builds one `_Row` per
  note exactly as a `write(if_version="new")` through `storage.postgres.
  PostgresBackend` would: `id` from the frontmatter, `namespace` from
  `vault.paths.parse_note_path`, `version` the sha256 `vault.note.version`
  computes over the file's own bytes (the generator already writes the
  canonical byte form `vault.note.serialize` produces, so this never needs
  re-serializing), `current_revision`/`revision` fixed at 1, `author`/
  `client` the marker `"loadtest"`, `message` naming the path.
  `created_at`/`updated_at`/`xid` are left to the tables' own `default
  now()`/`default pg_current_xact_id()` (migration `0004_vault.sql`) - this
  never sets them, only the columns named in `_VAULT_NOTES_COLUMNS`/
  `_VAULT_REVISIONS_COLUMNS` are part of either `COPY`.
- `load` is the async part: (re)creates a fixed-name database (`--db-name`,
  default `mm_loadtest` - never a random name, unlike `tests/conftest.py`'s
  `test_database_url`, so a repeated `make loadtest-smoke` run always starts
  from a clean slate instead of accumulating databases on `mm-pg`),
  migrates it, bulk-inserts every row via `load_vault`
  (`asyncpg.Pool.copy_records_to_table`), creates one static token per
  sampled synthetic principal (`namespaces.json`'s `user-*` aliases, via
  `create_principal_tokens`) with `namespaces=["*"]` (ADR scope: per-namespace
  tokens are #109's job, not this one), and writes a JSON side file for k6:
  the server's base URL, a sample of known note paths (`memory_read` must
  never be driven by a ULID), and the token list with each alias's own
  namespace memberships (unused while every token still carries `["*"]`
  itself, but already in the shape #109's per-namespace tokens need).

Nothing here starts a server or runs k6 - `scripts/loadtest-smoke.sh` does
both, after this has run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

from memory_manager.auth.tokens import ALL_NAMESPACES, create_token
from memory_manager.db.migrate import migrate
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE
from memory_manager.vault.note import parse, version
from memory_manager.vault.paths import PathRejected, parse_note_path

__all__ = ["build_rows", "create_principal_tokens", "load", "load_vault", "main"]

_DEFAULT_DB_NAME = "mm_loadtest"
_DEFAULT_TOKEN_COUNT = 50
_DEFAULT_READ_PATH_COUNT = 500
_DEFAULT_BASE_URL = "http://127.0.0.1:8080/mcp"
_DEFAULT_SEED = 1

# A fixed marker, never a per-row value: every bulk-loaded revision was
# written by this loader, not by a real client - the same way `loadtest.
# generate`'s bodies are made-up words, not real user content.
_LOADTEST_MARKER = "loadtest"

_VAULT_NOTES_COLUMNS = ("id", "namespace", "path", "content", "version", "current_revision")
_VAULT_REVISIONS_COLUMNS = (
    "note_id",
    "revision",
    "path",
    "content",
    "version",
    "author",
    "client",
    "message",
)


@dataclass(frozen=True)
class _Row:
    """One note, ready to insert into `vault_notes`/`vault_revisions`."""

    id: str
    namespace: str
    path: str
    content: bytes
    version: str


def _iter_note_files(vault_dir: Path) -> list[Path]:
    """Every `*.md` file under `vault_dir`, sorted for a deterministic load order."""
    return sorted(vault_dir.rglob("*.md"))


def build_rows(vault_dir: Path) -> list[_Row]:
    """Build one `_Row` per note under `vault_dir` (a generated vault's `vault/` directory).

    Raises `ValueError` if a file's path relative to `vault_dir` is not a
    valid vault-relative note path - `loadtest.generate` never produces one,
    so this only ever fires against a hand-edited or unrelated directory.
    """
    rows: list[_Row] = []
    for file_path in _iter_note_files(vault_dir):
        relative = file_path.relative_to(vault_dir).as_posix()
        try:
            note_path = parse_note_path(relative)
        except PathRejected as exc:
            raise ValueError(f"{file_path} is not a valid vault-relative note path") from exc
        data = file_path.read_bytes()
        note = parse(data)
        rows.append(
            _Row(
                id=note.id,
                namespace=note_path.namespace,
                path=relative,
                content=data,
                version=version(data),
            )
        )
    return rows


def _notes_records(rows: Sequence[_Row]) -> list[tuple[Any, ...]]:
    return [(row.id, row.namespace, row.path, row.content, row.version, 1) for row in rows]


def _revisions_records(rows: Sequence[_Row]) -> list[tuple[Any, ...]]:
    return [
        (
            row.id,
            1,
            row.path,
            row.content,
            row.version,
            _LOADTEST_MARKER,
            _LOADTEST_MARKER,
            f"write {row.path}",
        )
        for row in rows
    ]


async def load_vault(database_url: str, rows: Sequence[_Row]) -> None:
    """Migrate `database_url` and bulk-insert `rows` into `vault_notes`/`vault_revisions`.

    One transaction for both tables: a failure on either `COPY` leaves
    neither behind, never a `vault_notes` row with no matching revision.
    """
    conn = await asyncpg.connect(database_url)
    try:
        await migrate(conn)
        async with conn.transaction():
            await conn.copy_records_to_table(
                "vault_notes", records=_notes_records(rows), columns=list(_VAULT_NOTES_COLUMNS)
            )
            await conn.copy_records_to_table(
                "vault_revisions",
                records=_revisions_records(rows),
                columns=list(_VAULT_REVISIONS_COLUMNS),
            )
    finally:
        await conn.close()


async def _recreate_database(admin_url: str, db_name: str) -> str:
    """Drop `db_name` if it exists and create it fresh; return its own connection URL."""
    admin_conn = await asyncpg.connect(admin_url)
    try:
        await admin_conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity "
            "where datname = $1 and pid <> pg_backend_pid()",
            db_name,
        )
        await admin_conn.execute(f'drop database if exists "{db_name}"')
        await admin_conn.execute(f'create database "{db_name}"')
    finally:
        await admin_conn.close()
    base, _, _ = admin_url.rpartition("/")
    return f"{base}/{db_name}"


def _personal_aliases(namespaces: Mapping[str, Any]) -> list[str]:
    """Every `user-*` alias in a generated `namespaces.json` document, sorted."""
    return sorted(
        alias
        for alias, info in namespaces["namespaces"].items()
        if alias.startswith("user-") and info["kind"] == "personal"
    )


def _membership_namespaces(alias: str, namespaces: Mapping[str, Any]) -> list[str]:
    """Every namespace alias `alias` is a member of, sorted (its own personal
    namespace, any sampled group, and `org`)."""
    return sorted(
        ns_alias for ns_alias, info in namespaces["namespaces"].items() if alias in info["members"]
    )


async def create_principal_tokens(
    pool: asyncpg.Pool, aliases: Sequence[str], namespaces: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """One static, read+write, every-namespace token per `aliases` entry.

    Each entry's `namespaces` records the alias's actual membership from
    `namespaces` (`namespaces.json`) for k6's own bookkeeping - the token
    itself still carries `["*"]` (`tokens.ALL_NAMESPACES`): per-namespace
    tokens are #109's scope, not this one's.
    """
    tokens: list[dict[str, Any]] = []
    for alias in aliases:
        plaintext, _info = await create_token(
            pool,
            f"loadtest-{alias}",
            scopes=[READ_SCOPE, WRITE_SCOPE],
            namespaces=[ALL_NAMESPACES],
        )
        tokens.append(
            {
                "alias": alias,
                "token": plaintext,
                "namespaces": _membership_namespaces(alias, namespaces),
            }
        )
    return tokens


def _sample(items: Sequence[str], count: int, rng: random.Random) -> list[str]:
    if count >= len(items):
        return list(items)
    return rng.sample(list(items), count)


async def load(
    *,
    vault_out: Path,
    admin_url: str,
    db_name: str,
    token_count: int,
    read_path_count: int,
    base_url: str,
    context_out: Path,
    seed: int,
) -> None:
    """Load `vault_out` (a `loadtest.generate` output directory) into `db_name`
    and write `context_out`, the JSON side file `loadtest/k6/lib.js` reads.
    """
    vault_dir = vault_out / "vault"
    rows = build_rows(vault_dir)
    if not rows:
        raise ValueError(f"no notes found under {vault_dir}")

    namespaces = json.loads((vault_out / "namespaces.json").read_text(encoding="utf-8"))

    database_url = await _recreate_database(admin_url, db_name)
    await load_vault(database_url, rows)

    rng = random.Random(seed)  # noqa: S311 - deterministic sampling, not a secret
    sampled_aliases = _sample(_personal_aliases(namespaces), token_count, rng)
    read_paths = _sample([row.path for row in rows], read_path_count, rng)

    pool = await asyncpg.create_pool(database_url)
    try:
        tokens = await create_principal_tokens(pool, sampled_aliases, namespaces)
    finally:
        await pool.close()

    context = {"base_url": base_url, "read_paths": read_paths, "tokens": tokens}
    context_out.write_text(json.dumps(context, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"loaded {len(rows)} notes into {db_name!r}; wrote {len(tokens)} tokens to {context_out}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadtest.load",
        description="bulk-load a synthetic vault into Postgres and prepare k6's side inputs (#108)",
    )
    parser.add_argument(
        "--vault", type=Path, required=True, help="a loadtest.generate output directory"
    )
    parser.add_argument(
        "--admin-url",
        default=os.environ.get("MM_TEST_DATABASE_URL"),
        help="admin Postgres URL used to (re)create --db-name (defaults to $MM_TEST_DATABASE_URL)",
    )
    parser.add_argument("--db-name", default=_DEFAULT_DB_NAME, help="database to drop and recreate")
    parser.add_argument(
        "--tokens",
        type=int,
        default=_DEFAULT_TOKEN_COUNT,
        help="number of synthetic principals to create a static token for",
    )
    parser.add_argument(
        "--read-paths",
        type=int,
        default=_DEFAULT_READ_PATH_COUNT,
        help="number of known note paths to sample for k6's memory_read scenario",
    )
    parser.add_argument(
        "--base-url",
        default=_DEFAULT_BASE_URL,
        help="the MCP endpoint k6 should call, recorded in --context-out",
    )
    parser.add_argument(
        "--context-out", type=Path, required=True, help="where to write the k6 side file (JSON)"
    )
    parser.add_argument(
        "--seed", type=int, default=_DEFAULT_SEED, help="seed for token/read-path sampling"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse `argv` (`sys.argv[1:]` if omitted) and run `load`."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.admin_url:
        print(
            "loadtest.load: --admin-url is required (or set MM_TEST_DATABASE_URL)",
            file=sys.stderr,
        )
        return 2
    asyncio.run(
        load(
            vault_out=args.vault,
            admin_url=args.admin_url,
            db_name=args.db_name,
            token_count=args.tokens,
            read_path_count=args.read_paths,
            base_url=args.base_url,
            context_out=args.context_out,
            seed=args.seed,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
