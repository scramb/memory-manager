# SPDX-License-Identifier: AGPL-3.0-only
"""Row-level security helpers for namespace permissions (ADR-0008 + addendum, #100).

`migrations/0005_rls.sql` adds `mm_readable_ns()`/`mm_writable_ns()` and
turns on `ENABLE`/`FORCE ROW LEVEL SECURITY` on `vault_notes`,
`vault_revisions`, `notes`, `chunks` and `links`. This module is the two
pieces of Python the SQL alone cannot provide:

- `grant_app_role`: an idempotent, owner-run grant of exactly the
  privileges a request-serving role needs (table DML, the `chunks_id_seq`
  sequence, `EXECUTE` on both functions) - and nothing on `namespaces` or
  the membership tables, which the `SECURITY DEFINER` functions read with
  the owner's own privileges regardless of the caller.
- `request_identity`: the per-transaction role switch and identity
  settings a request connection must perform before touching any of the
  five tables above. Everything here is set with `set_config(..., true)`
  (`SET LOCAL` semantics, ADR-0008 addendum + docs/research/enterprise.md
  §3.2): a pooled connection's `RESET ALL` on release clears plain
  session-level `SET`, but **not** a session-level `SET ROLE` - which is
  exactly why `role` must only ever be switched with the transaction-local
  form, never a plain `SET ROLE`. Using `set_config` with `is_local=true`
  for every one of `role`, `app.oid`, `app.roles` and `app.break_glass`
  means all four revert together at `COMMIT`/`ROLLBACK`, regardless of
  what the pool does afterwards.

Wiring this into `PostgresBackend`, search or the indexer, and pinning the
request path to it so no request connection can skip the switch, is #101.
Nothing here is called from application code yet.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import asyncpg

__all__ = ["grant_app_role", "request_identity"]

# `request_identity` is called with a connection acquired from a pool
# (`asyncpg.pool.PoolConnectionProxy`, the #101 request-path shape) or a
# plain `asyncpg.Connection` (this module's own tests, `migrate()`'s own
# caller); both expose the same `execute`/`transaction` methods (search.py's
# `_Queryable` is the same idea for read-only query helpers).
_Connectable = asyncpg.pool.PoolConnectionProxy | asyncpg.Connection

# The five tables RLS is enabled on (migration 0005_rls.sql). `vault_revisions`
# is append-only: no `update`/`delete` grant.
_FULL_DML_TABLES = ("vault_notes", "notes", "chunks", "links")
_APPEND_ONLY_TABLES = ("vault_revisions",)
_FUNCTIONS = ("mm_readable_ns", "mm_writable_ns")


class RoleRefused(Exception):
    """Raised by `grant_app_role` for a role that must never hold this grant."""


async def grant_app_role(conn: _Connectable, role: str) -> None:
    """Grant `role` exactly the privileges a request connection needs.

    Must run as the owner (the role `migrate.migrate` applied `0005_rls.sql`
    as) - a non-owner cannot grant table privileges it does not itself hold
    with `GRANT OPTION`, and this function does not attempt to work around
    that. Idempotent: safe to call again for a role that already holds the
    grant.

    Refuses a role that is a superuser, has `BYPASSRLS`, or is the owner
    itself (`current_user`) - every one of those bypasses row security
    entirely, which defeats the point of granting it the request role
    (ADR-0008 addendum: "the bypass is tied to the owner credential, not to
    a setting any code can flip").
    """
    row = await conn.fetchrow(
        "select rolsuper, rolbypassrls from pg_roles where rolname = $1", role
    )
    if row is None:
        raise RoleRefused(f"role {role!r} does not exist")
    if row["rolsuper"]:
        raise RoleRefused(f"role {role!r} is a superuser")
    if row["rolbypassrls"]:
        raise RoleRefused(f"role {role!r} has BYPASSRLS")

    owner = await conn.fetchval("select current_user")
    if role == owner:
        raise RoleRefused(f"role {role!r} is the owner that ran this migration")

    async with conn.transaction():
        for table in _FULL_DML_TABLES:
            await conn.execute(f'grant select, insert, update, delete on "{table}" to "{role}"')
        for table in _APPEND_ONLY_TABLES:
            await conn.execute(f'grant select, insert on "{table}" to "{role}"')
        # `chunks.id` is a plain `bigserial`, not an identity column: its
        # sequence needs its own `USAGE` grant for `insert` to work. The
        # other tables' primary keys are either client-supplied text
        # (ULIDs) or `generated always as identity` (`namespaces.id`,
        # `break_glass_grants.id`), which does not require a separate
        # sequence grant on top of table `INSERT`.
        await conn.execute(f'grant usage on sequence "chunks_id_seq" to "{role}"')
        for function in _FUNCTIONS:
            await conn.execute(f'grant execute on function "{function}"() to "{role}"')


@asynccontextmanager
async def request_identity(
    conn: _Connectable,
    *,
    role: str,
    oid: str,
    roles: Sequence[str] = (),
    break_glass: int | None = None,
) -> AsyncIterator[_Connectable]:
    """Open a transaction on `conn`, switch role and set the identity for it.

    `role` must already hold the grant `grant_app_role` gives it - this
    function does not check that itself, since checking it costs a
    round-trip every caller would pay for nothing on the happy path.

    `roles` is the comma-separated format `mm_readable_ns`/`mm_writable_ns`
    expect in `app.roles` (Entra app role values, e.g. `Memory.Curator`);
    joined here so callers pass a plain sequence of strings. `break_glass`
    is a `break_glass_grants.id`, or `None` for "no break-glass grant in
    play this transaction".

    Everything is set with `set_config(name, value, true)` - transaction-
    local, reverted automatically at `COMMIT`/`ROLLBACK` regardless of pool
    behaviour on release (module docstring). Yields `conn` back so callers
    can run their statements inside the same transaction.
    """
    async with conn.transaction():
        await conn.execute("select set_config('role', $1, true)", role)
        await conn.execute("select set_config('app.oid', $1, true)", oid)
        await conn.execute("select set_config('app.roles', $1, true)", ",".join(roles))
        await conn.execute(
            "select set_config('app.break_glass', $1, true)",
            "" if break_glass is None else str(break_glass),
        )
        yield conn
