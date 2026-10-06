# SPDX-License-Identifier: AGPL-3.0-only
"""The Streamable HTTP transport for the MCP server (#33).

How one route serves both protocol revisions, confirmed by reading the
installed SDK (`mcp` 2.3.0, see `docs/research/mcp-sdks.md` [P4]) rather
than assumed: `MCPServer.streamable_http_app()` (`mcp/server/mcpserver/
server.py`) delegates to the low-level `Server.streamable_http_app()`
(`mcp/server/lowlevel/server.py`), which builds one
`StreamableHTTPSessionManager` + `StreamableHTTPASGIApp` and mounts it at a
single `Route(streamable_http_path, ...)`. Every request that hits that
route, whichever era it speaks, is ultimately handed to `Server.run()`,
which drives `serve_dual_era_loop` - the exact same loop
`tests/conformance/test_stdio.py` already exercises over stdio for both
2025-11-25 (session-based) and 2026-07-28 (stateless per-request). So
nothing era-specific needs to happen in this module: mounting the one app
`streamable_http_app()` returns is enough for both revisions.

What this module adds on top of that app (none of it SDK-provided):
`/healthz`, `/readyz`, the vault webhook (`/hooks/vault`), and Origin
validation per the MCP spec's DNS-rebinding guidance. The last one is kept
deliberately separate from the SDK's own `TransportSecuritySettings` (which
checks `Host`, not just `Origin`, and is keyed off `host` looking like
`127.0.0.1`/`localhost`/`::1`) - running both would mean two different
Origin policies disagreeing with each other, so `TransportSecuritySettings`
is passed explicitly disabled here and this module's own middleware is the
only Origin check that runs.

`create_app(services_factory, config)` takes a *factory*, not a built
`Services`: building `Services` is async (`app.open_services` clones and
syncs the vault, migrates and reindexes Postgres), so it has to happen
inside this app's own `lifespan`, after Starlette's "lifespan.startup" ASGI
event - not before `Starlette(...)` is even constructed. The MCP sub-app
`build_server(services)` produces depends on that same `services`, so it is
built lazily too, and requests reach it through `_McpMount`, a one-line
ASGI indirection that looks the built sub-app up from `app.state` on every
request rather than capturing it as a constructor argument that does not
exist yet. `custom_route`-free: #35/#36 add OAuth AS routes/middleware the same way
this module adds the webhook - as plain Starlette routes/middleware around
whatever `create_app` already builds, not as a dependency of it.

Static-token bearer auth (#34, ADR-0004) turns on exactly when `services.pool`
is set, i.e. `DATABASE_URL` is configured: the `static_tokens` table a token
verifies against lives there, so there is nothing to verify a token against
otherwise. `AuthSettings.resource_server_url`/`issuer_url` both come from
`config.resource_url()` - there is no real authorization server behind
`issuer_url` yet (no `auth_server_provider` is passed), so no `/authorize`/
`/token` routes are mounted; only the bearer-token middleware and the
Protected Resource Metadata route the SDK adds whenever `token_verifier` is
set. `required_scopes` is left empty: which scope a call needs depends on
the tool it calls (`memory:read` vs `memory:write`), not on reaching `/mcp`
at all, so that check lives in `mcp/server.py`'s tools via `mcp/authz.py`,
not here.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from memory_manager import __commit__, __version__
from memory_manager.app import Services
from memory_manager.auth.verifier import StaticTokenVerifier
from memory_manager.config import ServerConfig
from memory_manager.mcp.server import build_server

__all__ = ["ServicesFactory", "create_app"]

_logger = logging.getLogger(__name__)

HEALTH_PATH = "/healthz"
READY_PATH = "/readyz"
WEBHOOK_PATH = "/hooks/vault"

_SOURCE_URL = "https://github.com/scramb/memory-manager"
_MAX_WEBHOOK_BODY_BYTES = 1024 * 1024

_GITHUB_SIGNATURE_HEADER = "x-hub-signature-256"
_GITEA_SIGNATURE_HEADER = "x-gitea-signature"

ServicesFactory = Callable[[], AbstractAsyncContextManager[Services]]


def create_app(services_factory: ServicesFactory, config: ServerConfig) -> Starlette:
    """Build the Streamable HTTP ASGI app: MCP, health/ready, the vault webhook.

    `services_factory` is called exactly once, inside this app's `lifespan`
    (e.g. `lambda: open_services(os.environ)`); the `Services` it yields is
    torn down when the app shuts down. Origin validation
    (`config.allowed_origins`) wraps every route below, including
    `/healthz`/`/readyz` - a browser sending a disallowed `Origin` gets the
    same 403 everywhere, not just on the MCP endpoint.
    """

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with services_factory() as services:
            token_verifier = (
                StaticTokenVerifier(services.pool) if services.pool is not None else None
            )
            auth = (
                AuthSettings(
                    issuer_url=config.resource_url(),  # type: ignore[arg-type]
                    resource_server_url=config.resource_url(),  # type: ignore[arg-type]
                    # Static tokens carry no RFC 8707 resource indicator of their
                    # own (#34 is bearer tokens only, no OAuth flow to bind one
                    # with yet) - checking it would reject every one of them.
                    validate_token_resource=False,
                )
                if token_verifier is not None
                else None
            )
            # `MCPServer.streamable_http_app` forwards `self.settings.auth`/
            # `self._token_verifier` (set here, at construction) to the
            # lowlevel `Server.streamable_http_app` below - passing them to
            # that call instead would be a no-op, since it reads only its
            # own `self`'s copies.
            mcp = build_server(services, auth=auth, token_verifier=token_verifier)
            mcp_app = mcp.streamable_http_app(
                streamable_http_path=config.mcp_path,
                json_response=config.json_response,
                stateless_http=True,
                host=config.host,
                # This module's own `_OriginValidationMiddleware` is the one
                # Origin check that runs (see the module docstring) - the
                # SDK's host-keyed DNS-rebinding guard stays off so the two
                # never disagree.
                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
            )
            app.state.services = services
            app.state.mcp_app = mcp_app
            async with mcp_app.router.lifespan_context(mcp_app):
                yield

    routes = [
        Route(HEALTH_PATH, endpoint=_healthz, methods=["GET"]),
        Route(READY_PATH, endpoint=_readyz, methods=["GET"]),
        Route(WEBHOOK_PATH, endpoint=_vault_webhook, methods=["POST"]),
        # Mounted last (lowest route-matching precedence), same reasoning
        # `mcp/server/lowlevel/server.py` uses for its own custom routes:
        # the three routes above must win their exact paths before this
        # catch-all gets a chance to.
        Mount("/", app=_McpMount()),
    ]
    middleware = [Middleware(_OriginValidationMiddleware, allowed_origins=config.allowed_origins)]
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    # Known synchronously (no vault/DB work needed), unlike `services`/`mcp_app`
    # above - set right away rather than deferred into `lifespan`.
    app.state.config = config
    return app


class _McpMount:
    """Defers every request to `request.app.state.mcp_app`, built by `lifespan`.

    A plain `Mount(path, app=mcp_app)` cannot work here: `mcp_app` does not
    exist until `services_factory()` has run inside `lifespan`, which is
    after `Starlette(routes=...)` already needs its route table. Starlette
    sets `scope["app"]` to the app instance before routing
    (`starlette.applications.Starlette.__call__`), which is what makes this
    indirection possible without a second, parallel way of finding the
    running app.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        mcp_app: ASGIApp = scope["app"].state.mcp_app
        await mcp_app(scope, receive, send)


