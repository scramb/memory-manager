# SPDX-License-Identifier: AGPL-3.0-only
"""Row-level security helpers for namespace permissions (ADR-0008 + addendum, #100).

`migrations/0005_rls.sql` adds `mm_readable_ns()`/`mm_writable_ns()` and
turns on `ENABLE`/`FORCE ROW LEVEL SECURITY` on `vault_notes`,
`vault_revisions`, `notes`, `chunks` and `links`; `0009_namespace_resolution.
sql` adds `mm_ensure_personal_ns()`/`mm_principal_namespaces(text[])`. This
module is the two pieces of Python the SQL alone cannot provide:

- `grant_app_role`: an idempotent, owner-run grant of exactly the
  privileges a request-serving role needs (table DML, the `chunks_id_seq`
  sequence, `EXECUTE` on all four functions) - and nothing on `namespaces`
  or the membership tables, which the `SECURITY DEFINER` functions read
  with the owner's own privileges regardless of the caller.
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

`Principal`/`current_principal`/`request_connection`/`check_app_role` are #116's
own addition, wiring the above into the request path:

- `current_principal` reads the calling request's `oid`/`roles` claims
  straight off `mcp.server.auth.middleware.auth_context.get_access_token()` -
  the SDK's own contextvar accessor, not `memory_manager.mcp.authz.
  current_access_token` (that module sits under `memory_manager.mcp`, which
  imports `memory_manager.app`; `memory_manager.app` needs this module, so
  importing the other way round here would cycle).
- `request_connection` is the one place a request-serving transaction may
  acquire a pool connection for a `FORCE ROW LEVEL SECURITY` content table: it
  raises `NoPrincipal` *before* acquiring one at all if the current request
  carries none, so a missing `oid` claim never silently runs as the owner.
  `storage.postgres.PostgresBackend` (with `app_role` configured) and
  `mcp/server.py`'s `memory_search` are its only two callers.
- `check_app_role` is `app.py`'s own startup gate, run once before
  `grant_app_role` on every process start.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import asyncpg
from mcp.server.auth.middleware.auth_context import get_access_token

__all__ = [
    "NoPrincipal",
    "Principal",
    "check_app_role",
    "current_principal",
    "grant_app_role",
    "request_connection",
    "request_identity",
]

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
# `(name, argument signature)`: `grant_app_role`'s loop below formats the
# signature straight into the `GRANT EXECUTE` statement, so
# `mm_principal_namespaces` (0009_namespace_resolution.sql) - the one
# function here that is not zero-arg - carries its own `(text[])` alongside
# the other three's `()`.
_FUNCTIONS = (
    ("mm_readable_ns", "()"),
    ("mm_writable_ns", "()"),
    ("mm_ensure_personal_ns", "()"),
    ("mm_principal_namespaces", "(text[])"),
)


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
        for function, signature in _FUNCTIONS:
            await conn.execute(f'grant execute on function "{function}"{signature} to "{role}"')


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


class NoPrincipal(Exception):
    """Raised by `request_connection` when the current request carries no principal.

    Fails closed *before* a pool connection is ever acquired, let alone a
    transaction opened (#116: "no principal -> fail closed with a clear
    error"). Stdio mode never reaches here at all (`cli.py`'s `serve --stdio`
    refuses `STORAGE_BACKEND=postgres` outright, ADR-0008 addendum); an HTTP
    request with no bearer token never reaches a tool in the first place (the
    SDK answers 401 before any of this runs). This only fires for a token
    whose claims carry no `oid` - a legacy static token created without an
    owner principal, or (until WP-22's login exists) an OAuth access token,
    which carries none yet.
    """


@dataclass(frozen=True)
class Principal:
    """Who a request-serving transaction acts as (ADR-0008 addendum, #116).

    Read straight from the calling request's `AccessToken.claims` by
    `current_principal` below - never computed, never trusted beyond what the
    token verifier already put there (`auth.verifier`). `groups` is reserved
    for a claim no token issuer sets yet (the app-side permission matrix and
    `me`/alias resolution are #101's own follow-up, not this one) - carried
    here so a future claim needs no new type, but nothing today reads it.
    """

    oid: str
    roles: tuple[str, ...] = ()
    groups: tuple[str, ...] = field(default=())


def current_principal() -> Principal | None:
    """The calling request's `Principal`, or `None` if it carries no `oid` claim.

    Reads `mcp.server.auth.middleware.auth_context.get_access_token()`
    directly - the SDK's own contextvar accessor - rather than
    `memory_manager.mcp.authz.current_access_token`, to keep this module free
    of importing `memory_manager.mcp` (module docstring: `app.py` needs this
    module, and `memory_manager.mcp` imports `memory_manager.app`).

    `None` for stdio (no token concept at all, and refused for
    `STORAGE_BACKEND=postgres` anyway), for an HTTP request the transport
    never attached a token to, and for a token whose claims carry no `oid` -
    a legacy static token (`auth.tokens`'s `owner_oid` is optional) or an
    OAuth access token issued before WP-22's login exists.
    """
    token = get_access_token()
    if token is None:
        return None
    claims = token.claims or {}
    oid = claims.get("oid")
    if oid is None:
        return None
    roles_raw = claims.get("roles") or ()
    groups_raw = claims.get("groups") or ()
    return Principal(
        oid=str(oid),
        roles=tuple(str(role) for role in roles_raw),
        groups=tuple(str(group) for group in groups_raw),
    )


@asynccontextmanager
async def request_connection(pool: asyncpg.Pool, *, role: str) -> AsyncIterator[_Connectable]:
    """Acquire a pool connection, switched to `role` and the caller's identity.

    The one seam a request-serving transaction may use to touch a
    `FORCE ROW LEVEL SECURITY` content table (#116; `storage.postgres.
    PostgresBackend`'s `_content_connection`, `mcp/server.py`'s
    `memory_search`, are its only two callers): raises `NoPrincipal` *before*
    `pool.acquire()` runs at all if `current_principal()` finds none, so a
    missing `oid` claim can never fall through to running as the owner on an
    un-switched connection. Everything the caller does inside the `async with`
    runs in the one transaction `request_identity` opens, under `role` and
    the principal's identity - reverted automatically at `COMMIT`/`ROLLBACK`
    (that function's own docstring), before the connection is released back
    to `pool`.
    """
    principal = current_principal()
    if principal is None:
        raise NoPrincipal(
            "the current request carries no principal (no 'oid' claim) - refusing "
            "to touch row-level-security-protected content as the owner"
        )
    async with (
        pool.acquire() as conn,
        request_identity(conn, role=role, oid=principal.oid, roles=principal.roles) as identified,
    ):
        yield identified


async def check_app_role(conn: _Connectable, role: str) -> None:
    """Fail fast at startup if `role` cannot safely be the request-serving app role.

    The same three checks `grant_app_role` makes (role exists, is not a
    superuser, has no `BYPASSRLS`, is not the owner itself) plus one more:
    `pg_has_role(current_user, role, 'MEMBER')` - the owner must actually be a
    member of `role` for `request_identity`'s `set_config('role', ...)` to
    ever succeed for a non-superuser owner (a superuser owner always passes
    this membership check regardless of any grant - `app.py`'s own, separate
    "superuser owner -> warning only" check covers that risk instead). Meant
    to run once at startup, immediately before `grant_app_role` - which
    repeats the first three checks on its own and would raise the same way,
    just without this module's new membership check.

    Raises `RoleRefused` naming which check failed.
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
        raise RoleRefused(f"role {role!r} is the owner that connected as")

    is_member = await conn.fetchval("select pg_has_role(current_user, $1, 'MEMBER')", role)
    if not is_member:
        raise RoleRefused(
            f"the owner {owner!r} is not a member of role {role!r} - grant it with "
            f'`grant "{role}" to "{owner}"` before this process can switch to it'
        )
