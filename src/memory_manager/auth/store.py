# SPDX-License-Identifier: AGPL-3.0-only
"""Postgres persistence for the embedded OAuth authorization server (ADR-0004, #36).

Every function here takes the pool as its first argument, the same
convention `memory_manager.auth.tokens` already uses for static tokens -
there is no `Store` class of its own, just plain functions against the
`oauth_*` tables `db/migrations/0003_oauth.sql` creates.

Every secret this module ever receives as a plaintext argument (a pending
id, an authorization code, an access/refresh token) is hashed with SHA-256
before it touches a query; nothing here ever stores or logs that plaintext
(CLAUDE.md "token hashes only"). `oauth_tokens` rows are never hard-deleted
on rotation - `revoke_token_row`/`revoke_family` only ever set `revoked_at`,
so `auth.provider` can tell "never existed" apart from "already used once"
when a refresh token is replayed. `cleanup_expired`/`cleanup_stale_clients`
are the only functions that actually `DELETE`, and only once a row is well
past being useful to anyone.

One exception to "hashed, never plaintext": a confidential DCR client's
`client_secret` (RFC 7591 §3.2.1 - the SDK's `register.py` mints one by
default, for every client that does not explicitly request
`token_endpoint_auth_method: "none"`). It cannot be hashed like every other
secret here: the SDK's own `ClientAuthenticator.authenticate_request`
(`mcp/server/auth/middleware/client_auth.py`, confirmed by reading it, not
assumed) authenticates a `/token`/`/revoke` call by calling `provider.
get_client()` and comparing the request's secret against
`client.client_secret` **in plaintext** (`hmac.compare_digest(client.
client_secret.encode(), request_client_secret.encode())`) - there is no
parameter or subclassing seam to make that comparison run against a hash
instead; `create_auth_routes` builds its own `ClientAuthenticator(provider)`
internally. So `get_client` has to be able to hand back the actual secret.
`save_client`/`get_client` instead encrypt just that one field at rest with
`ClientSecretCipher` (`cryptography.fernet.Fernet`, authenticated
encryption, already a transitive dependency of this project's installed
`mcp` through `pyjwt[crypto]` - confirmed in `uv.lock`, not a new one) keyed
by `OAUTH_CLIENT_SECRET_KEY`: a stolen `oauth_clients.client_info` row alone
is useless without that key, the same bar the plaintext-Bring!-refresh-token
field in the reference implementation's `store.py` meets with the same
primitive. Every other field of `client_info` (redirect URIs, client name,
...) stays plain JSON - nothing else in it is a credential.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import asyncpg
from cryptography.fernet import Fernet, InvalidToken

__all__ = [
    "CleanupStats",
    "ClientSecretCipher",
    "PendingRow",
    "StoredCode",
    "StoredToken",
    "cleanup",
    "delete_pending",
    "get_client",
    "get_code",
    "get_pending",
    "get_token",
    "revoke_family",
    "revoke_token_row",
    "save_client",
    "save_code",
    "save_pending",
    "save_token",
    "take_code",
]

#: The `client_info` key `ClientSecretCipher` encrypts/decrypts - RFC 7591 §3.2.1's
#: `client_secret`, the one field of a DCR client record that is a credential.
_CLIENT_SECRET_FIELD = "client_secret"  # noqa: S105 - a JSON key name, not a credential

#: Keep an expired/revoked token row around this long before a cleanup run purges it -
#: long enough that a late refresh-token replay still gets caught (`get_token`'s
#: `include_revoked`) before the evidence of the earlier, legitimate use is gone.
_TOKEN_CLEANUP_GRACE = timedelta(days=7)

#: A DCR-registered client younger than this is never swept, even with no live token -
#: the gap between `/register` and the first `/token` call (a user working through the
#: consent screen) must never look like an abandoned client.
_CLIENT_CLEANUP_AGE = timedelta(days=30)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ClientSecretCipher:
    """Authenticated encryption for the one credential inside a DCR client's
    `client_info`: its `client_secret` (see this module's docstring for why it
    cannot be hashed like every other secret here instead).
    """

    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "OAUTH_CLIENT_SECRET_KEY must be a Fernet key - generate one with "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from exc

    def encrypt(self, secret: str) -> str:
        return self._fernet.encrypt(secret.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str | None:
        """`token` decrypted, or `None` if it cannot be (e.g. the key changed) - a client
        with an undecryptable secret still exists, it just can no longer authenticate as a
        confidential client until it registers again."""
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken:
            return None


@dataclass(frozen=True)
class PendingRow:
    """One row of `oauth_pending`: the parked parameters of an `/authorize` call."""

    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    state: str | None
    resource: str | None
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class StoredCode:
    """One row of `oauth_auth_codes`: a single-use authorization code, not yet exchanged.

    `user_oid`/`roles` are the Entra principal a completed Entra login established
    (ADR-0006, #213) - `None`/`()` for every `password`/`oidc` code, unchanged.
    """

    client_id: str
    subject: str
    namespaces: tuple[str, ...]
    scopes: tuple[str, ...]
    code_challenge: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    resource: str | None
    expires_at: datetime
    user_oid: str | None
    roles: tuple[str, ...]


@dataclass(frozen=True)
class StoredToken:
    """One row of `oauth_tokens` - an access or a refresh token, by its plaintext's hash.

    `user_oid`/`roles` are the Entra principal this token's grant was issued to
    (`None`/`()` for a `password`/`oidc` token, unchanged); `family_started_at` is the
    family's own session start, carried unchanged across every refresh rotation of it
    (`auth.provider`), `None` for the same `password`/`oidc` case.
    """

    kind: str
    client_id: str
    subject: str
    namespaces: tuple[str, ...]
    scopes: tuple[str, ...]
    resource: str | None
    family_id: str
    client_label: str
    expires_at: datetime
    revoked_at: datetime | None
    user_oid: str | None
    roles: tuple[str, ...]
    family_started_at: datetime | None


@dataclass(frozen=True)
class CleanupStats:
    """What one `cleanup_expired`/`cleanup_stale_clients` run actually removed."""

    pending: int
    codes: int
    tokens: int
    clients: int
    sessions: int


async def save_client(
    pool: asyncpg.Pool,
    client_id: str,
    client_info: dict[str, object],
    *,
    cipher: ClientSecretCipher,
) -> None:
    """Insert or update a DCR-registered client's stored metadata (`OAuthClientInformationFull`).

    `client_info[_CLIENT_SECRET_FIELD]` - if present, a public client (`token_endpoint_auth_method:
    "none"`) has none - is encrypted with `cipher` before it ever reaches `json.dumps`; every other
    field is stored as plain JSON (see this module's docstring for why the secret alone needs this).
    """
    info = dict(client_info)
    secret = info.get(_CLIENT_SECRET_FIELD)
    if isinstance(secret, str):
        info[_CLIENT_SECRET_FIELD] = cipher.encrypt(secret)
    await pool.execute(
        """
        insert into oauth_clients (client_id, client_info)
        values ($1, $2)
        on conflict (client_id) do update set client_info = excluded.client_info
        """,
        client_id,
        json.dumps(info),
    )


async def get_client(
    pool: asyncpg.Pool, client_id: str, *, cipher: ClientSecretCipher
) -> dict[str, object] | None:
    """The stored `client_info` for `client_id`, decrypted, or `None` if never registered."""
    raw = await pool.fetchval(
        "select client_info from oauth_clients where client_id = $1", client_id
    )
    if raw is None:
        return None
    info: dict[str, object] = json.loads(raw)
    secret = info.get(_CLIENT_SECRET_FIELD)
    if isinstance(secret, str):
        info[_CLIENT_SECRET_FIELD] = cipher.decrypt(secret)
    return info


async def save_pending(
    pool: asyncpg.Pool,
    pending_id: str,
    *,
    client_id: str,
    redirect_uri: str,
    redirect_uri_provided_explicitly: bool,
    code_challenge: str,
    state: str | None,
    resource: str | None,
    scopes: Sequence[str],
    ttl: timedelta,
) -> None:
    """Park one `/authorize` call's parameters under `pending_id`, until `/login` completes it."""
    params = {
        "redirect_uri": redirect_uri,
        "redirect_uri_provided_explicitly": redirect_uri_provided_explicitly,
        "code_challenge": code_challenge,
        "state": state,
        "resource": resource,
        "scopes": list(scopes),
    }
    await pool.execute(
        """
        insert into oauth_pending (id_hash, client_id, params, expires_at)
        values ($1, $2, $3, now() + $4)
        """,
        _hash(pending_id),
        client_id,
        json.dumps(params),
        ttl,
    )


async def get_pending(pool: asyncpg.Pool, pending_id: str) -> PendingRow | None:
    """The parked parameters for `pending_id`, or `None` if unknown or expired.

    Filtered to `kind = 'authorize'` (`db/migrations/0006_shared_state.sql`) so a
    pending login `auth.shared_state.PostgresSharedState` parked in this same table
    (`kind != 'authorize'`, no `client_id` of its own) can never be mistaken for one
    of this function's own rows, even on an `id_hash` collision.
    """
    row = await pool.fetchrow(
        "select client_id, params from oauth_pending "
        "where id_hash = $1 and kind = 'authorize' and expires_at > now()",
        _hash(pending_id),
    )
    if row is None:
        return None
    params = json.loads(row["params"])
    return PendingRow(
        client_id=row["client_id"],
        redirect_uri=params["redirect_uri"],
        redirect_uri_provided_explicitly=params["redirect_uri_provided_explicitly"],
        code_challenge=params["code_challenge"],
        state=params["state"],
        resource=params["resource"],
        scopes=tuple(params["scopes"]),
    )


async def delete_pending(pool: asyncpg.Pool, pending_id: str) -> None:
    await pool.execute("delete from oauth_pending where id_hash = $1", _hash(pending_id))


async def save_code(
    pool: asyncpg.Pool,
    code: str,
    *,
    client_id: str,
    subject: str,
    namespaces: Sequence[str],
    scopes: Sequence[str],
    code_challenge: str,
    redirect_uri: str,
    redirect_uri_provided_explicitly: bool,
    resource: str | None,
    ttl: timedelta,
    user_oid: str | None = None,
    roles: Sequence[str] = (),
) -> None:
    """`user_oid`/`roles` are the Entra principal `auth.login.complete_authorization`
    established (ADR-0006, #213); `None`/`()` for a `password`/`oidc` code, unchanged."""
    await pool.execute(
        """
        insert into oauth_auth_codes (
            code_hash, client_id, subject, namespaces, scopes, code_challenge,
            redirect_uri, redirect_uri_provided_explicitly, resource, expires_at,
            user_oid, roles
        ) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, now() + $10, $11, $12)
        """,
        _hash(code),
        client_id,
        subject,
        list(namespaces),
        list(scopes),
        code_challenge,
        redirect_uri,
        redirect_uri_provided_explicitly,
        resource,
        ttl,
        user_oid,
        list(roles),
    )


def _row_to_code(row: asyncpg.Record) -> StoredCode:
    return StoredCode(
        client_id=row["client_id"],
        subject=row["subject"],
        namespaces=tuple(row["namespaces"]),
        scopes=tuple(row["scopes"]),
        code_challenge=row["code_challenge"],
        redirect_uri=row["redirect_uri"],
        redirect_uri_provided_explicitly=row["redirect_uri_provided_explicitly"],
        resource=row["resource"],
        expires_at=row["expires_at"],
        user_oid=row["user_oid"],
        roles=tuple(row["roles"]),
    )


async def get_code(pool: asyncpg.Pool, code: str) -> StoredCode | None:
    """`code`'s stored row, or `None` if it is unknown or already expired.

    Non-destructive: the SDK's own `TokenHandler` calls `auth.provider`'s
    `load_authorization_code` (backed by this) to check PKCE/expiry *before* ever deciding to
    consume the code - `take_code` is the only function here that actually does that.
    """
    row = await pool.fetchrow(
        "select * from oauth_auth_codes where code_hash = $1 and expires_at > now()", _hash(code)
    )
    return _row_to_code(row) if row is not None else None


async def take_code(pool: asyncpg.Pool, code: str) -> bool:
    """Delete `code`; `True` only for the caller that actually removed it (single use).

    The delete-and-check-rowcount happens in one round trip, so two concurrent exchanges of
    the same code can never both succeed (RFC 6749 §10.5: a code is single use).
    """
    result = await pool.execute("delete from oauth_auth_codes where code_hash = $1", _hash(code))
    return result != "DELETE 0"


async def save_token(
    pool: asyncpg.Pool,
    token: str,
    *,
    kind: str,
    client_id: str,
    subject: str,
    namespaces: Sequence[str],
    scopes: Sequence[str],
    resource: str | None,
    family_id: str,
    client_label: str,
    ttl: timedelta,
    user_oid: str | None = None,
    roles: Sequence[str] = (),
    family_started_at: datetime | None = None,
) -> None:
    """`user_oid`/`roles`/`family_started_at` are the Entra principal and the family's
    own session start (ADR-0006, #213) - `None`/`()`/`None` for a `password`/`oidc`
    token, unchanged. `auth.provider._issue` sets `family_started_at` once, on the
    first token of a family, and carries that same value, unmodified, through every
    later rotation of it."""
    await pool.execute(
        """
        insert into oauth_tokens (
            token_hash, kind, client_id, subject, namespaces, scopes, resource,
            family_id, client_label, expires_at, user_oid, roles, family_started_at
        ) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, now() + $10, $11, $12, $13)
        """,
        _hash(token),
        kind,
        client_id,
        subject,
        list(namespaces),
        list(scopes),
        resource,
        family_id,
        client_label,
        ttl,
        user_oid,
        list(roles),
        family_started_at,
    )


async def get_token(
    pool: asyncpg.Pool, token: str, kind: str, *, include_revoked: bool = False
) -> StoredToken | None:
    """The stored row for `token` of `kind`, or `None` if unknown, expired, or (unless
    `include_revoked`) revoked.

    `include_revoked=True` is for a refresh token only - `auth.provider` needs to see a
    revoked-but-not-yet-purged row to recognize a replay and revoke the rest of its family,
    something an access-token lookup never needs (an access token is simply invalid once
    revoked, whichever way that happened).
    """
    row = await pool.fetchrow(
        "select * from oauth_tokens where token_hash = $1 and kind = $2 and expires_at > now()",
        _hash(token),
        kind,
    )
    if row is None:
        return None
    if row["revoked_at"] is not None and not include_revoked:
        return None
    return StoredToken(
        kind=row["kind"],
        client_id=row["client_id"],
        subject=row["subject"],
        namespaces=tuple(row["namespaces"]),
        scopes=tuple(row["scopes"]),
        resource=row["resource"],
        family_id=str(row["family_id"]),
        client_label=row["client_label"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        user_oid=row["user_oid"],
        roles=tuple(row["roles"]),
        family_started_at=row["family_started_at"],
    )


async def revoke_token_row(pool: asyncpg.Pool, token: str, kind: str) -> None:
    """Soft-revoke one token row - used when rotating a refresh token (ADR-0004)."""
    await pool.execute(
        "update oauth_tokens set revoked_at = now() "
        "where token_hash = $1 and kind = $2 and revoked_at is null",
        _hash(token),
        kind,
    )


async def revoke_family(pool: asyncpg.Pool, family_id: str) -> None:
    """Soft-revoke every token (access and refresh) sharing `family_id`.

    Used both for an explicit `/revoke` call and for the nuclear response to a detected
    refresh-token replay: the whole grant is distrusted, not just the one token presented.
    """
    await pool.execute(
        "update oauth_tokens set revoked_at = now() where family_id = $1 and revoked_at is null",
        family_id,
    )


async def cleanup(pool: asyncpg.Pool) -> CleanupStats:
    """Hard-delete rows that are stale well beyond being useful to anyone.

    Pending authorizations and authorization codes are deleted as soon as they expire (their
    own `expires_at`, both minutes-scale); tokens are kept for `_TOKEN_CLEANUP_GRACE` past
    their own `expires_at` - replay detection (`revoke_family` on a reused refresh token)
    only needs the row to still exist, not to live forever, but purging it the instant it
    expires would erase the evidence an operator might still want to look at. A DCR-registered
    client older than `_CLIENT_CLEANUP_AGE` with no token referencing it is swept last, so a
    client whose last token this same run just deleted is still eligible in the same pass.

    Account sessions (`account/sessions.py`, #228) are swept by their own absolute
    `expires_at`, deleted the instant it passes - unlike a refresh token, an expired
    session carries no replay evidence worth keeping around. This is the one shared
    place both `http.py`'s `_run_cleanup_iteration` (today, the Git backend) and the
    Postgres-mode worker (WP-23) call, so the sweep lives here rather than in either
    caller.
    """
    pending = await pool.fetchval(
        "with deleted as (delete from oauth_pending where expires_at <= now() returning 1) "
        "select count(*) from deleted"
    )
    codes = await pool.fetchval(
        "with deleted as (delete from oauth_auth_codes where expires_at <= now() returning 1) "
        "select count(*) from deleted"
    )
    tokens = await pool.fetchval(
        "with deleted as ("
        "delete from oauth_tokens where expires_at <= now() - $1::interval returning 1"
        ") select count(*) from deleted",
        _TOKEN_CLEANUP_GRACE,
    )
    clients = await pool.fetchval(
        "with deleted as ("
        "delete from oauth_clients c where c.created_at <= now() - $1::interval "
        "and not exists (select 1 from oauth_tokens t where t.client_id = c.client_id) "
        "returning 1"
        ") select count(*) from deleted",
        _CLIENT_CLEANUP_AGE,
    )
    sessions = await pool.fetchval(
        "with deleted as (delete from account_sessions where expires_at <= now() returning 1) "
        "select count(*) from deleted"
    )
    return CleanupStats(
        pending=pending, codes=codes, tokens=tokens, clients=clients, sessions=sessions
    )
