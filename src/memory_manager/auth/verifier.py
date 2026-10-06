# SPDX-License-Identifier: AGPL-3.0-only
"""`TokenVerifier` for static bearer tokens (ADR-0004), backed by Postgres.

`StaticTokenVerifier.verify_token` is what the SDK's `BearerAuthBackend`
calls for every request to `/mcp` once `http.py` wires it into
`Server.streamable_http_app(token_verifier=...)`: `None` for an unknown,
revoked or expired token (the SDK answers 401), an `AccessToken` otherwise.

A static token has no OAuth `resource`/`subject` of its own, so those stay
unset; its namespaces are not an `AccessToken` field either, so they travel
in `AccessToken.claims["namespaces"]` - `mcp/authz.py` reads them back out
through `mcp.server.auth.middleware.auth_context.get_access_token()`.
"""

from __future__ import annotations

import asyncpg
from mcp.server.auth.provider import AccessToken, TokenVerifier

from memory_manager.auth.tokens import verify

__all__ = ["StaticTokenVerifier"]

_CLIENT_ID_PREFIX = "static:"


class StaticTokenVerifier(TokenVerifier):
    """Verifies a bearer token against the `static_tokens` table in `pool`."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def verify_token(self, token: str) -> AccessToken | None:
        info = await verify(self._pool, token)
        if info is None:
            return None
        return AccessToken(
            token=token,
            client_id=f"{_CLIENT_ID_PREFIX}{info.name}",
            scopes=list(info.scopes),
            expires_at=int(info.expires_at.timestamp()) if info.expires_at is not None else None,
            claims={"namespaces": list(info.namespaces)},
        )
