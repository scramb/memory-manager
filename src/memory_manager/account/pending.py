# SPDX-License-Identifier: AGPL-3.0-only
"""Pending `/account/login` attempts (#229): a third `oauth_pending.kind`.

`db/migrations/0006_shared_state.sql` already widened `oauth_pending` past
`auth.store`'s own `kind = 'authorize'` rows (a real `/authorize` call,
always carrying a `client_id`) for exactly this reason - so that more than
one caller could park state in that one table without either ever being
mistaken for the other, even on an `id_hash` collision. `auth.shared_state`'s
`PostgresSharedState` already uses `kind = 'login_pending'` for the OIDC/
Entra `state` round trip; `_KIND` here is a third value neither of those two
filters ever matches.

This is deliberately its own tiny module, not a change to `auth.store`
(whose `get_pending` hardcodes `kind = 'authorize'`, and whose `save_pending`/
`PendingRow` carry a `client_id`, PKCE challenge, `redirect_uri` and scopes
this flow has none of): logging into `/account` has nothing to park beyond
"has a login attempt been started, and is it still within its own time
budget" - `pending_exists` is a non-destructive read (called on every `GET`/
`POST {LOGIN_PATH}`, including a retried wrong password, the same role
`auth.store.get_pending` plays for the OAuth flow); only `complete_pending`
ever deletes a row, exactly once, when the login actually finishes.

`pending_id` is hashed with SHA-256 before it ever touches a query, the same
reason `auth.store` hashes its own pending ids (CLAUDE.md "token hashes
only"): it round-trips through a URL and a form field a browser holds, and a
stolen row alone must not be enough to finish someone else's login.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

import asyncpg

__all__ = ["PENDING_TTL", "complete_pending", "create_pending", "pending_exists"]

#: Never `'authorize'` (`auth.store`'s own) or `'login_pending'`
#: (`auth.shared_state.PostgresSharedState`'s own) - see the module docstring.
_KIND = "account_login"

#: How long a started `/account/login` attempt stays valid - matches
#: `auth.provider`'s own OAuth pending-authorization TTL (`_PENDING_TTL`), since
#: neither flow should give a human a meaningfully different window to finish
#: a login they just started.
PENDING_TTL = timedelta(minutes=10)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def create_pending(pool: asyncpg.Pool, *, ttl: timedelta = PENDING_TTL) -> str:
    """Park a new `/account/login` attempt and return its plaintext id - the `pending`
    an `Authenticator.handle` call is shown, the same role `auth.provider.authorize`'s
    own `pending_id` plays for the OAuth flow."""
    pending_id = secrets.token_urlsafe(32)
    await pool.execute(
        "insert into oauth_pending (id_hash, client_id, kind, params, expires_at) "
        "values ($1, null, $2, $3, now() + $4)",
        _hash(pending_id),
        _KIND,
        "{}",
        ttl,
    )
    return pending_id


async def pending_exists(pool: asyncpg.Pool, pending_id: str) -> bool:
    """Whether `pending_id` is still parked and unexpired - non-destructive, so a
    retried `GET`/`POST {LOGIN_PATH}` (e.g. a wrong-password retry) keeps resolving the
    same pending attempt rather than consuming it."""
    row = await pool.fetchval(
        "select 1 from oauth_pending where id_hash = $1 and kind = $2 and expires_at > now()",
        _hash(pending_id),
        _KIND,
    )
    return row is not None


async def complete_pending(pool: asyncpg.Pool, pending_id: str) -> bool:
    """Consume `pending_id` - `True` if a still-valid row was actually removed (the
    login may finish), `False` if it was unknown, expired, or already completed by a
    concurrent request (the same "cannot be reused" guarantee `auth.provider.
    complete_authorization` gives the OAuth flow)."""
    result = await pool.execute(
        "delete from oauth_pending where id_hash = $1 and kind = $2 and expires_at > now()",
        _hash(pending_id),
        _KIND,
    )
    return result != "DELETE 0"
