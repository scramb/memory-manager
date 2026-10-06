# SPDX-License-Identifier: AGPL-3.0-only
"""The embedded OAuth 2.1 authorization server (ADR-0004, #36).

`MemoryManagerOAuthProvider` implements the SDK's
`OAuthAuthorizationServerProvider` protocol (`mcp/server/auth/provider.py`,
confirmed by reading it rather than assumed) on top of `auth.store`'s
Postgres tables. The SDK itself still does PKCE verification (`code_verifier`
against the stored `code_challenge`, S256 only - `AuthorizationRequest.
code_challenge_method` is a literal `"S256"`, so a plain-method request
never even reaches this provider) and the whole `/register`, `/token`
request-shape validation (`mcp/server/auth/handlers/*`); what is left to
this provider is storage, issuance and - the one thing the SDK leaves
entirely to the provider (confirmed by reading `mcp/server/auth/handlers/
token.py`: neither `AuthorizationCodeRequest.resource` nor
`RefreshTokenRequest.resource` is ever read again once parsed) - the RFC
8707 audience check: `authorize`/`exchange_authorization_code` reject a
`resource` that is missing or is not the canonical resource URL (both
`invalid_target`; RFC 8707's indicator is REQUIRED by this server, not
merely validated when a client bothers to send one), and every issued
access token carries that same `resource`, checked again on every
verification by `auth.verifier.verify_bearer_token`.

Login is not this module's job (`auth.login`, #37): `authorize` only parks
the request and returns a `/login` URL; `complete_authorization` is what
`auth.login.login_routes` calls once an `Authenticator` has established a
subject, turning the pending authorization into a single-use code.

Token rotation keeps a replayed refresh token tellable apart from one that
never existed: `auth.store.revoke_token_row` only ever soft-revokes (never
deletes) a refresh token on its first use, so a second presentation of the
exact same token is recognized here as a replay - not a "token not found" -
and revokes the whole `family_id` (every access and refresh token the grant
has ever produced), per ADR-0004's "rotating refresh with family
revocation".
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from memory_manager.auth import store
from memory_manager.auth.login import PendingAuthorization
from memory_manager.auth.verifier import OAUTH_ACCESS_TOKEN_PREFIX, verify_bearer_token
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE

__all__ = ["MemoryManagerOAuthProvider"]

_REFRESH_TOKEN_PREFIX = "mmr_"  # noqa: S105 - a format marker, not a credential
_TOKEN_ENTROPY_BYTES = 32

_DEFAULT_SCOPES = (READ_SCOPE, WRITE_SCOPE)

_PENDING_TTL = timedelta(minutes=10)
_CODE_TTL = timedelta(minutes=5)
_ACCESS_TOKEN_TTL = timedelta(hours=1)
_REFRESH_TOKEN_TTL = timedelta(days=30)

#: claude.ai's hosted callback host (docs/research/mcp-auth-and-connectors.md §4) - any
#: client whose redirect URI points here authenticates Claude's hosted surfaces, not
#: Claude Code's loopback redirect; `_client_label_for` reads this to decide the committer
#: identity a write through that client's tokens uses (`mcp/server.py`'s `current_client`).
_CLAUDE_AI_HOSTS = frozenset({"claude.ai", "claude.com"})


class _AuthorizationCode(AuthorizationCode):
    """`AuthorizationCode` plus the subject's namespaces, carried through to `_issue`."""

    namespaces: list[str]


class _RefreshToken(RefreshToken):
    """`RefreshToken` plus everything `exchange_refresh_token` needs without a second query."""

    namespaces: list[str]
    family_id: str
    client_label: str
    revoked: bool


class MemoryManagerOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """Implements `OAuthAuthorizationServerProvider` on top of `auth.store`.

    `resource` is the canonical MCP resource URL (`ServerConfig.resource_url()`,
    includes `mcp_path`); `issuer` is the canonical origin (`mcp_path`-less,
    ADR-0004: "the issuer = canonical origin") - `authorize`/the token endpoints
    compare an incoming `resource` indicator against `resource`, never `issuer`.
    """

    def __init__(
        self, pool: asyncpg.Pool, *, resource: str, issuer: str, client_secret_key: str
    ) -> None:
        self._pool = pool
        self._resource = resource
        self._issuer = issuer
        # `ValueError` on a malformed `client_secret_key` is deliberately not caught here -
        # `http.py` is expected to let it surface as a startup failure, the same way a bad
        # `PUBLIC_URL` does, not something this provider should paper over.
        self._client_secret_cipher = store.ClientSecretCipher(client_secret_key)

    @property
    def resource(self) -> str:
        """The canonical MCP resource URL every issued token is bound to (RFC 8707)."""
        return self._resource

    @property
    def issuer(self) -> str:
        """The canonical origin this authorization server identifies itself as."""
        return self._issuer

    # ---- clients --------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = await store.get_client(self._pool, client_id, cipher=self._client_secret_cipher)
        return OAuthClientInformationFull.model_validate(info) if info is not None else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        info = client_info.model_dump(mode="json")
        await store.save_client(
            self._pool, client_info.client_id, info, cipher=self._client_secret_cipher
        )

    # ---- authorization ----------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource != self._resource:
            # RFC 8707 `resource` is REQUIRED here, not merely checked-if-present: a missing
            # one is rejected exactly like a wrong one (`params.resource` is `None` then,
            # which can never equal `self._resource`, a non-empty string).
            raise AuthorizeError(
                error="invalid_target",
                error_description=f"resource must be {self._resource!r}, got {params.resource!r}",
            )
        default_scopes = client.scope.split(" ") if client.scope else list(_DEFAULT_SCOPES)
        scopes = params.scopes or default_scopes
        pending_id = secrets.token_urlsafe(32)
        await store.save_pending(
            self._pool,
            pending_id,
            client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge,
            state=params.state,
            resource=params.resource,
            scopes=scopes,
            ttl=_PENDING_TTL,
        )
        return f"{self._issuer}/login?pending={pending_id}"

    async def pending_authorization(self, pending_id: str) -> PendingAuthorization | None:
        """The parked `/authorize` call behind `pending_id`, for `auth.login.login_routes`."""
        row = await store.get_pending(self._pool, pending_id)
        if row is None:
            return None
        client_info = await store.get_client(
            self._pool, row.client_id, cipher=self._client_secret_cipher
        )
        client_name = client_info.get("client_name") if client_info is not None else None
        return PendingAuthorization(
            id=pending_id,
            client_id=row.client_id,
            client_name=client_name if isinstance(client_name, str) else None,
            scopes=row.scopes,
            resource=row.resource,
        )

    async def complete_authorization(
        self, pending_id: str, subject: str, namespaces: list[str]
    ) -> str | None:
        """Turn a parked `/authorize` call into a single-use code, for `auth.login.login_routes`.

        `None` if `pending_id` is unknown or expired - including a concurrent completion of the
        same pending authorization, since `auth.store.delete_pending` only removes a still-present
        row, never raises on a missing one.
        """
        row = await store.get_pending(self._pool, pending_id)
        if row is None:
            return None
        await store.delete_pending(self._pool, pending_id)

        code = secrets.token_urlsafe(32)
        await store.save_code(
            self._pool,
            code,
            client_id=row.client_id,
            subject=subject,
            namespaces=namespaces,
            scopes=row.scopes,
            code_challenge=row.code_challenge,
            redirect_uri=row.redirect_uri,
            redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
            resource=row.resource,
            ttl=_CODE_TTL,
        )
        return construct_redirect_uri(
            row.redirect_uri, code=code, state=row.state, iss=self._issuer
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> _AuthorizationCode | None:
        # Non-destructive on purpose: `exchange_authorization_code` is what consumes the code
        # (`auth.store.take_code`) - the SDK calls this to validate PKCE/expiry first, and may
        # never call exchange at all if those checks fail.
        stored = await store.get_code(self._pool, authorization_code)
        if stored is None or stored.client_id != client.client_id:
            return None
        return _AuthorizationCode(
            code=authorization_code,
            scopes=list(stored.scopes),
            expires_at=stored.expires_at.timestamp(),
            client_id=stored.client_id,
            code_challenge=stored.code_challenge,
            redirect_uri=AnyUrl(stored.redirect_uri),
            redirect_uri_provided_explicitly=stored.redirect_uri_provided_explicitly,
            resource=stored.resource,
            subject=stored.subject,
            namespaces=list(stored.namespaces),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        code = authorization_code
        if not isinstance(code, _AuthorizationCode):  # pragma: no cover - defensive
            raise TokenError(
                error="invalid_grant", error_description="authorization code is not recognized"
            )
        if code.resource != self._resource:
            # Same "required, not merely checked" rule as `authorize` - redundant in
            # practice (a code with a missing/wrong `resource` never gets issued, since
            # `authorize` already rejects it), kept as a second, independent check rather
            # than trusted-by-construction.
            raise TokenError(
                error="invalid_target",
                error_description=f"resource must be {self._resource!r}",
            )
        if not await store.take_code(self._pool, code.code):
            raise TokenError(
                error="invalid_grant", error_description="authorization code already used"
            )
        if code.subject is None:  # pragma: no cover - always set by `complete_authorization`
            raise TokenError(
                error="invalid_grant", error_description="authorization code has no subject"
            )
        return await self._issue(
            client_id=client.client_id,
            subject=code.subject,
            namespaces=code.namespaces,
            scopes=list(code.scopes),
            resource=code.resource,
            family_id=str(uuid4()),
            client_label=_client_label_for(client),
        )

    # ---- refresh ----------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> _RefreshToken | None:
        stored = await store.get_token(self._pool, refresh_token, "refresh", include_revoked=True)
        if stored is None or stored.client_id != client.client_id:
            return None
        return _RefreshToken(
            token=refresh_token,
            client_id=stored.client_id,
            scopes=list(stored.scopes),
            expires_at=int(stored.expires_at.timestamp()),
            resource=stored.resource,
            subject=stored.subject,
            namespaces=list(stored.namespaces),
            family_id=stored.family_id,
            client_label=stored.client_label,
            revoked=stored.revoked_at is not None,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        token = refresh_token
        if not isinstance(token, _RefreshToken):  # pragma: no cover - defensive
            raise TokenError(
                error="invalid_grant", error_description="refresh token is not recognized"
            )
        if token.revoked:
            # A refresh token is only ever soft-revoked (`auth.store.revoke_token_row`) on its
            # first use, so seeing it again here is a replay, not a race with ourselves - the
            # whole grant is now distrusted, including whatever pair replaced it since.
            await store.revoke_family(self._pool, token.family_id)
            raise TokenError(
                error="invalid_grant",
                error_description="refresh token was already used; the grant has been revoked",
            )
        await store.revoke_token_row(self._pool, token.token, "refresh")
        if token.subject is None:  # pragma: no cover - always set by `_issue`
            raise TokenError(
                error="invalid_grant", error_description="refresh token has no subject"
            )
        return await self._issue(
            client_id=client.client_id,
            subject=token.subject,
            namespaces=token.namespaces,
            scopes=scopes or token.scopes,
            resource=token.resource,
            family_id=token.family_id,
            client_label=token.client_label,
        )

    # ---- access -------------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        return await verify_bearer_token(self._pool, token, oauth_resource=self._resource)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "refresh" if isinstance(token, RefreshToken) else "access"
        stored = await store.get_token(self._pool, token.token, kind, include_revoked=True)
        if stored is not None:
            await store.revoke_family(self._pool, stored.family_id)

    # ---- helpers --------------------------------------------------------------

    async def _issue(
        self,
        *,
        client_id: str,
        subject: str,
        namespaces: list[str],
        scopes: list[str],
        resource: str | None,
        family_id: str,
        client_label: str,
    ) -> OAuthToken:
        access = OAUTH_ACCESS_TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
        refresh = _REFRESH_TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
        for token, kind, ttl in (
            (access, "access", _ACCESS_TOKEN_TTL),
            (refresh, "refresh", _REFRESH_TOKEN_TTL),
        ):
            await store.save_token(
                self._pool,
                token,
                kind=kind,
                client_id=client_id,
                subject=subject,
                namespaces=namespaces,
                scopes=scopes,
                resource=resource,
                family_id=family_id,
                client_label=client_label,
                ttl=ttl,
            )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",  # noqa: S106 - the RFC 6749 token type, not a credential
            expires_in=int(_ACCESS_TOKEN_TTL.total_seconds()),
            refresh_token=refresh,
            scope=" ".join(scopes),
        )


def _client_label_for(client: OAuthClientInformationFull) -> str:
    """`"claude-ai"` if any registered redirect URI points at claude.ai/claude.com, else
    `"claude-code"` (`vault.repo.author_for`'s two machine-client identities, #36 step 7)."""
    for redirect_uri in client.redirect_uris or ():
        if urlsplit(str(redirect_uri)).hostname in _CLAUDE_AI_HOSTS:
            return "claude-ai"
    return "claude-code"
