# SPDX-License-Identifier: AGPL-3.0-only
"""Protected Resource Metadata (RFC 9728) for `/mcp` (#35, ADR-0004).

What the installed SDK (`mcp` 2.3.0) already does on its own, confirmed by
reading `mcp/server/auth/routes.py` and `mcp/server/lowlevel/server.py`
rather than assumed:

- `MCPServer.streamable_http_app`/`Server.streamable_http_app`, when given
  an `AuthSettings` with a `resource_server_url` and a `token_verifier`,
  mounts exactly **one** PRM route via `create_protected_resource_routes`,
  at the **path-suffixed** well-known URL only
  (`build_resource_metadata_url`: `/.well-known/oauth-protected-resource`
  + the resource path). There is no root-level fallback
  (`/.well-known/oauth-protected-resource` alone) at all.
- That call never passes `resource_name`, so the SDK's own PRM document
  never carries one.
- `RequireAuthMiddleware._send_auth_error` (`mcp/server/auth/middleware/
  bearer_auth.py`) builds the 401 `WWW-Authenticate` header as
  `Bearer error="invalid_token", error_description="...",
  resource_metadata="<path-suffixed URL>"` - the `resource_metadata` part
  is exactly right (RFC 9728 §5.1), but there is no `scope` parameter at
  all, and no public hook to add one from outside (the string is built and
  sent from inside that one method).

What this module adds, because of those two gaps:

- `serve_protected_resource_metadata`, mounted by `http.py` at **both**
  the root and the path-suffixed well-known URLs, on the outer Starlette
  app - ahead of the `Mount("/", ...)` that forwards everything else into
  the SDK's own sub-app, so these two routes are matched first and the
  SDK's own (resource_name-less) path-suffixed route underneath is simply
  never reached. Both routes share one `ProtectedResourceMetadata` object
  (`build_protected_resource_metadata`) and one response renderer
  (`PydanticJSONResponse`, the SDK's own), so the two URLs are
  byte-identical by construction, not by coincidence.
- `http.py`'s own `_ScopeChallengeMiddleware` appends `scope="..."` to any
  outgoing `WWW-Authenticate: Bearer ...` response header that is missing
  one - the only way to add it without reimplementing
  `RequireAuthMiddleware`/`BearerAuthBackend`.

Not touched: a valid token missing a *tool's* required scope
(`memory:read`/`memory:write`) is a `ToolError` result from that tool call,
not an HTTP 403 - `mcp/authz.py`'s `require_scope` runs inside the tool,
after the MCP request already succeeded at the transport level, because
which scope is needed depends on which tool is being called, not on
reaching `/mcp` at all. `AuthSettings.required_scopes` therefore stays
empty (`http.py`), and `RequireAuthMiddleware`'s 403
`error="insufficient_scope"` branch is consequently dead code here - kept
only because `AuthSettings`/`token_verifier` are the SDK's own wiring, not
ours to strip down.
"""

from __future__ import annotations

from typing import cast

from mcp.server.auth.json_response import PydanticJSONResponse
from mcp.shared.auth import ProtectedResourceMetadata
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from memory_manager.app import Services
from memory_manager.auth.scopes import READ_SCOPE, WRITE_SCOPE
from memory_manager.config import ServerConfig, canonical_resource_url

__all__ = [
    "RESOURCE_NAME",
    "SCOPE_CHALLENGE",
    "WELL_KNOWN_ROOT_PATH",
    "build_protected_resource_metadata",
    "path_suffixed_well_known_path",
    "serve_protected_resource_metadata",
]

RESOURCE_NAME = "memory-manager"

_SCOPES = (READ_SCOPE, WRITE_SCOPE)

#: The `scope` parameter for a `WWW-Authenticate: Bearer ...` 401 challenge
#: (RFC 6750 §3) and PRM's `scopes_supported` - every scope this server ever
#: grants, so a client knows the full set to request even before it has any
#: token at all. Deliberately excludes `offline_access` (docs/research/
#: mcp-auth-and-connectors.md §3: "The server (RS) SHOULD NOT list
#: `offline_access` in its challenge or PRM").
SCOPE_CHALLENGE = " ".join(_SCOPES)

