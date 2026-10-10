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

`kind` (`KIND_PERSONAL`/`KIND_AGENT`/`KIND_SERVICE`, ADR-0012, #134,
`migrations/0025_token_kind.sql`) says whether a token belongs to a person,
an agent (`KIND_AGENT` - WP-52, not reachable from `cli.py` yet) or a
service; every token `create_token` has ever accepted before this defaults
to `KIND_SERVICE` and keeps behaving exactly as before. A `KIND_PERSONAL`
token additionally requires an owner and an expiry no further out than
`personal_max_days` from now (`_validate_personal`, mirroring the same
"Python before insert AND DB CHECK" convention `_validate_enterprise`
already follows) - `auth.verifier._verify_static_token` is what then
narrows such a token's rights down to its owner's *current* ones on every
single verification (`auth.owner_rights`), not only at creation, since
"the owner's rights can shrink after creation" (ADR-0012 option A).
`created_by`/`description` are plain metadata, never interpreted: the
former is this module's own account of who ran `create_token` (an audit
trail, CLAUDE.md "audit log for every write" - `cli.py` passes a fixed
marker, never personal data), the latter is free text for a human to tell
their own tokens apart.

`list_personal_tokens` and `revoke_token`'s `owner_oid`/`kind` keyword
filters (ADR-0012, #135) are this module's self-service surface:
`/account`'s own token section (`account.tokens`) is their one caller,
scoping both a listing and a revoke to the signed-in user's own
`KIND_PERSONAL` tokens, in SQL - never a Python-side filter over a broader
result. `cli.py`'s `token list`/`token revoke` keep calling `list_tokens`/
`revoke_token` with neither, unchanged: an operator still sees and revokes
every token, by name, regardless of owner or kind.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

from memory_manager.audit import AuditWriter

__all__ = [
    "ALL_NAMESPACES",
    "DEFAULT_MAX_EXPIRES_DAYS",
    "DEFAULT_PERSONAL_MAX_EXPIRES_DAYS",
    "KIND_AGENT",
    "KIND_PERSONAL",
    "KIND_SERVICE",
    "MEMORY_ROLES",
    "TOKEN_KINDS",
    "TokenInfo",
    "create_token",
    "enterprise_violations",
    "list_personal_tokens",
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

#: ADR-0012's default maximum lifetime for a personal token, overridable per
#: deployment via `PERSONAL_TOKEN_MAX_DAYS` (`cli.py`). In enterprise mode
#: (`STORAGE_BACKEND=postgres`) both ceilings apply to the same `expires_at` -
#: `create_token` runs `_validate_personal` and `_validate_enterprise`
#: unconditionally once each applies, so the effective maximum is whichever
#: of `STATIC_TOKEN_MAX_DAYS`/`PERSONAL_TOKEN_MAX_DAYS` is smaller.
DEFAULT_PERSONAL_MAX_EXPIRES_DAYS = 90

#: Who/what a token represents (ADR-0012, #134). `KIND_AGENT` is accepted by
#: the DB CHECK (`migrations/0025_token_kind.sql`) but not reachable from
#: `cli.py` yet - WP-52 wires up agent tokens/policies.
KIND_PERSONAL = "personal"
KIND_AGENT = "agent"
KIND_SERVICE = "service"
TOKEN_KINDS = (KIND_PERSONAL, KIND_AGENT, KIND_SERVICE)

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
    "name, scopes, namespaces, owner_oid, roles, kind, created_by, description, "
    "created_at, expires_at, revoked_at, last_used_at"
)


@dataclass(frozen=True)
class TokenInfo:
    """A static token's metadata - never the plaintext or its `token_hash`.

    `owner_oid`/`roles` are `None`/`()` for a legacy token created without an owner
    principal (ADR-0008 addendum 2026-10-07, #115). `kind` is `KIND_SERVICE` for
    every token predating ADR-0012 (#134) and every token `create_token` is not
    explicitly told otherwise - `created_by`/`description` are `None` for the same
    tokens, since neither existed before this column was added.
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
    # Defaulted, and kept last: every direct `TokenInfo(...)` construction that
    # predates ADR-0012 (#134, this module's own `_row_to_info` aside, always
    # keyword-based) keeps compiling unchanged - `tests/auth/
    # test_static_tokens_enterprise.py`'s own `_info` helper among them.
    kind: str = KIND_SERVICE
    created_by: str | None = None
    description: str | None = None


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
        kind=row["kind"],
        created_by=row["created_by"],
        description=row["description"],
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


def _validate_personal(
    *, owner_oid: str | None, expires_at: datetime | None, max_expires_days: int
) -> None:
    """ADR-0012: raise `ValueError` naming the first `KIND_PERSONAL` rule
    `create_token` does not meet - a personal token identifies one person and
    is useless without both an owner and an expiry.

    Runs after `_validate_owner_and_roles`, so `owner_oid is None` already
    implies `roles == ()` by the time this is called. Independent of
    `_validate_enterprise`: both run unconditionally once they apply, so a
    `KIND_PERSONAL` token created with `enterprise=True` is bound by whichever
    of `max_expires_days`/the enterprise ceiling is smaller (`cli.py`'s
    `PERSONAL_TOKEN_MAX_DAYS`/`STATIC_TOKEN_MAX_DAYS`).
    """
    if owner_oid is None:
        raise ValueError("a personal token (ADR-0012) requires an owner: pass --owner and --role")
    if expires_at is None:
        raise ValueError("a personal token (ADR-0012) requires an expiry: pass --expires-days")
    if expires_at > datetime.now(UTC) + timedelta(days=max_expires_days):
        raise ValueError(
            "a personal token (ADR-0012) allows an expiry of at most "
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
    kind: str = KIND_SERVICE,
    created_by: str | None = None,
    description: str | None = None,
    enterprise: bool = False,
    max_expires_days: int = DEFAULT_MAX_EXPIRES_DAYS,
    personal_max_days: int = DEFAULT_PERSONAL_MAX_EXPIRES_DAYS,
) -> tuple[str, TokenInfo]:
    """Create a new token named `name` and return `(plaintext, TokenInfo)`.

    The plaintext is generated here and returned exactly once - nothing else
    in this module can ever reproduce it from what is stored. Raises
    `asyncpg.UniqueViolationError` if `name` is already taken, `ValueError`
    if `kind` is not one of `TOKEN_KINDS`, `roles` contains anything outside
    `MEMORY_ROLES`, the owner/roles pairing is invalid
    (`_validate_owner_and_roles`), `kind=KIND_PERSONAL` and the owner/expiry
    rule ADR-0012 requires is not met (`_validate_personal`), or -
    with `enterprise=True` (ADR-0006 §7, #224; `cli.py`'s `token create` sets
    it from `STORAGE_BACKEND=postgres`) - the owner, expiry or owner-in-`users`
    rule that mode additionally requires is not met (`_validate_enterprise`).
    Writes one `audit_log` entry (CLAUDE.md "audit log for every write") once
    the insert succeeds - `created_by` is its `actor` if given, the token's
    own `name` otherwise (there is no better actor to record for a token
    `create_token` was called for with no caller identity at all).
    """
    if kind not in TOKEN_KINDS:
        raise ValueError(f"unknown kind {kind!r}, expected one of {TOKEN_KINDS}")
    deduped_roles = _validate_owner_and_roles(owner_oid, roles)
    if kind == KIND_PERSONAL:
        _validate_personal(
            owner_oid=owner_oid, expires_at=expires_at, max_expires_days=personal_max_days
        )
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
            name, token_hash, scopes, namespaces, expires_at, owner_oid, roles,
            kind, created_by, description
        )
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        returning {_SELECT_COLUMNS}
        """,  # noqa: S608
        name,
        _hash_token(plaintext),
        list(scopes),
        list(namespaces),
        expires_at,
        owner_oid,
        list(deduped_roles),
        kind,
        created_by,
        description,
    )
    if row is None:  # pragma: no cover - `insert ... returning` always returns its own row
        raise RuntimeError(f"insert into static_tokens for {name!r} returned no row")
    await AuditWriter(pool).record(
        actor=created_by if created_by is not None else name,
        client="auth",
        op="token_create",
        path=None,
        commit_sha=None,
        outcome="ok",
        detail={"name": name, "kind": kind},
    )
    return plaintext, _row_to_info(row)


