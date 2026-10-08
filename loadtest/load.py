# SPDX-License-Identifier: AGPL-3.0-only
"""Bulk-load a synthetic vault (`loadtest.generate`, #107) into Postgres under
RLS, with registered principals (#108, #124), and prepare k6's side inputs.

Usage::

    python -m loadtest.load --vault ./loadtest-vault \\
        --admin-url postgresql://mm:mm@localhost:55432/mm \\
        --context-out ./loadtest-vault/k6-context.json --base-url http://127.0.0.1:18080/mcp

Since WP-19/ADR-0008's addendum, a request transaction never touches content
as the owner: it switches to a non-superuser app role and the caller's own
`oid`/`roles` identity (`db.rls.request_identity`), and every content table
is under `FORCE ROW LEVEL SECURITY` (`migrations/0005_rls.sql`). This module
sets both halves up, not just the data:

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
- `ensure_app_role` creates the non-owner app role (`--app-role`, default
  `mm_loadtest_app`) idempotently and grants it to the admin connection's own
  `current_user` - the role `db.rls.check_app_role`/`grant_app_role` (run by
  `app.py` at `serve` startup) need to already exist and already be held by
  the owner; this module never grants the table/function privileges itself,
  that stays `serve`'s own startup gate.
- `populate_registry` fills `users`/`namespaces`/`user_groups` (migration
  `0005_rls.sql`'s membership tables) from the generated vault's own
  `namespaces.json`: every `user-*` alias becomes a synthetic, deterministic
  `oid` (`f"oid-{alias}"`, CLAUDE.md "no real personal data") and a
  `namespaces` row of kind `'user'` whose `alias` is already the generator's
  own alias - so `mm_ensure_personal_ns()` only ever confirms it
  (`migrations/0009_namespace_resolution.sql` ~56-100's "if v_alias is null"
  branch never fires for a row that already carries one), never invents the
  production `u-<id>` alias instead. Group aliases and `user_groups`
  membership follow the same way; `org` gets one fixed row.
- `load` is the async part: (re)creates a fixed-name database (`--db-name`,
  default `mm_loadtest` - never a random name, unlike `tests/conftest.py`'s
  `test_database_url`, so a repeated `make loadtest-smoke` run always starts
  from a clean slate instead of accumulating databases on `mm-pg`), ensures
  the app role, migrates the fresh database, bulk-inserts every row via
  `load_vault` (`asyncpg.Pool.copy_records_to_table`), populates the
  registry, creates one static token per sampled synthetic principal
  (`namespaces.json`'s `user-*` aliases, via `create_principal_tokens`) with
  an owner principal (`owner_oid`/`roles=["Memory.User"]`) and
  `namespaces=["*"]` (ADR scope: the RLS matrix, not the token's own
  namespaces claim, is what actually narrows a static token under this
  module - per-namespace *token* claims are #109's job, not this one), and
  writes a JSON side file for k6: the server's base URL and the token list,
  each carrying its own sample of readable note paths rewritten to `me/...`
  (its own personal notes) or kept at `org/...` (never a group path: a
  static token carries no `groups` claim, so `namespaces.Resolution.
  readable()` never adds one for it either).

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

__all__ = [
    "build_rows",
    "create_principal_tokens",
    "ensure_app_role",
    "load",
    "load_vault",
    "main",
    "populate_registry",
]

_DEFAULT_DB_NAME = "mm_loadtest"
_DEFAULT_APP_ROLE = "mm_loadtest_app"
_DEFAULT_TOKEN_COUNT = 50
_DEFAULT_PER_TOKEN_READ_PATHS = 20
_DEFAULT_BASE_URL = "http://127.0.0.1:8080/mcp"
_DEFAULT_SEED = 1

#: The one role (ADR-0008 addendum, #115) every synthetic principal's static
#: token carries - `create_principal_tokens`'s own `roles` argument to
#: `auth.tokens.create_token`. No test here needs `Memory.Curator`/`Admin`:
#: #124's own scope is "the smoke stays green", not the permission matrix's
#: write-side branches (that is `tests/mcp/test_permission_matrix.py`'s job).
_TOKEN_ROLES = ("Memory.User",)

#: Synthetic Entra tenant id for every `users` row this module inserts
#: (CLAUDE.md "no real personal data") - one fixed value, never read by any
#: RLS function, just `users.tid`'s `not null` constraint.
_SYNTHETIC_TID = "tid-loadtest"

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


async def ensure_app_role(admin_url: str, role: str) -> None:
    """Idempotently create `role` (`NOLOGIN NOSUPERUSER NOBYPASSRLS`) and grant
    it to the admin connection's own `current_user`.

    Roles are cluster-wide, not per-database (unlike `_recreate_database`'s
    `db_name`), so this runs against `admin_url` directly, before the loaded
    database even exists. Only ever creates the role and its membership -
    never the table/function privileges `db.rls.grant_app_role` grants,
    which is `serve`'s own startup gate (`app.py`, run once `DATABASE_APP_ROLE`
    is set): `db.rls.check_app_role`'s membership check
    (`pg_has_role(current_user, role, 'MEMBER')`) needs that membership to
    already be there before `serve` can switch to `role` for a request.

    Safe to call again: an existing role is left alone, and granting a
    membership a second time is a Postgres no-op (`GRANT ... TO` on an
    already-held membership, not an error) - the same idempotency
    `db.rls.grant_app_role` itself relies on for repeated `serve` starts.
    """
    conn = await asyncpg.connect(admin_url)
    try:
        exists = await conn.fetchval("select 1 from pg_roles where rolname = $1", role)
        if not exists:
            await conn.execute(f'create role "{role}" nologin nosuperuser nobypassrls')
        owner = await conn.fetchval("select current_user")
        await conn.execute(f'grant "{role}" to "{owner}"')
    finally:
        await conn.close()


def _synthetic_oid(alias: str) -> str:
    """A deterministic, synthetic `users.oid` for a generator `user-*` alias.

    Never a real Entra object id (CLAUDE.md "no real personal data") - just
    the alias itself under a fixed prefix, reproducible across runs and
    legible by eye while debugging a failed load or token.
    """
    return f"oid-{alias}"


def _synthetic_group_id(alias: str) -> str:
    """A deterministic, synthetic Entra group object id for a generator
    `group-*` alias - `_synthetic_oid`'s own idea, for `user_groups.group_id`."""
    return f"gid-{alias}"


