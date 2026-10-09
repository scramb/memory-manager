# SPDX-License-Identifier: AGPL-3.0-only
"""Browser sessions for `/account` (ADR-0008 addendum 2026-10-08, #228).

Same shape as `auth/tokens.py`'s static tokens and `auth/store.py`'s OAuth
rows: `create` is the only function that ever sees the plaintext session id
- it returns it once, for the caller to put in the cookie, and stores only
`sha256(plaintext)` in `account_sessions` (CLAUDE.md "token hashes only").
256 bits of `secrets.token_urlsafe` entropy makes plain SHA-256 fine to hash
it with, the same reasoning `auth/tokens.py`'s module docstring gives for
static tokens - no slow KDF needed.

Two independent expiries, both from ADR-0008's addendum: an idle timeout
(`_IDLE_TIMEOUT`, 30 min since the session was last looked up) and an
absolute lifetime (`_ABSOLUTE_LIFETIME`, 8 h since creation, unmovable by
activity). `lookup` slides the idle window forward - stamping
`last_seen_at` - on every call that does not fail either check, so a session
only an idle browser tab is holding open still expires on schedule, and an
active one never outlives 8 h. Only the absolute expiry is a stored column
(`expires_at`): it is what `auth/store.cleanup`'s sweep and this migration's
own index are keyed on, since it never moves after creation and a lookup
race around it is harmless (worst case, one extra request succeeds a few
milliseconds past it).

A session for an Entra principal (`oid` set) is invalid once that user is
disabled (ADR-0006 §6, mirrors `auth/verifier.py`'s same check on an OAuth
access token) - `lookup` reads `users.disabled_at` live on every call,
never caching it into the session row, for the same "visible on every
replica at once" reason ADR-0009 §3 gives for group membership. A
`password`/`oidc` session carries no `oid` at all and skips that check,
same as those login modes' OAuth tokens today (`0010_oauth_token_principal.
sql`).

`csrf_token`/`verify_csrf` store nothing extra at all: the token is an HMAC
keyed by the *raw* session id (known only to the browser holding the cookie
and, transiently, to a request that just looked the session up - never
stored anywhere, unlike the hash) over the form name. A CSRF token is
therefore bound to both the session and the specific form without a
database column of its own, and cannot be reproduced by anyone who only
has the stored hash. `verify_csrf` compares in constant time
(`hmac.compare_digest`), the same primitive `http.py`'s webhook signature
check already uses.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import asyncpg

from memory_manager.auth import users

__all__ = [
    "LOGIN_MODES",
    "SessionInfo",
    "create",
    "csrf_token",
    "lookup",
    "revoke",
    "revoke_all_for_oid",
    "verify_csrf",
]

_SESSION_PREFIX = "mms_"
_SESSION_ENTROPY_BYTES = 32

#: Since the session was last looked up (`lookup`'s own sliding window) - ADR-0008
#: addendum 2026-10-08.
_IDLE_TIMEOUT = timedelta(minutes=30)

#: Since the session was created, unmovable by activity - same addendum.
_ABSOLUTE_LIFETIME = timedelta(hours=8)

#: The three `LOGIN_MODE` values `config.py`/`http.py.build_authenticator` accept -
#: what a session's own `login_mode` column may ever carry.
LOGIN_MODES = ("password", "oidc", "entra")

_SELECT_COLUMNS = "subject, oid, roles, login_mode, created_at, last_seen_at, expires_at"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class SessionInfo:
    """One `account_sessions` row - never the plaintext id or its hash."""

    subject: str
    oid: str | None
    roles: tuple[str, ...]
    login_mode: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime


def _hash_session_id(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _generate_plaintext() -> str:
    return _SESSION_PREFIX + secrets.token_urlsafe(_SESSION_ENTROPY_BYTES)


def _row_to_info(row: asyncpg.Record) -> SessionInfo:
    return SessionInfo(
        subject=row["subject"],
        oid=row["oid"],
        roles=tuple(row["roles"]),
        login_mode=row["login_mode"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        expires_at=row["expires_at"],
    )


async def create(
    pool: asyncpg.Pool,
    *,
    subject: str,
    login_mode: str,
    oid: str | None = None,
    roles: Sequence[str] = (),
    clock: Callable[[], datetime] = _utc_now,
) -> str:
    """Create a new session for a completed login and return its plaintext id.

    The plaintext is generated here and returned exactly once - nothing else in this
    module can ever reproduce it from what is stored. `login_mode` must be one of
    `LOGIN_MODES`; the same set the database's own CHECK constraint enforces
    (CLAUDE.md "validated in Python before insert AND by DB CHECK").
    """
    if login_mode not in LOGIN_MODES:
        raise ValueError(f"unknown login_mode {login_mode!r}, expected one of {LOGIN_MODES}")
    plaintext = _generate_plaintext()
    now = clock()
    await pool.execute(
        """
        insert into account_sessions (
            session_hash, subject, oid, roles, login_mode,
            created_at, last_seen_at, expires_at
        ) values ($1, $2, $3, $4, $5, $6, $6, $7)
        """,
        _hash_session_id(plaintext),
        subject,
        oid,
        list(roles),
        login_mode,
        now,
        now + _ABSOLUTE_LIFETIME,
    )
    return plaintext


async def lookup(
    pool: asyncpg.Pool, session_id: str, *, clock: Callable[[], datetime] = _utc_now
) -> SessionInfo | None:
    """`session_id`'s `SessionInfo`, or `None` if it is unknown, idle-expired,
    absolute-expired, or belongs to a now-disabled user.

    Slides the idle window forward as a side effect of every call that does not fail
    one of the checks above: `last_seen_at` is stamped to `clock()`, unconditionally,
    unlike `auth/tokens.py`'s throttled `last_used_at` - the idle timeout is read back
    on the *next* call, so it has to be exact, not just an observability timestamp.
    """
    row = await pool.fetchrow(
        f"select {_SELECT_COLUMNS} from account_sessions where session_hash = $1",  # noqa: S608
        _hash_session_id(session_id),
    )
    if row is None:
        return None

    info = _row_to_info(row)
    now = clock()
    if info.expires_at <= now:
        return None
    if now - info.last_seen_at >= _IDLE_TIMEOUT:
        return None

    if info.oid is not None:
        user = await users.get_user(pool, info.oid)
        if user is None or user.disabled_at is not None:
            return None

    await pool.execute(
        "update account_sessions set last_seen_at = $1 where session_hash = $2",
        now,
        _hash_session_id(session_id),
    )
    return replace(info, last_seen_at=now)


async def revoke(pool: asyncpg.Pool, session_id: str) -> bool:
    """Hard-delete `session_id`'s row. Returns whether a row was actually removed.

    A session is operational state, not vault content - CLAUDE.md's "no MCP tool
    hard-deletes notes" does not apply here, the same way it does not apply to
    `auth/store.py`'s `revoke_token_row`'s eventual cleanup.
    """
    result = await pool.execute(
        "delete from account_sessions where session_hash = $1", _hash_session_id(session_id)
    )
    return result != "DELETE 0"


async def revoke_all_for_oid(pool: asyncpg.Pool, oid: str) -> int:
    """Hard-delete every `account_sessions` row for `oid`. Returns how many rows
    were actually removed.

    `account.admin`'s "revoke access" action (#235) calls this, by `oid` rather
    than one plaintext session id, the same `oid`-keyed delete `storage.erasure.
    erase_user` already performs when erasing a user's identity rows outright -
    the only other caller that ever removes an `account_sessions` row by `oid`
    instead of by the single session a browser presents on logout (`revoke`
    above).
    """
    rows = await pool.fetch(
        "delete from account_sessions where oid = $1 returning session_hash", oid
    )
    return len(rows)


def csrf_token(session_id: str, form: str) -> str:
    """An HMAC of `form`, keyed by the raw `session_id` - bound to both and stored
    nowhere (see this module's docstring)."""
    return hmac.new(session_id.encode("utf-8"), form.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_csrf(session_id: str, form: str, token: str) -> bool:
    """Whether `token` is `csrf_token(session_id, form)`, compared in constant time."""
    return hmac.compare_digest(csrf_token(session_id, form), token)
