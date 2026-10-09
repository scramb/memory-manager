# SPDX-License-Identifier: AGPL-3.0-only
"""Entra user records (ADR-0006 "New tables: users ..., user_groups cache"; #213).

Plain functions against the `users`/`user_groups` tables `0005_rls.sql`
creates and `0010_oauth_token_principal.sql` extends, the same convention
`memory_manager.auth.tokens`/`memory_manager.auth.store` already use - no
`Store` class of its own.

`upsert_user`/`replace_groups` are the two writes a completed Entra login
(#215) performs; `touch_last_seen` is the refresh-time re-check's own write
(ADR-0006 §5, #216). `get_user` is what `auth.verifier` already calls, for
the `disabled_at` check on every verification of a user-bound OAuth access
token (ADR-0009 §3: group membership and disabled state are read live from
Postgres, never copied into the token row, so a change is visible on every
replica at once).

`mark_disabled`/`revoke_all_credentials`/`disable_user`/`enable_user` are
ADR-0006 §6's deprovisioning (#222). `mark_disabled` only ever stamps
`users.disabled_at`; `revoke_all_credentials` only ever revokes
`oauth_tokens`/`static_tokens`; `disable_user` is the one call that does
both, atomically, plus the audit entry - `auth.login_entra.
EntraAuthenticator.check_refresh` (#216) and the later Graph delta-sync
worker (ADR-0006 §6, #223) are its two callers. `revoke_all_credentials` is
also exposed on its own, reusable by WP-26's admin "revoke all sessions"
action (#235) without disabling the user. Both take a `_Queryable` (a pool
or a caller-held connection, the same seam `memory_manager.search` already
uses for the same reason) so `disable_user` can run its own write and
`revoke_all_credentials` inside one transaction, while a caller that already
holds a connection of its own - the delta-sync worker's own per-user
transaction - can join that instead of opening a second one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import asyncpg
import asyncpg.pool

from memory_manager.audit import AuditWriter

__all__ = [
    "RevocationCounts",
    "User",
    "disable_user",
    "enable_user",
    "get_user",
    "group_ids",
    "mark_disabled",
    "replace_groups",
    "revoke_all_credentials",
    "touch_last_seen",
    "upsert_user",
]

# A pool or a single connection acquired from one (including one already inside a
# caller's own transaction) - both expose the same `execute`/`fetchval` methods, the
# same `_Queryable` idiom `memory_manager.search` already uses, for the same reason:
# `disable_user` needs its writes and `revoke_all_credentials`'s to share one
# transaction, while a standalone caller (a future admin action, a test) just wants
# to pass its pool.
_Queryable = asyncpg.Pool | asyncpg.pool.PoolConnectionProxy | asyncpg.Connection

_SELECT_COLUMNS = "oid, tid, display_name, disabled_at, last_seen, groups_fetched_at"


@dataclass(frozen=True)
class User:
    """One row of `users` - never `user_groups`, which `group_ids` reads separately."""

    oid: str
    tid: str
    display_name: str
    disabled_at: datetime | None
    last_seen: datetime | None
    groups_fetched_at: datetime | None


def _row_to_user(row: asyncpg.Record) -> User:
    return User(
        oid=row["oid"],
        tid=row["tid"],
        display_name=row["display_name"],
        disabled_at=row["disabled_at"],
        last_seen=row["last_seen"],
        groups_fetched_at=row["groups_fetched_at"],
    )


async def upsert_user(pool: asyncpg.Pool, oid: str, *, tid: str, display_name: str) -> User:
    """Create `oid`'s row, or refresh `tid`/`display_name`/`last_seen` on an existing one.

    `disabled_at` is never touched here - the Graph delta sync (ADR-0006 §6, a later
    task) is the only writer of that column; a successful Entra login always implies
    the user is currently enabled, but this function does not act on that, it only
    records that the user was just seen.
    """
    row = await pool.fetchrow(
        f"""
        insert into users (oid, tid, display_name, last_seen)
        values ($1, $2, $3, now())
        on conflict (oid) do update
            set tid = excluded.tid, display_name = excluded.display_name, last_seen = now()
        returning {_SELECT_COLUMNS}
        """,  # noqa: S608 - `_SELECT_COLUMNS` is a module constant, never caller input
        oid,
        tid,
        display_name,
    )
    if row is None:  # pragma: no cover - `insert ... returning` always returns its own row
        raise RuntimeError(f"upsert into users for {oid!r} returned no row")
    return _row_to_user(row)


async def get_user(pool: asyncpg.Pool, oid: str) -> User | None:
    """`oid`'s stored row, or `None` if the user has never signed in."""
    row = await pool.fetchrow(
        f"select {_SELECT_COLUMNS} from users where oid = $1",  # noqa: S608
        oid,
    )
    return _row_to_user(row) if row is not None else None


async def group_ids(pool: asyncpg.Pool, oid: str) -> tuple[str, ...]:
    """`oid`'s cached Entra group object ids, in no particular order.

    Read live on every call - the same "never copied into the token row" rule
    `replace_groups`'s own docstring explains; `auth.verifier` calls this on every
    verification of a user-bound OAuth access token.
    """
    rows = await pool.fetch("select group_id from user_groups where oid = $1", oid)
    return tuple(row["group_id"] for row in rows)


async def mark_disabled(pool: _Queryable, oid: str) -> None:
    """Record that `oid` is disabled or no longer exists in Entra (ADR-0006 §5/§6).

    Idempotent: `disabled_at` is set once, at the first sighting, and never moved by a
    later call - `disable_user` below is this function's only caller today (from
    inside its own transaction), on the same "disabled or missing" condition the
    refresh-time re-check (`EntraAuthenticator.check_refresh`, #216) and the later
    Graph delta-sync worker (ADR-0006 §6, #223) detect; neither should overwrite the
    other's timestamp with a more recent "now".
    """
    await pool.execute(
        "update users set disabled_at = coalesce(disabled_at, now()) where oid = $1", oid
    )


