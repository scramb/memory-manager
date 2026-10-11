# SPDX-License-Identifier: AGPL-3.0-only
"""Operator-registered confidential OAuth clients (ADR-0015, #197).

The embedded authorization server registers a client through DCR (`/register`) or CIMD
(`auth.cimd`, #38) - some integrations (Gemini Enterprise is the first, #196) support
neither, only a fixed client id and secret an admin enters once. `create`/`list`/`revoke`
below are `cli.py`'s `oauth-client` group: the one new way a client row reaches
`oauth_clients` without ever going through `/register` or a CIMD document.

`create`'s `client_id` is generated with `_CLIENT_ID_PREFIX` (`"mmc_"`), never an `https`
URL - the one shape `auth.provider._is_cimd_client_id` reads as "this is a CIMD client_id"
(an https URL, SEP-991) and the one shape DCR never produces (`mcp/server/auth/handlers/
register.py`: `str(uuid4())`) - so a pre-registered client is never mistaken for either and
always resolved through `auth.store.get_client`, the plain DCR-shaped lookup.

The secret `create` generates is handed back exactly once, like a DCR client's own
(`auth.store`'s module docstring); nothing here can reproduce it afterwards. It is stored
encrypted via `ClientSecretCipher`, never hashed - the SDK's own `ClientAuthenticator.
authenticate_request` (`mcp/server/auth/middleware/client_auth.py`) compares a `/token`
request's secret against the stored one in plaintext, so `auth.store.get_client` has to be
able to hand it back decrypted, the same constraint a confidential DCR client's secret is
already under.

A client registered with `token_endpoint_auth_method="client_secret_basic"` still has to
send `client_id` in the `/token` request body, not only in the `Authorization: Basic` header
- `ClientAuthenticator.authenticate_request` reads `client_id` from the form first, before it
even looks at which auth method the client is registered for, and 400s with "Missing
client_id" if the form does not carry it (confirmed by reading that handler, not assumed).
An operator pointing a `client_secret_basic` integration at this server needs to know that -
#198 (operator docs) is where this belongs for an operator to read; this module only notes
it here, for the next implementer.

`create`/`revoke` each write exactly one `audit_log` entry (CLAUDE.md "audit log for every
write"), with `actor="cli"` - like `auth.tokens`' own `created_by`/`actor` fallback, a fixed
marker naming the tool, never the operator who ran it (no personal data in the audit trail).
`detail` never carries the secret, only `client_id` and metadata.

`revoke` only ever matches a row with `registered_by` set - the one new client-level
revocation surface this feature adds does not reach into a DCR/CIMD client's row at all,
which has no equivalent "revoke the client itself" operation today. It sets
`oauth_clients.revoked_at`, soft-revokes every not-yet-revoked `oauth_tokens` row the client
ever had issued (the same soft-delete `auth.store.revoke_family` already uses, so a replay
of an access token already in flight is told apart from "never existed"), and hard-deletes
its `oauth_pending`/`oauth_auth_codes` rows outright - neither carries replay evidence worth
keeping, the same shape `auth.store.cleanup` already purges both as on their own expiry.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from mcp.shared.auth import OAuthClientInformationFull

from memory_manager.audit import AuditWriter
from memory_manager.auth.scopes import READ_SCOPE, WRITE_SCOPE
from memory_manager.auth.store import ClientSecretCipher, save_client

__all__ = [
    "AUTH_METHODS",
    "AUTH_METHOD_BASIC",
    "AUTH_METHOD_POST",
    "DEFAULT_AUTH_METHOD",
    "RegisteredClientError",
    "RegisteredClientInfo",
    "create",
    "list_clients",
    "revoke",
]

#: Never an `https` URL (see this module's docstring for why that shape is reserved).
_CLIENT_ID_PREFIX = "mmc_"

_SECRET_ENTROPY_BYTES = 32

#: The fixed, non-personal audit/`registered_by` marker every call this module makes uses -
#: there is no caller identity more specific than "someone ran the CLI" to record.
_ACTOR = "cli"

AUTH_METHOD_BASIC = "client_secret_basic"
AUTH_METHOD_POST = "client_secret_post"
AUTH_METHODS = (AUTH_METHOD_BASIC, AUTH_METHOD_POST)
DEFAULT_AUTH_METHOD = AUTH_METHOD_POST

#: RFC 8252 loopback hosts - the only hosts an `http://` redirect URI may point at
#: (every other redirect URI must be `https://`, same bar ADR-0015 sets for exact match).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: Every scope this server supports (`auth.scopes`) - the same default a DCR client that
#: never requested a scope gets at registration (`auth.provider`'s `_DEFAULT_SCOPES`).
_SCOPE = f"{READ_SCOPE} {WRITE_SCOPE}"


class RegisteredClientError(ValueError):
    """`create`'s own `--redirect-uri`/`--auth-method` validation failed."""


@dataclass(frozen=True)
class RegisteredClientInfo:
    """One pre-registered client's metadata, as `list` renders it - never its secret."""

    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    token_endpoint_auth_method: str
    created_at: datetime
    revoked_at: datetime | None


