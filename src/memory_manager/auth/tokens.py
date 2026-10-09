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

`create_token(..., enterprise=True)` additionally enforces ADR-0006 §7
(#224): an owner with at least one role, an expiry no further out than
`max_expires_days` from now, and an owner that already exists in `users` -
otherwise the delta sync could never revoke a departed owner's token
(ADR-0006 §7 addendum 2026-10-08). `cli.py`'s `token create` sets
`enterprise` from `STORAGE_BACKEND=postgres`; deployments without it keep
today's fully optional owner/expiry (F-01 "Existing users"). `token list`
flags a token that predates enterprise mode (or was inserted directly) with
`enterprise_violations`/`owners_not_in_users` instead of re-deriving the
same checks.
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
    "DEFAULT_MAX_EXPIRES_DAYS",
    "MEMORY_ROLES",
    "TokenInfo",
    "create_token",
    "enterprise_violations",
    "list_tokens",
    "owners_not_in_users",
    "revoke_token",
    "verify",
]

_TOKEN_PREFIX = "mm_"  # noqa: S105 - a format marker, not a credential
_TOKEN_ENTROPY_BYTES = 32

ALL_NAMESPACES = "*"

#: ADR-0006 §7's default maximum lifetime for an enterprise static token,
#: overridable per deployment via `STATIC_TOKEN_MAX_DAYS` (`cli.py`).
DEFAULT_MAX_EXPIRES_DAYS = 90

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


def _validate_enterprise(
    *,
    owner_oid: str | None,
    roles: Sequence[str],
    expires_at: datetime | None,
    max_expires_days: int,
) -> None:
    """ADR-0006 §7 (#224): raise `ValueError` naming the first enterprise
    rule `create_token(..., enterprise=True)` does not meet.

    Runs after `_validate_owner_and_roles`, so by the time this is called
    `owner_oid is None` implies `roles == ()` and vice versa - the pairing
    itself is already enforced unconditionally, in both modes. This only
    adds what enterprise mode additionally requires: an owner at all, and an
    expiry within `max_expires_days`.
    """
    if owner_oid is None:
        raise ValueError(
            "enterprise mode (ADR-0006 §7) requires an owner with at least one "
            "role: pass --owner and --role"
        )
    if expires_at is None:
        raise ValueError("enterprise mode (ADR-0006 §7) requires an expiry")
    if expires_at > datetime.now(UTC) + timedelta(days=max_expires_days):
        raise ValueError(
            "enterprise mode (ADR-0006 §7) allows an expiry of at most "
            f"{max_expires_days} days from now, got {expires_at.isoformat()}"
        )


async def create_token(
    pool: asyncpg.Pool,
    name: str,
    *,
    scopes: Sequence[str],
    namespaces: Sequence[str],
    expires_at: datetime | None = None,
    owner_oid: str | None = None,
    roles: Sequence[str] = (),
    enterprise: bool = False,
    max_expires_days: int = DEFAULT_MAX_EXPIRES_DAYS,
) -> tuple[str, TokenInfo]:
    """Create a new token named `name` and return `(plaintext, TokenInfo)`.

    The plaintext is generated here and returned exactly once - nothing else
    in this module can ever reproduce it from what is stored. Raises
    `asyncpg.UniqueViolationError` if `name` is already taken, `ValueError`
    if `roles` contains anything outside `MEMORY_ROLES`, the owner/roles
    pairing is invalid (`_validate_owner_and_roles`), or - with
    `enterprise=True` (ADR-0006 §7, #224; `cli.py`'s `token create` sets it
    from `STORAGE_BACKEND=postgres`) - the owner, expiry or owner-in-`users`
    rule that mode additionally requires is not met (`_validate_enterprise`).
    """
    deduped_roles = _validate_owner_and_roles(owner_oid, roles)
    if enterprise:
        _validate_enterprise(
            owner_oid=owner_oid,
            roles=deduped_roles,
            expires_at=expires_at,
            max_expires_days=max_expires_days,
        )
        owner_known = await pool.fetchval(
            "select exists (select 1 from users where oid = $1)", owner_oid
        )
        if not owner_known:
            raise ValueError(
                f"owner_oid {owner_oid!r} is not in users (ADR-0006 §7): the owner "
                "must sign in at least once before an enterprise token can be "
                "created for it"
            )
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


def enterprise_violations(
    info: TokenInfo, *, max_expires_days: int = DEFAULT_MAX_EXPIRES_DAYS
) -> tuple[str, ...]:
    """Every ADR-0006 §7 enterprise rule `info` fails, as short reason
    strings (`"no-owner"`, `"no-expiry"`, `"expiry-too-far"`) - empty if it
    meets every one of them.

    For `cli.py`'s `token list` to flag a token created before enterprise
    mode turned on, or by a direct SQL insert that bypassed `create_token`
    entirely - a token `create_token(..., enterprise=True)` itself accepted
    never fails any of these, since it already enforced them before the
    insert (`_validate_enterprise`). Does not check the owner against
    `users` - that needs a database round trip `owners_not_in_users` below
    batches once for every token being listed, not once per token here.
    """
    reasons: list[str] = []
    if info.owner_oid is None or not info.roles:
        reasons.append("no-owner")
    if info.expires_at is None:
        reasons.append("no-expiry")
    elif info.expires_at > info.created_at + timedelta(days=max_expires_days):
        reasons.append("expiry-too-far")
    return tuple(reasons)


async def owners_not_in_users(pool: asyncpg.Pool, owner_oids: Sequence[str]) -> frozenset[str]:
    """The subset of `owner_oids` that has no row in `users` (ADR-0006 §7:
    "the owner must exist in users") - one query for every `owner_oid`
    `cli.py`'s `token list` is about to render, instead of one query per
    token. Empty input returns an empty set without querying.
    """
    unique = sorted({oid for oid in owner_oids})
    if not unique:
        return frozenset()
    rows = await pool.fetch("select oid from users where oid = any($1::text[])", unique)
    known = {row["oid"] for row in rows}
    return frozenset(oid for oid in unique if oid not in known)


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