async def list_tokens(pool: asyncpg.Pool) -> list[TokenInfo]:
    """Every token's metadata, by name - never the plaintext or its hash."""
    rows = await pool.fetch(
        f"select {_SELECT_COLUMNS} from static_tokens order by name"  # noqa: S608
    )
    return [_row_to_info(row) for row in rows]


async def list_personal_tokens(pool: asyncpg.Pool, owner_oid: str) -> list[TokenInfo]:
    """Every `KIND_PERSONAL` token owned by `owner_oid`, newest first - never the
    plaintext or its hash, same as `list_tokens`.

    `/account`'s own token section (ADR-0012, #135) is the one caller: a signed-in
    user must only ever see their own tokens, never another owner's or a
    `KIND_SERVICE`/`KIND_AGENT` one - both filters are applied here, in SQL, rather
    than left to the caller to narrow a broader `list_tokens()` result down (CLAUDE.md
    "token hashes only" extends to "never return a row the caller did not ask to see
    either").
    """
    rows = await pool.fetch(
        f"select {_SELECT_COLUMNS} from static_tokens "  # noqa: S608
        "where owner_oid = $1 and kind = $2 order by created_at desc",
        owner_oid,
        KIND_PERSONAL,
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


async def revoke_token(
    pool: asyncpg.Pool,
    name: str,
    *,
    actor: str | None = None,
    owner_oid: str | None = None,
    kind: str | None = None,
) -> bool:
    """Mark the token `name` revoked. Returns whether a not-yet-revoked token was found.

    `owner_oid`/`kind` narrow the `update` itself, in SQL - not an extra check run
    after a plain by-name lookup - so a name that exists but belongs to a different
    owner or a different `kind` revokes nothing at all and is indistinguishable from
    an unknown name (ADR-0012, #135: `/account`'s own token section passes both,
    so a user can never revoke anyone else's token, or a `KIND_SERVICE`/`KIND_AGENT`
    one, by guessing or being handed its name). `cli.py`'s `token revoke` passes
    neither - an operator may still revoke any token by name, unchanged.

    Writes one `audit_log` entry (CLAUDE.md "audit log for every write") only when a
    row was actually revoked - a call that found nothing to revoke made no write, so
    there is nothing to audit. `actor` is `name` itself when not given, the same
    fallback `create_token` uses for its own audit entry.
    """
    conditions = ["name = $1", "revoked_at is null"]
    params: list[object] = [name]
    if owner_oid is not None:
        params.append(owner_oid)
        conditions.append(f"owner_oid = ${len(params)}")
    if kind is not None:
        params.append(kind)
        conditions.append(f"kind = ${len(params)}")
    result = await pool.execute(
        # Every piece of `conditions` is one of this function's own fixed literals
        # above, never caller input - not the string-built-from-a-request-parameter
        # pattern S608 looks for (`create_token`'s own `_SELECT_COLUMNS` comment).
        f"update static_tokens set revoked_at = now() where {' and '.join(conditions)}",  # noqa: S608
        *params,
    )
    revoked = result != "UPDATE 0"
    if revoked:
        await AuditWriter(pool).record(
            actor=actor if actor is not None else name,
            client="auth",
            op="token_revoke",
            path=None,
            commit_sha=None,
            outcome="ok",
            detail={"name": name},
        )
    return revoked


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