class _OriginValidationMiddleware:
    """Per the MCP spec's DNS-rebinding guidance: validate `Origin`, not `Host`.

    A request with no `Origin` header at all is allowed outright - that is
    every non-browser client (Claude Code, curl, another server), which
    never sends one; only a browser does, and only a browser is what DNS
    rebinding threatens. A request that does carry an `Origin` is checked
    against `allowed_origins` exactly, no wildcard matching.
    """

    def __init__(self, app: ASGIApp, *, allowed_origins: tuple[str, ...]) -> None:
        self._app = app
        self._allowed_origins = frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        origin = Headers(scope=scope).get("origin")
        if origin is not None and origin not in self._allowed_origins:
            response = PlainTextResponse("origin not allowed", status_code=403)
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)


async def _healthz(_request: Request) -> Response:
    """ADR-0002 §13: a modified deployment's `/healthz` must point back at its source."""
    return JSONResponse(
        {"status": "ok", "version": __version__, "commit": __commit__, "source": _SOURCE_URL}
    )


async def _readyz(request: Request) -> Response:
    """503 when the vault clone is missing, or a configured database is unreachable."""
    services: Services = request.app.state.services
    vault_ready = (services.vault_root / ".git").is_dir()

    database_ready = True
    if services.pool is not None:
        try:
            await services.pool.fetchval("select 1")
        except Exception:
            # Any failure here means "not ready", not a crash of the endpoint itself.
            database_ready = False

    ready = vault_ready and database_ready
    body = {"ready": ready, "vault": vault_ready, "database": database_ready}
    return JSONResponse(body, status_code=200 if ready else 503)