def _org_alias(namespaces: Mapping[str, Any]) -> str | None:
    """The one `kind: "org"` alias in a generated `namespaces.json`, or `None`.

    `loadtest.generate` always writes exactly one, but this module does not
    assume that - a hand-built `namespaces.json` (this file's own tests) may
    omit it.
    """
    for alias, info in namespaces["namespaces"].items():
        if info["kind"] == "org":
            return str(alias)
    return None


async def populate_registry(pool: asyncpg.Pool, namespaces: Mapping[str, Any]) -> None:
    """Insert every `namespaces.json` alias into the registry tables
    `migrations/0005_rls.sql`'s `mm_readable_ns`/`mm_writable_ns` and
    `migrations/0009_namespace_resolution.sql`'s `mm_principal_namespaces`
    read: `users`, `namespaces`, `user_groups`. Without this, every one of
    those functions sees an empty registry and resolves every identity to
    zero namespaces - RLS hides the whole, already-loaded vault.

    Every personal namespace keeps the exact alias `loadtest.generate`
    already wrote the vault under (`namespaces.alias` is set directly, by
    the insert below, not left `null` for `mm_ensure_personal_ns()` to
    invent the production `u-<id>` form for). Runs once, against a freshly
    (re)created and still-empty database (`load`'s own `_recreate_database`
    always runs first) - no row here can conflict with another, so none of
    the inserts need an `on conflict` clause.
    """
    entries = namespaces["namespaces"]
    personal = sorted((a, i) for a, i in entries.items() if i["kind"] == "personal")
    groups = sorted((a, i) for a, i in entries.items() if i["kind"] == "group")
    orgs = sorted((a, i) for a, i in entries.items() if i["kind"] == "org")

    user_rows = [
        (_synthetic_oid(alias), _SYNTHETIC_TID, f"loadtest user {alias}") for alias, _ in personal
    ]
    namespace_rows = (
        [("user", _synthetic_oid(alias), alias) for alias, _ in personal]
        + [("group", _synthetic_group_id(alias), alias) for alias, _ in groups]
        + [("org", alias, alias) for alias, _ in orgs]
    )
    membership_rows = [
        (_synthetic_oid(member), _synthetic_group_id(alias))
        for alias, info in groups
        for member in info["members"]
    ]

    async with pool.acquire() as conn, conn.transaction():
        if user_rows:
            await conn.copy_records_to_table(
                "users", records=user_rows, columns=["oid", "tid", "display_name"]
            )
        if namespace_rows:
            await conn.copy_records_to_table(
                "namespaces", records=namespace_rows, columns=["kind", "external_key", "alias"]
            )
        if membership_rows:
            await conn.copy_records_to_table(
                "user_groups", records=membership_rows, columns=["oid", "group_id"]
            )


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


def _sample(items: Sequence[str], count: int, rng: random.Random) -> list[str]:
    if count >= len(items):
        return list(items)
    return rng.sample(list(items), count)


def _rewrite_to_me(path: str) -> str:
    """`path` (a generator-written vault path) with its namespace segment
    replaced by `me` - the pseudo-alias every client-facing path uses for
    its own personal namespace (`mcp.namespaces.ME_ALIAS`)."""
    _namespace, _, rest = path.partition("/")
    return f"me/{rest}"