def _validate_redirect_uris(redirect_uris: Sequence[str]) -> tuple[str, ...]:
    """Every `redirect_uris` entry, unchanged, if all of them are an absolute `https` URL,
    or an `http` URL on a loopback host (RFC 8252), with no fragment - raises
    `RegisteredClientError` naming the first one that is not, or if none were given."""
    if not redirect_uris:
        raise RegisteredClientError("at least one --redirect-uri is required")
    validated: list[str] = []
    for uri in redirect_uris:
        parsed = urlsplit(uri)
        if parsed.fragment:
            raise RegisteredClientError(f"redirect URI {uri!r} must not carry a fragment")
        if parsed.scheme == "https" and parsed.netloc:
            validated.append(uri)
            continue
        if parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS:
            validated.append(uri)
            continue
        raise RegisteredClientError(
            f"redirect URI {uri!r} must be an absolute https URL, or an http URL on a "
            f"loopback host {sorted(_LOOPBACK_HOSTS)!r}"
        )
    return tuple(validated)


async def create(
    pool: asyncpg.Pool,
    name: str,
    *,
    redirect_uris: Sequence[str],
    cipher: ClientSecretCipher,
    auth_method: str = DEFAULT_AUTH_METHOD,
) -> tuple[str, str]:
    """Register a new confidential client named `name`; returns `(client_id, secret)`.

    The secret is generated here and returned exactly once - nothing else in this module
    can ever reproduce it from what is stored. Raises `RegisteredClientError` if
    `auth_method` is not one of `AUTH_METHODS`, or `redirect_uris` fails
    `_validate_redirect_uris`. Writes one `audit_log` entry (`"oauth_client_create"`) once
    the insert succeeds.
    """
    if auth_method not in AUTH_METHODS:
        raise RegisteredClientError(
            f"unknown auth method {auth_method!r}, expected one of {AUTH_METHODS}"
        )
    validated_redirect_uris = _validate_redirect_uris(redirect_uris)

    client_id = f"{_CLIENT_ID_PREFIX}{uuid4().hex}"
    secret = secrets.token_urlsafe(_SECRET_ENTROPY_BYTES)
    # `OAuthClientInformationFull` is the same model `auth.provider.register_client` stores a
    # DCR client's own echoed metadata as (`model_dump(mode="json")`) - validating through it
    # here keeps a pre-registered client's stored shape identical to a DCR one's.
    client_info = OAuthClientInformationFull(
        client_id=client_id,
        client_name=name,
        redirect_uris=list(validated_redirect_uris),  # type: ignore[arg-type]
        token_endpoint_auth_method=auth_method,
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=_SCOPE,
        client_secret=secret,
        client_secret_expires_at=0,
    ).model_dump(mode="json")

    await save_client(pool, client_id, client_info, cipher=cipher, registered_by=_ACTOR)
    await AuditWriter(pool).record(
        actor=_ACTOR,
        client="auth",
        op="oauth_client_create",
        path=None,
        commit_sha=None,
        outcome="ok",
        detail={"client_id": client_id, "auth_method": auth_method},
    )
    return client_id, secret


async def list_clients(pool: asyncpg.Pool) -> list[RegisteredClientInfo]:
    """Every pre-registered client's metadata, oldest first - never a secret, revoked or
    not (`list` shows a revoked client's status too, the same shape `auth.tokens.list_tokens`
    already renders for a revoked static token)."""
    rows = await pool.fetch(
        "select client_id, client_info, created_at, revoked_at from oauth_clients "
        "where registered_by is not null order by created_at"
    )
    result: list[RegisteredClientInfo] = []
    for row in rows:
        info = json.loads(row["client_info"])
        result.append(
            RegisteredClientInfo(
                client_id=row["client_id"],
                client_name=info.get("client_name") or row["client_id"],
                redirect_uris=tuple(info.get("redirect_uris") or ()),
                token_endpoint_auth_method=info.get("token_endpoint_auth_method")
                or DEFAULT_AUTH_METHOD,
                created_at=row["created_at"],
                revoked_at=row["revoked_at"],
            )
        )
    return result


async def revoke(pool: asyncpg.Pool, client_id: str) -> int | None:
    """Revoke the pre-registered client `client_id`; returns how many `oauth_tokens` rows it
    soft-revoked, or `None` if `client_id` is unknown, not a pre-registered client, or
    already revoked.

    Runs in one transaction: `oauth_clients.revoked_at` is set, every not-yet-revoked
    `oauth_tokens` row of the client is soft-revoked (the same way a token family is,
    `auth.store.revoke_family`), and its `oauth_pending`/`oauth_auth_codes` rows are deleted
    outright - neither carries replay evidence worth keeping (`auth.store.cleanup` purges
    both the same way, on their own expiry). Writes one `audit_log` entry
    (`"oauth_client_revoke"`) only once a row was actually revoked.
    """
    async with pool.acquire() as conn, conn.transaction():
        revoked_client_id = await conn.fetchval(
            "update oauth_clients set revoked_at = now() "
            "where client_id = $1 and registered_by is not null and revoked_at is null "
            "returning client_id",
            client_id,
        )
        if revoked_client_id is None:
            return None
        revoked_tokens = int(
            await conn.fetchval(
                "with revoked as ("
                "update oauth_tokens set revoked_at = now() "
                "where client_id = $1 and revoked_at is null "
                "returning 1"
                ") select count(*) from revoked",
                client_id,
            )
        )
        await conn.execute("delete from oauth_pending where client_id = $1", client_id)
        await conn.execute("delete from oauth_auth_codes where client_id = $1", client_id)

    await AuditWriter(pool).record(
        actor=_ACTOR,
        client="auth",
        op="oauth_client_revoke",
        path=None,
        commit_sha=None,
        outcome="ok",
        detail={"client_id": client_id, "tokens_revoked": revoked_tokens},
    )
    return revoked_tokens
