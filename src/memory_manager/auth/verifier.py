# SPDX-License-Identifier: AGPL-3.0-only
"""Bearer-token verification (ADR-0004), backed by Postgres - static tokens and OAuth access
tokens alike.

`StaticTokenVerifier.verify_token` is what the SDK's `BearerAuthBackend`
calls for every request to `/mcp` once `http.py` wires it into
`Server.streamable_http_app(token_verifier=...)`, for a static-token-only
deployment (no OAuth authorization server, #34): `None` for an unknown,
revoked or expired token (the SDK answers 401), an `AccessToken` otherwise.

A static token has no OAuth `resource`/`subject` of its own, so those stay
unset; its namespaces are not an `AccessToken` field either, so they travel
in `AccessToken.claims["namespaces"]` - `mcp/authz.py` reads them back out
through `mcp.server.auth.middleware.auth_context.get_access_token()`.

`verify_bearer_token` is the merge the module docstring of `auth.provider`
talks about: once the embedded OAuth authorization server is enabled, the
SDK only ever calls *one* verifier for every bearer token on `/mcp` (its
`MCPServer` refuses to take both an `auth_server_provider` and a
`token_verifier` at once - confirmed by reading `mcp/server/mcpserver/
server.py`), so `auth.provider.MemoryManagerOAuthProvider.load_access_token`
- the method that one verifier ultimately calls - delegates here to decide
between an OAuth access token (`mma_` prefix) and a static one (`mm_`)
rather than only ever handling its own.
"""

from __future__ import annotations

import asyncpg
from mcp.server.auth.provider import AccessToken, TokenVerifier

from memory_manager.auth import store
from memory_manager.auth.tokens import verify

__all__ = ["OAUTH_ACCESS_TOKEN_PREFIX", "StaticTokenVerifier", "verify_bearer_token"]

_CLIENT_ID_PREFIX = "static:"

#: `auth.provider`'s access tokens always start with this - how `verify_bearer_token`
#: tells an OAuth access token apart from a static one without a DB round trip first.
OAUTH_ACCESS_TOKEN_PREFIX = "mma_"  # noqa: S105 - a format marker, not a credential


class StaticTokenVerifier(TokenVerifier):
    """Verifies a bearer token against the `static_tokens` table in `pool`."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def verify_token(self, token: str) -> AccessToken | None:
        return await _verify_static_token(self._pool, token)


async def verify_bearer_token(
    pool: asyncpg.Pool, token: str, *, oauth_resource: str
) -> AccessToken | None:
    """The one place a bearer token on `/mcp` is checked once OAuth is enabled.

    `token`'s own prefix picks the table it is looked up in - `auth.store`'s
    `oauth_tokens` for an OAuth access token (`OAUTH_ACCESS_TOKEN_PREFIX`),
    `static_tokens` otherwise - never both, so a token forged to look like
    the other kind simply fails its own lookup rather than falling through.
    An OAuth access token whose stored `resource` is not `oauth_resource`
    (ADR-0004: audience enforced) is rejected here, not left to
    `AuthSettings.validate_token_resource` - that SDK flag would also reject
    every static token, which carries no `resource` of its own at all.
    """
    if token.startswith(OAUTH_ACCESS_TOKEN_PREFIX):
        return await _verify_oauth_access_token(pool, token, oauth_resource=oauth_resource)
    return await _verify_static_token(pool, token)


async def _verify_static_token(pool: asyncpg.Pool, token: str) -> AccessToken | None:
    info = await verify(pool, token)
    if info is None:
        return None
    return AccessToken(
        token=token,
        client_id=f"{_CLIENT_ID_PREFIX}{info.name}",
        scopes=list(info.scopes),
        expires_at=int(info.expires_at.timestamp()) if info.expires_at is not None else None,
        claims={"namespaces": list(info.namespaces)},
    )


async def _verify_oauth_access_token(
    pool: asyncpg.Pool, token: str, *, oauth_resource: str
) -> AccessToken | None:
    stored = await store.get_token(pool, token, "access")
    if stored is None or stored.resource != oauth_resource:
        return None
    return AccessToken(
        token=token,
        client_id=stored.client_id,
        scopes=list(stored.scopes),
        expires_at=int(stored.expires_at.timestamp()),
        resource=stored.resource,
        subject=stored.subject,
        claims={"namespaces": list(stored.namespaces), "client_label": stored.client_label},
    )
