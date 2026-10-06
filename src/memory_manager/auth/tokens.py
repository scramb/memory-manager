# SPDX-License-Identifier: AGPL-3.0-only
"""Static bearer tokens (ADR-0004): create/list/revoke/verify against Postgres.

Plaintext format: `mm_` + urlsafe base64 of 32 random bytes (ADR-0004's
secret-scanner-friendly prefix; 256 bits of entropy makes plain SHA-256 fine
to hash it with, no need for a slow KDF - CLAUDE.md "token hashes only").
`create_token` is the only function that ever sees the plaintext: it returns
it once to the caller and stores only `sha256(plaintext)`. Every other
function here - `list_tokens`, `verify` - works from the hash or from a
`TokenInfo`, which never carries the plaintext or its hash.

`namespaces = ("*",)` is the literal this module and `memory_manager.auth.
verifier` both read as "every namespace" (`'{*}'` in the migration's own
comment), never as a namespace actually named `*`.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

__all__ = ["ALL_NAMESPACES", "TokenInfo", "create_token", "list_tokens", "revoke_token", "verify"]

_TOKEN_PREFIX = "mm_"  # noqa: S105 - a format marker, not a credential
_TOKEN_ENTROPY_BYTES = 32

ALL_NAMESPACES = "*"

# `verify` only writes `last_used_at` again once this long has passed since the
# last write - every tool call through a busy token would otherwise mean one
# extra write per call, for a timestamp nothing except observability needs to
# the minute.
_LAST_USED_MIN_INTERVAL = timedelta(minutes=1)

_SELECT_COLUMNS = "name, scopes, namespaces, created_at, expires_at, revoked_at, last_used_at"


@dataclass(frozen=True)
class TokenInfo:
    """A static token's metadata - never the plaintext or its `token_hash`."""

    name: str
    scopes: tuple[str, ...]
    namespaces: tuple[str, ...]
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


def _hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _generate_plaintext() -> str:
    return _TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)


def _row_to_info(row: asyncpg.Record) -> TokenInfo:
    return TokenInfo(
        name=row["name"],
        scopes=tuple(row["scopes"]),
        namespaces=tuple(row["namespaces"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        last_used_at=row["last_used_at"],
    )


async def create_token(
    pool: asyncpg.Pool,
    name: str,
    *,
    scopes: Sequence[str],
    namespaces: Sequence[str],
    expires_at: datetime | None = None,
) -> tuple[str, TokenInfo]:
    """Create a new token named `name` and return `(plaintext, TokenInfo)`.

    The plaintext is generated here and returned exactly once - nothing else
    in this module can ever reproduce it from what is stored. Raises
    `asyncpg.UniqueViolationError` if `name` is already taken.
    """
    plaintext = _generate_plaintext()
    row = await pool.fetchrow(
        # `_SELECT_COLUMNS` is a module constant, never caller input - not the
        # string-built-from-a-request-parameter pattern S608 looks for.
        f"""
        insert into static_tokens (name, token_hash, scopes, namespaces, expires_at)
        values ($1, $2, $3, $4, $5)
        returning {_SELECT_COLUMNS}
        """,  # noqa: S608
        name,
        _hash_token(plaintext),
        list(scopes),
        list(namespaces),
        expires_at,
    )
    if row is None:  # pragma: no cover - `insert ... returning` always returns its own row
        raise RuntimeError(f"insert into static_tokens for {name!r} returned no row")
    return plaintext, _row_to_info(row)


async def list_tokens(pool: asyncpg.Pool) -> list[TokenInfo]:
    """Every token's metadata, by name - never the plaintext or its hash."""
    rows = await pool.fetch(
        f"select {_SELECT_COLUMNS} from static_tokens order by name"  # noqa: S608
    )
    return [_row_to_info(row) for row in rows]


async def revoke_token(pool: asyncpg.Pool, name: str) -> bool:
    """Mark the token `name` revoked. Returns whether a not-yet-revoked token was found."""
    result = await pool.execute(
        "update static_tokens set revoked_at = now() where name = $1 and revoked_at is null",
        name,
    )
    return result != "UPDATE 0"


async def verify(pool: asyncpg.Pool, plaintext: str) -> TokenInfo | None:
    """`plaintext`'s `TokenInfo`, or `None` if it is unknown, revoked or expired.

    Updates `last_used_at` as a side effect, at most once per
    `_LAST_USED_MIN_INTERVAL` - best-effort, not awaited-for-correctness: a
    caller only needs the verification result, not confirmation the
    timestamp write landed.
    """
    row = await pool.fetchrow(
        f"select {_SELECT_COLUMNS} from static_tokens where token_hash = $1",  # noqa: S608
        _hash_token(plaintext),
    )
    if row is None:
        return None

    info = _row_to_info(row)
    if info.revoked_at is not None:
        return None
    now = datetime.now(UTC)
    if info.expires_at is not None and info.expires_at <= now:
        return None

    if info.last_used_at is None or now - info.last_used_at >= _LAST_USED_MIN_INTERVAL:
        await pool.execute(
            "update static_tokens set last_used_at = $1 where name = $2", now, info.name
        )

    return info