async def _vault_webhook(request: Request) -> Response:
    """Verify a GitHub/Gitea push webhook's HMAC signature and trigger a sync.

    404 when no `VAULT_WEBHOOK_SECRET` is configured at all (the endpoint
    does not exist, rather than existing just to reject every call); 401 for
    a missing or wrong signature; 413 over the 1 MiB body cap; 202 once a
    verified webhook has triggered `Services.trigger_sync()` - deliberately
    *after* the sync has run, not fire-and-forget, so the response means
    what it says: the vault is caught up by the time the caller sees it.
    """
    config: ServerConfig = request.app.state.config
    secret = config.webhook_secret
    if secret is None:
        return PlainTextResponse("not found", status_code=404)

    body = await _read_capped_body(request, _MAX_WEBHOOK_BODY_BYTES)
    if body is None:
        return PlainTextResponse("payload too large", status_code=413)

    if not _verify_webhook_signature(request.headers, body, secret):
        _logger.warning("rejected a %s request with an invalid or missing signature", WEBHOOK_PATH)
        return PlainTextResponse("invalid signature", status_code=401)

    services: Services = request.app.state.services
    if services.trigger_sync is None:
        # create_app always builds Services through services_factory, which always sets
        # trigger_sync - reaching this would be an open_services()/create_app() wiring bug.
        raise RuntimeError("Services.trigger_sync is None on a Services built for the HTTP app")
    await services.trigger_sync()
    return PlainTextResponse("accepted", status_code=202)


async def _read_capped_body(request: Request, limit: int) -> bytes | None:
    """`request.body()`, but `None` instead of ever buffering more than `limit` bytes.

    Checked against the actual bytes read, not just a `Content-Length`
    header a caller could lie about.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _verify_webhook_signature(headers: Headers, body: bytes, secret: str) -> bool:
    """GitHub's `X-Hub-Signature-256: sha256=<hex>`, or Gitea's `X-Gitea-Signature: <hex>`.

    Neither header present at all is a bad signature too - there is nothing
    to verify, so this never falls back to "unsigned is fine".
    """
    github_signature = headers.get(_GITHUB_SIGNATURE_HEADER)
    if github_signature is not None:
        expected = "sha256=" + _hmac_sha256_hex(secret, body)
        return hmac.compare_digest(expected, github_signature)

    gitea_signature = headers.get(_GITEA_SIGNATURE_HEADER)
    if gitea_signature is not None:
        expected = _hmac_sha256_hex(secret, body)
        return hmac.compare_digest(expected, gitea_signature)

    return False


def _hmac_sha256_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