def _per_token_read_paths(
    alias: str,
    rows: Sequence[_Row],
    org_alias: str | None,
    count: int,
    rng: random.Random,
) -> list[str]:
    """`count` read paths `alias`'s own static token may always read: its own
    personal notes (rewritten to `me/...`) plus the org's notes (kept at
    their real `org/...` path, never rewritten - `org` is not the caller's
    own personal namespace).

    Never a group path: a static token carries no `groups` claim
    (`auth.verifier`'s own module docstring), so `namespaces.Resolution.
    readable()` never adds one for it either - sampling one here would only
    ever produce a `memory_read` call this module's own canary
    (`loadtest/k6/read.js`) is built to catch as a silent `NotFound`.
    """
    own = [_rewrite_to_me(row.path) for row in rows if row.namespace == alias]
    org = [row.path for row in rows if org_alias is not None and row.namespace == org_alias]
    return sorted(_sample(own + org, count, rng))


async def create_principal_tokens(
    pool: asyncpg.Pool,
    aliases: Sequence[str],
    namespaces: Mapping[str, Any],
    rows: Sequence[_Row],
    *,
    rng: random.Random,
    read_path_count: int = _DEFAULT_PER_TOKEN_READ_PATHS,
) -> list[dict[str, Any]]:
    """One static, read+write, every-namespace token per `aliases` entry, each
    with an owner principal (`owner_oid`/`roles=["Memory.User"]`, ADR-0008
    addendum #115) and its own sample of readable paths.

    `namespaces=["*"]` (`tokens.ALL_NAMESPACES`) still means "no restriction
    from the token's own claim" - what actually narrows a static token under
    RLS is the owner principal's matrix (`namespaces.Resolution.readable()`),
    computed fresh per request from `populate_registry`'s rows, not a
    per-namespace token claim (#109's scope, not this one's).

    Each entry's `namespaces` records the alias's actual membership from
    `namespaces` (`namespaces.json`, including any group) for k6's own
    bookkeeping; `read_paths` is `_per_token_read_paths`'s own sample -
    `me/...`/`org/...` only, matching what the owner principal can actually
    read end to end through the MCP tools, not just through raw SQL.
    """
    org_alias = _org_alias(namespaces)
    tokens: list[dict[str, Any]] = []
    for alias in aliases:
        plaintext, _info = await create_token(
            pool,
            f"loadtest-{alias}",
            scopes=[READ_SCOPE, WRITE_SCOPE],
            namespaces=[ALL_NAMESPACES],
            owner_oid=_synthetic_oid(alias),
            roles=list(_TOKEN_ROLES),
        )
        tokens.append(
            {
                "alias": alias,
                "token": plaintext,
                "namespaces": _membership_namespaces(alias, namespaces),
                "read_paths": _per_token_read_paths(alias, rows, org_alias, read_path_count, rng),
            }
        )
    return tokens


async def load(
    *,
    vault_out: Path,
    admin_url: str,
    db_name: str,
    app_role: str,
    token_count: int,
    base_url: str,
    context_out: Path,
    seed: int,
) -> None:
    """Load `vault_out` (a `loadtest.generate` output directory) into `db_name`
    under RLS, with a registered principal per sampled token, and write
    `context_out`, the JSON side file `loadtest/k6/lib.js` reads.
    """
    vault_dir = vault_out / "vault"
    rows = build_rows(vault_dir)
    if not rows:
        raise ValueError(f"no notes found under {vault_dir}")

    namespaces = json.loads((vault_out / "namespaces.json").read_text(encoding="utf-8"))

    await ensure_app_role(admin_url, app_role)
    database_url = await _recreate_database(admin_url, db_name)
    await load_vault(database_url, rows)

    rng = random.Random(seed)  # noqa: S311 - deterministic sampling, not a secret
    sampled_aliases = _sample(_personal_aliases(namespaces), token_count, rng)

    pool = await asyncpg.create_pool(database_url)
    try:
        await populate_registry(pool, namespaces)
        tokens = await create_principal_tokens(pool, sampled_aliases, namespaces, rows, rng=rng)
    finally:
        await pool.close()

    context = {"base_url": base_url, "tokens": tokens}
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
        "--app-role",
        default=_DEFAULT_APP_ROLE,
        help="non-owner role request transactions switch to under RLS (ensure_app_role)",
    )
    parser.add_argument(
        "--tokens",
        type=int,
        default=_DEFAULT_TOKEN_COUNT,
        help="number of synthetic principals to create a static token for",
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
            app_role=args.app_role,
            token_count=args.tokens,
            base_url=args.base_url,
            context_out=args.context_out,
            seed=args.seed,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