async def touch_last_seen(pool: asyncpg.Pool, oid: str) -> None:
    """Stamp `oid`'s `last_seen` to now - every refresh of an entra-bound token family
    that passed the Graph re-check (ADR-0006 §5, #216) calls this, the same `last_seen`
    column a login itself stamps through `upsert_user`."""
    await pool.execute("update users set last_seen = now() where oid = $1", oid)


async def replace_groups(pool: asyncpg.Pool, oid: str, group_ids_: Sequence[str]) -> None:
    """Atomically replace `oid`'s cached group memberships with exactly `group_ids_`,
    and stamp `users.groups_fetched_at`.

    Runs inside one transaction: a concurrent `group_ids`/RLS read must never observe
    the delete without the matching insert. `users.groups_fetched_at` is still stamped
    even when `group_ids_` is empty - a user with no groups right now still needs the
    "last checked" marker the cache TTL (ADR-0006 §4) reads, which a now-empty
    `user_groups` table can no longer carry on its own `fetched_at` column.
    """
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("delete from user_groups where oid = $1", oid)
        if group_ids_:
            await conn.executemany(
                "insert into user_groups (oid, group_id) values ($1, $2)",
                [(oid, group_id) for group_id in group_ids_],
            )
        await conn.execute("update users set groups_fetched_at = now() where oid = $1", oid)


@dataclass(frozen=True)
class RevocationCounts:
    """How many credentials one `revoke_all_credentials` call actually revoked -
    returned to its caller and, from `disable_user`, written into that call's own
    audit entry (never the token values themselves, CLAUDE.md "token hashes only")."""

    oauth_tokens: int
    static_tokens: int


async def revoke_all_credentials(conn: _Queryable, oid: str) -> RevocationCounts:
    """Revoke every `oauth_tokens` row with `user_oid = oid` and every `static_tokens`
    row with `owner_oid = oid` - without touching `users.disabled_at` (#222).

    Idempotent: an already-revoked row (`revoked_at is not null`) is left alone and
    not counted again, so calling this twice in a row reports zero the second time.
    A separate public function from `disable_user` on purpose - WP-26's admin "revoke
    all sessions" action (#235) calls this on its own, for a user that stays enabled.
    """
    oauth_tokens = await conn.fetchval(
        "with revoked as ("
        "update oauth_tokens set revoked_at = now() "
        "where user_oid = $1 and revoked_at is null "
        "returning 1"
        ") select count(*) from revoked",
        oid,
    )
    static_tokens = await conn.fetchval(
        "with revoked as ("
        "update static_tokens set revoked_at = now() "
        "where owner_oid = $1 and revoked_at is null "
        "returning 1"
        ") select count(*) from revoked",
        oid,
    )
    return RevocationCounts(oauth_tokens=oauth_tokens, static_tokens=static_tokens)


async def disable_user(pool: asyncpg.Pool, oid: str, reason: str) -> RevocationCounts:
    """Mark `oid` disabled and revoke every OAuth token family and static token it
    owns, in one transaction (ADR-0006 §6, #222) - then write one `audit_log` entry
    for the call, success or not-yet-seen alike.

    Idempotent, and safe to call again for a user that is already disabled:
    `mark_disabled` never moves an already-set `disabled_at`, and
    `revoke_all_credentials` only ever touches a not-yet-revoked row, so a second
    call simply reports `RevocationCounts(0, 0)` - but still writes its own audit
    entry, since the call itself (and `reason`) is the fact worth recording, not only
    its effect. `reason` is free text for the audit trail (e.g. "entra refresh:
    disabled or missing in Graph", "admin: offboarding") - never a token value.
    """
    async with pool.acquire() as conn, conn.transaction():
        await mark_disabled(conn, oid)
        counts = await revoke_all_credentials(conn, oid)
    await AuditWriter(pool).record(
        actor=oid,
        client="auth",
        op="disable_user",
        path=None,
        commit_sha=None,
        outcome="ok",
        detail={
            "reason": reason,
            "oauth_tokens_revoked": counts.oauth_tokens,
            "static_tokens_revoked": counts.static_tokens,
        },
    )
    return counts


async def enable_user(pool: asyncpg.Pool, oid: str, reason: str) -> None:
    """Clear `oid`'s `disabled_at` - revokes nothing back (#222) - then write one
    `audit_log` entry for the call, the same "audit every write" shape
    `disable_user` above already gives its own side of this pair (CLAUDE.md:
    "audit log for every write").

    A previously revoked `oauth_tokens`/`static_tokens` row stays revoked
    (`revoke_token_row`/`revoke_family`/`revoke_all_credentials` only ever set
    `revoked_at`, never clear it - CLAUDE.md "never overwrite silently" plus the
    soft-delete convention `auth.store`'s own docstring explains): re-enabling a
    user never revives an old token, it only lets a fresh login issue new ones.

    `reason` is free text for the audit trail (e.g. "entra delta sync: re-enabled
    in Graph", "admin: back from leave") - never a token value, the same
    convention `disable_user`'s own `reason` already follows.
    """
    await pool.execute("update users set disabled_at = null where oid = $1", oid)
    await AuditWriter(pool).record(
        actor=oid,
        client="auth",
        op="enable_user",
        path=None,
        commit_sha=None,
        outcome="ok",
        detail={"reason": reason},
    )