WELL_KNOWN_ROOT_PATH = "/.well-known/oauth-protected-resource"


def path_suffixed_well_known_path(mcp_path: str) -> str:
    """RFC 9728 §3.1's path-suffixed well-known URI: the root path plus `mcp_path`.

    `mcp_path` is normalized the same way `canonical_resource_url` normalizes
    a path (exactly one leading slash, no trailing slash) - this and
    `canonical_resource_url(public_url, mcp_path)` must never disagree on
    where the slash goes, or the `resource_metadata` URL `http.py` serves
    and the one it advertises in `WWW-Authenticate` would diverge.
    """
    return f"{WELL_KNOWN_ROOT_PATH}/{mcp_path.strip('/')}"


def build_protected_resource_metadata(config: ServerConfig) -> ProtectedResourceMetadata:
    """The RFC 9728 document served at both well-known URLs, byte-identical.

    `resource` is the canonical MCP URL (origin + `mcp_path`);
    `authorization_servers` carries the canonical *origin* only (ADR-0004:
    "the issuer = canonical origin, which #36 will serve") - the two differ
    by exactly `mcp_path`, which is why this calls `canonical_resource_url`
    twice rather than deriving one from the other. Plain strings are passed
    into `ProtectedResourceMetadata`, not pre-built `AnyHttpUrl` objects: the
    model's `url_preserve_empty_path=True` config (`mcp.shared.auth`) only
    takes effect while validating a string during construction - an
    already-built `AnyHttpUrl("https://host")` has its own trailing slash
    baked in first and that survives unchanged (confirmed against the
    installed SDK's pydantic models, not assumed).

    `config.resource_url()` is what actually enforces `PUBLIC_URL` being set
    (raises `ServerConfigError` otherwise); calling it first, and only then
    reading `config.public_url` directly, is what lets the `cast` below
    stand in for a second explicit check.
    """
    resource = config.resource_url()
    public_url = cast(str, config.public_url)  # resource_url() above already raised otherwise
    issuer = canonical_resource_url(public_url, "")
    return ProtectedResourceMetadata(
        resource=resource,  # type: ignore[arg-type]
        authorization_servers=[issuer],  # type: ignore[list-item]
        scopes_supported=list(_SCOPES),
        bearer_methods_supported=["header"],
        resource_name=RESOURCE_NAME,
    )


async def serve_protected_resource_metadata(request: Request) -> Response:
    """GET handler for both well-known URLs - shared so their JSON is identical.

    404 when bearer-token auth is off (`services.pool is None`, #34): there
    is no token to ever verify a request against, so advertising how to
    get one would be misleading - the same reasoning `http.py`'s vault
    webhook already uses for its own 404-when-unconfigured case.

    404 too when a database *is* configured but the embedded OAuth
    authorization server is not (`app.state.oauth_authorization_server_enabled`,
    #36 - `http.py` sets it once at startup): `authorization_servers` is a
    RFC 9728 **required**, minimum-one-entry field, and this server has no
    honest value to put there without a running AS - a static-token-only
    deployment (today, every deployment: #37's login methods are not wired
    up yet) must not advertise one that does not actually answer
    `/authorize`/`/token`. The 401 on `/mcp` itself still carries a `scope`
    challenge either way (`http.py`'s `_ScopeChallengeMiddleware`); it just
    never carries `resource_metadata` pointing here.

    No `Authorization` required to reach this (RFC 9728 §3.1 metadata is
    public by design); `Access-Control-Allow-Origin: *` is set so a
    browser-based client (e.g. the MCP Inspector) can read it cross-origin
    without a server-side proxy, harmless for metadata with no secrets in
    it.
    """
    services: Services = request.app.state.services
    if services.pool is None or not request.app.state.oauth_authorization_server_enabled:
        return PlainTextResponse("not found", status_code=404)

    config: ServerConfig = request.app.state.config
    metadata = build_protected_resource_metadata(config)
    return PydanticJSONResponse(
        metadata,
        headers={
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
        },
    )
