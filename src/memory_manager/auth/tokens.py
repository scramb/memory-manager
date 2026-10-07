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

A token's owner principal (`owner_oid` + `roles`, ADR-0008 addendum
2026-10-07 "identity sources and curate", #115) is optional: a legacy token
created without either stays exactly as before, with no `oid`/`roles` claim
(`auth.verifier._verify_static_token`). `create_token` validates `roles`
against `MEMORY_ROLES` and the owner/roles pairing before the insert
(CLAUDE.md "validated in Python before insert AND by DB CHECK");
`migrations/0007_token_principal.sql` enforces the same two rules again in
the database.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

__all__ = [
    "ALL_NAMESPACES",
    "MEMORY_ROLES",
    "TokenInfo",
    "create_token",
    "list_tokens",
    "revoke_token",
    "verify",
]

_TOKEN_PREFIX = "mm_"  # noqa: S105 - a format marker, not a credential
_TOKEN_ENTROPY_BYTES = 32

ALL_NAMESPACES = "*"

#: The three Entra app role values `0005_rls.sql`'s `mm_readable_ns`/`mm_writable_ns`
#: read from `app.roles` (ADR-0008 §3's permission matrix). The only roles a token's
#: `roles` column may ever carry.
MEMORY_ROLES = ("Memory.User", "Memory.Curator", "Memory.Admin")

_OWNER_OID_MAX_LENGTH = 128

# `verify` only writes `last_used_at` again once this long has passed since the
# last write - every tool call through a busy token would otherwise mean one
# extra write per call, for a timestamp nothing except observability needs to
# the minute.
_LAST_USED_MIN_INTERVAL = timedelta(minutes=1)

_SELECT_COLUMNS = (
    "name, scopes, namespaces, owner_oid, roles, created_at, expires_at, revoked_at, last_used_at"
)


@dataclass(frozen=True)
class TokenInfo:
    """A static token's metadata - never the plaintext or its `token_hash`.

    `owner_oid`/`roles` are `None`/`()` for a legacy token created without an owner
    principal (ADR-0008 addendum 2026-10-07, #115).
    """

    name: str
    scopes: tuple[str, ...]
    namespaces: tuple[str, ...]
    owner_oid: str | None
    roles: tuple[str, ...]
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
        owner_oid=row["owner_oid"],
        roles=tuple(row["roles"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        last_used_at=row["last_used_at"],
    )


def _validate_owner_and_roles(owner_oid: str | None, roles: Sequence[str]) -> tuple[str, ...]:
    """Validate the owner/roles pairing and return `roles`, deduplicated in order.

    Mirrors `migrations/0007_token_principal.sql`'s CHECK constraints
    (CLAUDE.md "validated in Python before insert AND by DB CHECK"): every
    role must be one of `MEMORY_ROLES`, a non-empty `roles` requires an
    `owner_oid`, and an `owner_oid` without any `roles` is rejected too
    (ADR-0006 §3: a user without a memory role is denied).
    """
    deduped: list[str] = []
    for role in roles:
        if role not in MEMORY_ROLES:
            raise ValueError(f"unknown role {role!r}, expected one of {MEMORY_ROLES}")
        if role not in deduped:
            deduped.append(role)

    if deduped and owner_oid is None:
        raise ValueError("roles require an owner_oid")
    if owner_oid is not None:
        if not deduped:
            raise ValueError("owner_oid requires at least one role")
        if (
            len(owner_oid) == 0
            or len(owner_oid) > _OWNER_OID_MAX_LENGTH
            or any(char.isspace() or not char.isprintable() for char in owner_oid)
        ):
            raise ValueError(
                f"invalid owner_oid {owner_oid!r}: must be 1-{_OWNER_OID_MAX_LENGTH} characters "
                "with no whitespace or control characters"
            )
    return tuple(deduped)


async def create_token(
    pool: asyncpg.Pool,
    name: str,
    *,
    scopes: Sequence[str],
    namespaces: Sequence[str],
    expires_at: datetime | None = None,
    owner_oid: str | None = None,
    roles: Sequence[str] = (),
) -> tuple[str, TokenInfo]:
    """Create a new token named `name` and return `(plaintext, TokenInfo)`.

    The plaintext is generated here and returned exactly once - nothing else
    in this module can ever reproduce it from what is stored. Raises
    `asyncpg.UniqueViolationError` if `name` is already taken, `ValueError`
    if `roles` contains anything outside `MEMORY_ROLES` or the owner/roles
    pairing is invalid (`_validate_owner_and_roles`).
    """
    deduped_roles = _validate_owner_and_roles(owner_oid, roles)
    plaintext = _generate_plaintext()
    row = await pool.fetchrow(
        # `_SELECT_COLUMNS` is a module constant, never caller input - not the
        # string-built-from-a-request-parameter pattern S608 looks for.
        f"""
        insert into static_tokens (
            name, token_hash, scopes, namespaces, expires_at, owner_oid, roles
        )
        values ($1, $2, $3, $4, $5, $6, $7)
        returning {_SELECT_COLUMNS}
        """,  # noqa: S608
        name,
        _hash_token(plaintext),
        list(scopes),
        list(namespaces),
        expires_at,
        owner_oid,
        list(deduped_roles),
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
