# SPDX-License-Identifier: AGPL-3.0-only
"""Entra user records (ADR-0006 "New tables: users ..., user_groups cache"; #213).

Plain functions against the `users`/`user_groups` tables `0005_rls.sql`
creates and `0010_oauth_token_principal.sql` extends, the same convention
`memory_manager.auth.tokens`/`memory_manager.auth.store` already use - no
`Store` class of its own.

`upsert_user`/`replace_groups` are the two writes a completed Entra login
(#215) and the Graph group sync (#214) perform; neither exists yet, so
nothing calls these two today - this module only lays the groundwork
`auth.verifier`'s user-bound claims (`oid`/`roles`/`groups`) and the later
deprovisioning worker (ADR-0006 §6) need. `get_user` is what `auth.verifier`
already calls, for the `disabled_at` check on every verification of a
user-bound OAuth access token (ADR-0009 §3: group membership and disabled
state are read live from Postgres, never copied into the token row, so a
change is visible on every replica at once).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import asyncpg

__all__ = ["User", "get_user", "group_ids", "replace_groups", "upsert_user"]

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
