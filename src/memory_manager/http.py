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
`/healthz`, `/readyz`, the vault webhook (`/hooks/vault`), Protected
Resource Metadata at both well-known URLs (`/.well-known/oauth-protected-
resource[/mcp]`, #35, see `memory_manager.auth.prm`'s module docstring for
exactly what the SDK does and doesn't provide here on its own), the
`scope` parameter on the 401 `WWW-Authenticate` challenge
(`_ScopeChallengeMiddleware`), and Origin validation per the MCP spec's
DNS-rebinding guidance. Origin validation is kept
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
exist yet. `custom_route`-free: #36 will add the OAuth AS's own routes/
middleware the same way this module adds the webhook and PRM below - as
plain Starlette routes/middleware around whatever `create_app` already
builds, not as a dependency of it.

Static-token bearer auth (#34, ADR-0004) turns on exactly when `services.pool`
is set, i.e. `DATABASE_URL` is configured: the `static_tokens` table a token
verifies against lives there, so there is nothing to verify a token against
otherwise. `AuthSettings.resource_server_url` comes from `config.resource_url()`,
which requires `PUBLIC_URL` to be set once `services.pool is not None` -
raising `ServerConfigError` (caught by `cli.py`'s `_serve`) otherwise, since
#35/ADR-0004 forbid falling back to a guessed URL. `AuthSettings.issuer_url`
is set to the same value for now even though it should, strictly, be the
canonical *origin* without `mcp_path` (ADR-0004) - harmless today because no
`auth_server_provider` is passed, so the SDK never mounts `/authorize`/
`/token` off of it; #36 is what will make that distinction matter and fix
it. `required_scopes` is left empty: which scope a call needs depends on
the tool it calls (`memory:read` vs `memory:write`), not on reaching `/mcp`
at all, so that check lives in `mcp/server.py`'s tools via `mcp/authz.py`,
not here - see `memory_manager.auth.prm`'s module docstring for why that
also means the SDK's built-in `insufficient_scope` 403 branch never fires.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import math
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import FrameType
from typing import cast

import asyncpg
import uvicorn
from mcp.server.auth.json_response import PydanticJSONResponse
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from memory_manager import __commit__, __version__
from memory_manager.app import Services
from memory_manager.auth import store
from memory_manager.auth.cimd import ClientMetadataFetcher
from memory_manager.auth.login import (
    Authenticator,
    AuthorizationCompleter,
    PendingAuthorization,
    PendingAuthorizationLookup,
    login_routes,
)
from memory_manager.auth.login_oidc import OidcAuthenticator, oidc_routes
from memory_manager.auth.login_password import PasswordAuthenticator
from memory_manager.auth.metadata import WELL_KNOWN_PATH as OAUTH_METADATA_PATH
from memory_manager.auth.metadata import build_authorization_server_metadata
from memory_manager.auth.prm import (
    SCOPE_CHALLENGE,
    WELL_KNOWN_ROOT_PATH,
    path_suffixed_well_known_path,
    serve_protected_resource_metadata,
)
from memory_manager.auth.provider import MemoryManagerOAuthProvider
from memory_manager.auth.ratelimit import RateLimiter
from memory_manager.auth.shared_state import (
    InMemorySharedState,
    PostgresSharedState,
    SharedState,
    ValkeySharedState,
    build_valkey_shared_state,
)
from memory_manager.auth.verifier import StaticTokenVerifier
from memory_manager.config import ServerConfig, ServerConfigError, canonical_resource_url
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE
from memory_manager.mcp.server import build_server
from memory_manager.observability.logging import RequestIdMiddleware
from memory_manager.observability.metrics import metrics_endpoint

__all__ = ["GracefulShutdownServer", "ServicesFactory", "build_authenticator", "create_app"]

_logger = logging.getLogger(__name__)

HEALTH_PATH = "/healthz"
READY_PATH = "/readyz"
WEBHOOK_PATH = "/hooks/vault"
METRICS_PATH = "/metrics"

_SOURCE_URL = "https://github.com/scramb/memory-manager"
_MAX_WEBHOOK_BODY_BYTES = 1024 * 1024

_GITHUB_SIGNATURE_HEADER = "x-hub-signature-256"
_GITEA_SIGNATURE_HEADER = "x-gitea-signature"

#: The OAuth authorization server's own endpoints (#36), rate-limited by
#: client IP (#39) - there is no bearer token yet at any of these to key on.
_OAUTH_RATE_LIMITED_PATHS = frozenset({"/register", "/token", "/authorize"})

#: The MCP tool names whose calls count against `ServerConfig.write_per_minute`/
#: `write_burst` in addition to the general `mcp_per_minute`/`mcp_burst` limit
#: every `mcp_path` request counts against (#39) - `mcp/server.py`'s five
#: write tools, minus `memory_search`/`memory_read`/`memory_index` (read-only).
_WRITE_TOOL_NAMES = frozenset({"memory_write", "memory_edit", "memory_supersede", "memory_archive"})

ServicesFactory = Callable[[], AbstractAsyncContextManager[Services]]

#: How often the embedded OAuth authorization server's stale state (expired
#: pending authorizations/codes, long-expired tokens, abandoned DCR clients)
#: is swept (`auth.store.cleanup`). Only runs at all while the server is (#36).
_OAUTH_CLEANUP_INTERVAL_SECONDS = 60 * 60


class _OAuthProviderCell:
    """Holds the one `MemoryManagerOAuthProvider` `lifespan` builds, for the `/login`
    route closures below - built before that provider exists, the same reason
    `_McpMount` exists for `mcp_app` (see its own docstring): both are filled in by
    `lifespan`, after the route table referencing them already has to exist."""

    provider: MemoryManagerOAuthProvider | None = None


class _SharedStateHandle:
    """Forwards every `SharedState` call to whatever `backend` is currently set.

    Built once by `create_app` (default `InMemorySharedState` - today's
    single-replica behaviour, in effect until and unless it is swapped) and
    handed, as this one object, to every `RateLimiter` and to `bind_shared_state` on
    a `PasswordAuthenticator`/`OidcAuthenticator` - the same "build now, fill in
    later" shape `_OAuthProviderCell` uses for the OAuth provider: `create_app`
    itself sets `backend` to a `ValkeySharedState` once `config.valkey_url` is set
    (ADR-0009 §2, #104 - does not depend on `services`, so there is no need to wait
    for `lifespan`), and `lifespan` sets it to a `PostgresSharedState` once
    `services.pool` exists and no Valkey backend won already; every holder of this
    handle picks either up on its next call, with nothing re-wired.
    """

    def __init__(self) -> None:
        self.backend: SharedState = InMemorySharedState()

    async def window_hit(self, key: str, *, window_seconds: float) -> tuple[int, float]:
        return await self.backend.window_hit(key, window_seconds=window_seconds)

    async def window_peek(self, key: str, *, window_seconds: float) -> tuple[int, float]:
        return await self.backend.window_peek(key, window_seconds=window_seconds)

    async def put_pending(self, key: str, payload: str, *, ttl_seconds: float) -> None:
        await self.backend.put_pending(key, payload, ttl_seconds=ttl_seconds)

    async def take_pending(self, key: str) -> str | None:
        return await self.backend.take_pending(key)


class GracefulShutdownServer(uvicorn.Server):
    """A `uvicorn.Server` that flips this ASGI app's `draining` flag the instant
    `SIGTERM`/`SIGINT` arrives (ADR-0009 §1/§5), so `/readyz` fails fast and a load
    balancer stops routing new requests here *before* uvicorn's own grace period
    (`Config.timeout_graceful_shutdown`, `ServerConfig.shutdown_grace_seconds`,
    `cli.py`'s `_serve_http`) even starts draining in-flight ones.

    `handle_exit` is overridden as a real method, not a per-instance closure or
    attribute: `sse_starlette` patches `uvicorn.Server.handle_exit` at import time
    (`AppStatus.handle_exit`) and, on signal, finds *the* running server by reading
    `signal.getsignal(signal.SIGTERM).__self__` - `uvicorn.Server.
    install_signal_handlers` registers exactly `self.handle_exit` as the handler, so
    that only resolves to this instance if `handle_exit` stays a bound method on
    this class. `super().handle_exit(sig, frame)` is what still runs uvicorn's own
    `should_exit`/`force_exit` bookkeeping and sse_starlette's patched behaviour
    (draining any open legacy GET stream) - this override only adds the one extra
    side effect, never replaces it.

    Reads the app straight off `self.config.app` instead of a constructor
    parameter: `cli.py` always builds this with the already-constructed `Starlette`
    app (never a string/factory), and `uvicorn.Config.__init__` assigns that value
    to `.app` verbatim, before `.load()` ever runs - so it is already the right
    object the moment this server exists.
    """

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        cast(Starlette, self.config.app).state.draining = True
        super().handle_exit(sig, frame)


def create_app(
    services_factory: ServicesFactory,
    config: ServerConfig,
    *,
    authenticator: Authenticator | None = None,
    cimd_fetcher: ClientMetadataFetcher | None = None,
) -> Starlette:
    """Build the Streamable HTTP ASGI app: MCP, health/ready, the vault webhook.

    `services_factory` is called exactly once, inside this app's `lifespan`
    (e.g. `lambda: open_services(os.environ)`); the `Services` it yields is
    torn down when the app shuts down. Origin validation
    (`config.allowed_origins`) wraps every route below, including
    `/healthz`/`/readyz` - a browser sending a disallowed `Origin` gets the
    same 403 everywhere, not just on the MCP endpoint.

    `authenticator` turns the embedded OAuth authorization server on (#36):
    with one given (and a database configured), `/authorize` parks requests
    that `auth.login`'s `/login` route hands to it, and `/token`/`/register`/
    `/revoke` come from the SDK's own `auth_server_provider` wiring
    (`memory_manager.mcp.server.build_server`); AS metadata is this module's
    own route instead (`auth.metadata`, #38 - see `_authorization_server_metadata`'s
    docstring for why). Without an `authenticator`, this server runs exactly
    as it did before #36: static bearer tokens only, no OAuth routes at all,
    no `authenticator`-shaped login UI to maintain. `build_authenticator` is
    what turns `config.login_mode` (`LOGIN_MODE`) into a real one (ADR-0004's
    L1/L2, #37) - `cli.py`'s `_serve_http` calls it and passes the result in
    here; `tests/auth/test_oauth_flow.py`'s `FakeAuthenticator` is a third,
    test-only implementation of the same `Authenticator` protocol.
    `config.login_mode` set without an `authenticator` given here is a
    startup error (`ServerConfigError`), not a silent no-op - a deployment
    that asked for OAuth login must not end up quietly running
    static-tokens-only instead.

    `cimd_fetcher` is a test seam (#38): given, it is used verbatim instead
    of the production `ClientMetadataFetcher()` `_build_oauth_provider` would
    otherwise build when `config.cimd_enabled` - a test builds one with an
    `httpx.MockTransport` and a fake `Resolver` instead of either touching
    real DNS or opening a real socket. Ignored entirely when
    `config.cimd_enabled` is false.
    """

    # Only needed for pending OIDC login state (`put_pending`); `None` when no
    # `OAUTH_CLIENT_SECRET_KEY` is configured still lets rate limiting work, both
    # `PostgresSharedState.put_pending` and `ValkeySharedState.put_pending` refuse to
    # run without one. Built once, here, rather than separately inside each backend's
    # own branch below.
    cipher = (
        store.ClientSecretCipher(config.oauth_client_secret_key)
        if config.oauth_client_secret_key is not None
        else None
    )

    oauth_cell = _OAuthProviderCell()
    shared_state = _SharedStateHandle()
    if config.valkey_url is not None:
        # Valkey takes precedence over Postgres (ADR-0009 §2, #104) and does not
        # depend on `services.pool`, so it is built right here rather than inside
        # `lifespan` - a missing `redis` package is a startup error before this
        # server ever binds a port, not something discovered only once `lifespan`
        # runs.
        try:
            shared_state.backend = build_valkey_shared_state(config.valkey_url, cipher=cipher)
        except ImportError as exc:
            raise ServerConfigError(
                "VALKEY_URL is set, but the 'redis' package is not installed: install "
                "the 'valkey' extra to use Valkey-backed shared state, e.g. "
                "`uv sync --extra valkey` or `pip install 'memory-manager[valkey]'`"
            ) from exc
    if isinstance(authenticator, (PasswordAuthenticator, OidcAuthenticator)):
        # Bound now, against the handle - not the backend it starts with: `lifespan`
        # below may still swap `shared_state.backend` to a `PostgresSharedState` once
        # `services.pool` exists (unless Valkey already won, just above), and this
        # authenticator (built outside `create_app`, by `build_authenticator`) picks
        # that up on its next call without being touched again.
        authenticator.bind_shared_state(shared_state)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with services_factory() as services:
            if config.valkey_url is None and services.pool is not None:
                # Rate limits, the login brute-force window and pending OIDC login
                # state move to Postgres once a database is configured (ADR-0009 §2,
                # #103) - the same condition `_build_oauth_provider`/`token_verifier`
                # already gate on below. Skipped when a `ValkeySharedState` already
                # won above (#104): the two are never combined.
                shared_state.backend = PostgresSharedState(services.pool, cipher=cipher)

            oauth_provider = _build_oauth_provider(config, services, authenticator, cimd_fetcher)
            oauth_cell.provider = oauth_provider

            token_verifier = (
                StaticTokenVerifier(services.pool)
                if services.pool is not None and oauth_provider is None
                else None
            )
            auth = _build_auth_settings(
                config, token_verifier=token_verifier, oauth_provider=oauth_provider
            )
            app.state.oauth_auth_settings = auth if oauth_provider is not None else None
            # `MCPServer.streamable_http_app` forwards `self.settings.auth`/
            # `self._token_verifier`/`self._auth_server_provider` (set here, at
            # construction) to the lowlevel `Server.streamable_http_app` below -
            # passing them to that call instead would be a no-op, since it reads
            # only its own `self`'s copies.
            mcp = build_server(
                services,
                auth=auth,
                token_verifier=token_verifier,
                auth_server_provider=oauth_provider,
            )
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
            app.state.oauth_authorization_server_enabled = oauth_provider is not None

            cleanup_task = (
                asyncio.create_task(_oauth_cleanup_loop(services.pool))
                if oauth_provider is not None and services.pool is not None
                else None
            )
            try:
                async with mcp_app.router.lifespan_context(mcp_app):
                    yield
            finally:
                if cleanup_task is not None:
                    cleanup_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await cleanup_task
                if isinstance(authenticator, OidcAuthenticator):
                    # Closes the `httpx.AsyncClient` `OidcAuthenticator.__init__` creates
                    # itself when no `http_client` is given (production; tests always
                    # pass their own mocked one, which stays theirs to close) - otherwise
                    # that client, and its connection pool, outlives this app's lifespan.
                    await authenticator.aclose()
                if isinstance(shared_state.backend, ValkeySharedState):
                    # Closes the `redis.asyncio.Redis` connection pool `create_app`
                    # built above - otherwise it outlives this app's lifespan.
                    await shared_state.backend.aclose()

    routes: list[Route | Mount] = [
        Route(HEALTH_PATH, endpoint=_healthz, methods=["GET"]),
        Route(READY_PATH, endpoint=_readyz, methods=["GET"]),
        Route(WEBHOOK_PATH, endpoint=_vault_webhook, methods=["POST"]),
        # Both well-known PRM URLs (#35) are registered here, on the outer
        # app, ahead of the `Mount` below - not left to the SDK's own
        # (resource_name-less, path-suffixed-only) route inside `mcp_app`,
        # see `memory_manager.auth.prm`'s module docstring for why.
        Route(WELL_KNOWN_ROOT_PATH, endpoint=serve_protected_resource_metadata, methods=["GET"]),
        Route(
            path_suffixed_well_known_path(config.mcp_path),
            endpoint=serve_protected_resource_metadata,
            methods=["GET"],
        ),
        # Shadows the SDK's own (CIMD-less) AS metadata route inside `mcp_app`, the
        # same way the two routes above shadow its PRM route - see `auth.metadata`'s
        # module docstring for why.
        Route(OAUTH_METADATA_PATH, endpoint=_authorization_server_metadata, methods=["GET"]),
        Route(METRICS_PATH, endpoint=metrics_endpoint, methods=["GET"]),
    ]
    if authenticator is not None:
        # `/login` only exists when an `Authenticator` is actually configured.
        routes.extend(
            login_routes(
                lookup=_pending_authorization_lookup(oauth_cell),
                complete=_authorization_completer(oauth_cell),
                authenticator=authenticator,
            )
        )
        if isinstance(authenticator, OidcAuthenticator):
            # `{CALLBACK_PATH}` is its own route, not part of `login_routes` - the upstream
            # IdP redirects the browser straight back here, with no `pending` query
            # parameter of its own (`auth.login_oidc`'s module docstring: `state` is what
            # carries the pending authorization across that round trip instead).
            routes.extend(
                oidc_routes(
                    complete=_authorization_completer(oauth_cell), authenticator=authenticator
                )
            )
    routes.append(
        # Mounted last (lowest route-matching precedence), same reasoning
        # `mcp/server/lowlevel/server.py` uses for its own custom routes:
        # the routes above must win their exact paths before this
        # catch-all gets a chance to. `/authorize`/`/token`/`/register`/`/revoke`
        # and the AS metadata document - when `oauth_cell.provider` ends up set -
        # are reachable through here too: the SDK mounts them directly on
        # `mcp_app`, the same Starlette app `_McpMount` forwards every other
        # request to (confirmed by reading `mcp/server/lowlevel/server.py`'s
        # `streamable_http_app`).
        Mount("/", app=_McpMount())
    )
    middleware = [
        # Outermost: every response, including a 429/413, carries a request id.
        Middleware(RequestIdMiddleware),
        # Outermost: reject an over-limit or oversized request before
        # Origin validation, routing or auth ever run (#39).
        Middleware(
            _LimitsMiddleware,
            mcp_path=config.mcp_path,
            webhook_path=WEBHOOK_PATH,
            max_request_bytes=config.max_request_bytes,
            mcp_limiter=RateLimiter(
                state=shared_state, per_minute=config.mcp_per_minute, burst=config.mcp_burst
            ),
            write_limiter=RateLimiter(
                state=shared_state, per_minute=config.write_per_minute, burst=config.write_burst
            ),
            oauth_limiter=RateLimiter(
                state=shared_state, per_minute=config.oauth_per_minute, burst=config.oauth_burst
            ),
            webhook_limiter=RateLimiter(
                state=shared_state,
                per_minute=config.webhook_per_minute,
                burst=config.webhook_burst,
            ),
        ),
        Middleware(_OriginValidationMiddleware, allowed_origins=config.allowed_origins),
        # Wraps the whole app, including the `Mount` below, so it sees the
        # `WWW-Authenticate` header `mcp_app`'s own `RequireAuthMiddleware`/
        # `BearerAuthBackend` build on a 401 (see `memory_manager.auth.prm`'s
        # module docstring) just as much as it would see one from a route
        # defined directly on this outer app.
        Middleware(_ScopeChallengeMiddleware, scope=SCOPE_CHALLENGE),
    ]
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    # Known synchronously (no vault/DB work needed), unlike `services`/`mcp_app`
    # above - set right away rather than deferred into `lifespan`.
    app.state.config = config
    # Flipped to `True` by `GracefulShutdownServer.handle_exit` on `SIGTERM`/
    # `SIGINT` (ADR-0009 §5) - `_readyz` checks this before anything else.
    app.state.draining = False
    return app


def _build_oauth_provider(
    config: ServerConfig,
    services: Services,
    authenticator: Authenticator | None,
    cimd_fetcher: ClientMetadataFetcher | None,
) -> MemoryManagerOAuthProvider | None:
    """The embedded OAuth authorization server for this `services`, or `None` to run
    without one.

    Raises `ServerConfigError` if `config.login_mode` is set but no `authenticator` was
    given - see `create_app`'s docstring for why that is a startup error, not a silent
    fallback to static tokens.
    """
    if services.pool is None or authenticator is None:
        if services.pool is not None and config.login_mode is not None:
            raise ServerConfigError(
                f"LOGIN_MODE={config.login_mode!r} is set, but create_app was not given an "
                "authenticator (build_authenticator(config, environ) builds one from "
                "LOGIN_MODE - cli.py's _serve_http is expected to call it); unset LOGIN_MODE "
                "to run this server with static tokens only"
            )
        return None

    if config.oauth_client_secret_key is None:
        raise ServerConfigError(
            "OAUTH_CLIENT_SECRET_KEY is required once the OAuth authorization server is "
            "enabled: a DCR client's client_secret is encrypted with it at rest "
            "(auth.store.ClientSecretCipher) - generate one with "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )

    resource = config.resource_url()
    issuer = canonical_resource_url(cast(str, config.public_url), "")
    # `cimd_fetcher` (the test seam) is only ever honoured while CIMD is actually on -
    # `config.cimd_enabled=False` means "no CIMD, whatever was passed in".
    resolved_cimd_fetcher = None
    if config.cimd_enabled:
        resolved_cimd_fetcher = (
            cimd_fetcher if cimd_fetcher is not None else ClientMetadataFetcher()
        )
    return MemoryManagerOAuthProvider(
        services.pool,
        resource=resource,
        issuer=issuer,
        client_secret_key=config.oauth_client_secret_key,
        cimd_fetcher=resolved_cimd_fetcher,
    )


def build_authenticator(config: ServerConfig, environ: Mapping[str, str]) -> Authenticator | None:
    """The production `Authenticator` for `config.login_mode` (ADR-0004 L1/L2, #37) -
    `None` if `login_mode` is unset (no OAuth authorization server at all).

    `cli.py`'s `_serve_http` calls this and passes the result into `create_app` as
    `authenticator`; `PasswordAuthenticator.from_env`/`OidcAuthenticator.from_env` raise
    `ServerConfigError` for a missing or inconsistent `LOGIN_MODE=password`/`oidc`
    configuration, which `cli.py`'s `_serve` already turns into a clean startup refusal.
    """
    if config.login_mode is None:
        return None
    if config.login_mode == "password":
        return PasswordAuthenticator.from_env(environ)
    if config.login_mode == "oidc":
        return OidcAuthenticator.from_env(environ)
    raise ServerConfigError(f"LOGIN_MODE must be 'password' or 'oidc', got {config.login_mode!r}")


def _build_auth_settings(
    config: ServerConfig,
    *,
    token_verifier: StaticTokenVerifier | None,
    oauth_provider: MemoryManagerOAuthProvider | None,
) -> AuthSettings | None:
    if oauth_provider is not None:
        return AuthSettings(
            issuer_url=oauth_provider.issuer,  # type: ignore[arg-type]
            resource_server_url=oauth_provider.resource,  # type: ignore[arg-type]
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[READ_SCOPE, WRITE_SCOPE],
                default_scopes=[READ_SCOPE, WRITE_SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=True),
            # `auth.verifier.verify_bearer_token` enforces the RFC 8707 audience itself
            # (ADR-0004: audience ENFORCED) - this SDK flag would also reject every static
            # token merged in alongside OAuth ones, which carries no `resource` at all.
            validate_token_resource=False,
        )
    if token_verifier is not None:
        return AuthSettings(
            issuer_url=config.resource_url(),  # type: ignore[arg-type]
            resource_server_url=config.resource_url(),  # type: ignore[arg-type]
            # Static tokens carry no RFC 8707 resource indicator of their
            # own (#34 is bearer tokens only, no OAuth flow to bind one
            # with yet) - checking it would reject every one of them.
            validate_token_resource=False,
        )
    return None


def _pending_authorization_lookup(cell: _OAuthProviderCell) -> PendingAuthorizationLookup:
    async def lookup(pending_id: str) -> PendingAuthorization | None:
        provider = _require_provider(cell)
        return await provider.pending_authorization(pending_id)

    return lookup


def _authorization_completer(cell: _OAuthProviderCell) -> AuthorizationCompleter:
    async def complete(pending_id: str, subject: str, namespaces: Sequence[str]) -> str | None:
        provider = _require_provider(cell)
        return await provider.complete_authorization(pending_id, subject, list(namespaces))

    return complete


def _require_provider(cell: _OAuthProviderCell) -> MemoryManagerOAuthProvider:
    """`cell.provider`, narrowed - `/login` routes only ever exist (`create_app`) once
    `_build_oauth_provider` actually produced one, so `None` here would be this module's
    own wiring bug, not a client-facing condition."""
    if cell.provider is None:  # pragma: no cover - defensive
        raise RuntimeError("the /login route was mounted without an OAuth provider configured")
    return cell.provider


async def _oauth_cleanup_loop(pool: asyncpg.Pool) -> None:
    """Sweep `auth.store`'s stale OAuth state every `_OAUTH_CLEANUP_INTERVAL_SECONDS`.

    Runs for the lifetime of the app (cancelled by `create_app`'s `lifespan` on shutdown);
    a failed sweep is logged and retried next interval, never allowed to crash the server.
    """
    while True:
        await asyncio.sleep(_OAUTH_CLEANUP_INTERVAL_SECONDS)
        try:
            stats = await store.cleanup(pool)
        except Exception:
            _logger.exception("oauth cleanup run failed")
            continue
        _logger.info(
            "oauth cleanup: pending=%d codes=%d tokens=%d clients=%d",
            stats.pending,
            stats.codes,
            stats.tokens,
            stats.clients,
        )


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


class _LimitsMiddleware:
    """Body-size cap plus per-key fixed-window rate limiting on a shared backend
    (#39, #103).

    Three route classes, matched on `scope["path"]` directly (the same
    style `_OriginValidationMiddleware` below uses, not a Starlette
    `Route` lookup - this runs ahead of routing):

    - `mcp_path`: capped at `max_request_bytes`, counted against actual
      bytes received as they stream in (`_buffer_capped_body`) - never
      trusted from a `Content-Length` header alone, so a chunked request
      with no such header is capped just the same. Counts against
      `mcp_limiter`, keyed by the hashed bearer token (`_request_key`) if
      one is present, the client IP otherwise. A `tools/call` body naming
      one of `_WRITE_TOOL_NAMES` additionally counts against
      `write_limiter`, same key - the tighter per-write limit CLAUDE.md's
      guardrails call for on top of the general per-token one.
    - `webhook_path`: no body cap of its own here (`_vault_webhook` already
      enforces its own 1 MiB cap); counts against `webhook_limiter`, keyed
      by client IP.
    - `/register`/`/token`/`/authorize` (`_OAUTH_RATE_LIMITED_PATHS`):
      counts against `oauth_limiter`, keyed by client IP - none of these
      carry a bearer token of their own to key on instead.

    Every other path (health/ready, PRM, login, the AS metadata document)
    passes through unlimited; none of them is a meaningful target for
    either abuse this middleware defends against.

    Every limiter call goes through `_allow`, which fails *open* (allows the
    request, logs a warning) if the shared `SharedState` backend raises -
    once a database or `VALKEY_URL` is configured (`create_app`), a limiter
    check is a Postgres or Valkey round trip, and a transient outage of
    either must never itself turn into "every request gets rejected" on top
    of whatever else that outage already breaks.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        mcp_path: str,
        webhook_path: str,
        max_request_bytes: int,
        mcp_limiter: RateLimiter,
        write_limiter: RateLimiter,
        oauth_limiter: RateLimiter,
        webhook_limiter: RateLimiter,
    ) -> None:
        self._app = app
        self._mcp_path = mcp_path
        self._webhook_path = webhook_path
        self._max_request_bytes = max_request_bytes
        self._mcp_limiter = mcp_limiter
        self._write_limiter = write_limiter
        self._oauth_limiter = oauth_limiter
        self._webhook_limiter = webhook_limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = scope["path"]
        if path == self._mcp_path:
            await self._handle_mcp(scope, receive, send)
            return
        if path == self._webhook_path:
            await self._handle_keyed(scope, receive, send, self._webhook_limiter, _client_ip_key)
            return
        if path in _OAUTH_RATE_LIMITED_PATHS:
            await self._handle_keyed(scope, receive, send, self._oauth_limiter, _client_ip_key)
            return
        await self._app(scope, receive, send)

    async def _handle_keyed(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        limiter: RateLimiter,
        key_of: Callable[[Scope], str],
    ) -> None:
        allowed, retry_after = await _allow_or_fail_open(limiter, key_of(scope))
        if not allowed:
            await _send_rate_limited(scope, receive, send, retry_after)
            return
        await self._app(scope, receive, send)

    async def _handle_mcp(self, scope: Scope, receive: Receive, send: Send) -> None:
        key = _request_key(scope)

        if scope.get("method") == "POST":
            body, oversized = await _buffer_capped_body(receive, self._max_request_bytes)
            if oversized:
                await _send_payload_too_large(scope, receive, send)
                return
            effective_receive = _replaying_receive(body, receive)
        else:
            # A GET (SSE stream resumption) or DELETE (session end) carries
            # no body worth capping or inspecting for a write-tool call.
            body = b""
            effective_receive = receive

        allowed, retry_after = await _allow_or_fail_open(self._mcp_limiter, key)
        if not allowed:
            await _send_rate_limited(scope, receive, send, retry_after)
            return

        if _is_write_tool_call(body):
            allowed, retry_after = await _allow_or_fail_open(self._write_limiter, key)
            if not allowed:
                await _send_rate_limited(scope, receive, send, retry_after)
                return

        await self._app(scope, effective_receive, send)


async def _allow_or_fail_open(limiter: RateLimiter, key: str) -> tuple[bool, float]:
    """`limiter.allow(key)`, but `(True, 0.0)` instead of raising if the underlying
    `SharedState` backend fails (e.g. Postgres unreachable) - see `_LimitsMiddleware`'s
    docstring for why failing open, not closed, is the right default here."""
    try:
        return await limiter.allow(key)
    except Exception:
        _logger.warning(
            "rate-limit backend unavailable; failing open for key=%r", key, exc_info=True
        )
        return True, 0.0


def _client_ip(scope: Scope) -> str:
    """`scope["client"]`'s host, or `"unknown"` for a transport that carries none.

    This is what uvicorn's own `ProxyHeadersMiddleware` has already
    rewritten by the time a request reaches this ASGI app, when the
    operator both passed `proxy_headers=True` and listed the actual proxy
    in `forwarded_allow_ips` (`ServerConfig.forwarded_allow_ips`'s
    docstring) - nothing here re-reads `X-Forwarded-For` itself.
    """
    client = scope.get("client")
    if not client:
        return "unknown"
    return str(client[0])


def _client_ip_key(scope: Scope) -> str:
    return f"ip:{_client_ip(scope)}"


def _request_key(scope: Scope) -> str:
    """The rate-limit key for an `mcp_path` request: a hashed bearer token, or the
    client IP if the request carries none (unauthenticated loopback mode, #33).

    Hashed, never the raw token - CLAUDE.md: "token hashes only" is a rule for
    anything this server keeps around, including in-memory rate-limit state.
    """
    authorization = Headers(scope=scope).get("authorization")
    if authorization is not None and authorization.lower().startswith("bearer "):
        token = authorization[len("Bearer ") :]
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return f"token:{digest}"
    return _client_ip_key(scope)


async def _buffer_capped_body(receive: Receive, limit: int) -> tuple[bytes, bool]:
    """Every `http.request` chunk `receive` yields, or `(b"", True)` the moment their
    total exceeds `limit`.

    Counts actual bytes received, not `Content-Length` - a chunked request
    with no such header is capped exactly the same way.
    """
    chunks: list[bytes] = []
    total = 0
    more_body = True
    while more_body:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk = cast(bytes, message.get("body", b""))
        total += len(chunk)
        if total > limit:
            return b"", True
        chunks.append(chunk)
        more_body = bool(message.get("more_body", False))
    return b"".join(chunks), False


def _replaying_receive(body: bytes, original_receive: Receive) -> Receive:
    """A `Receive` that yields `body` once, as a single message, then falls back to
    `original_receive` - so the request `_buffer_capped_body` already drained in full
    still reads normally for whatever handles it next (`mcp_app`/`_vault_webhook`)."""
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await original_receive()

    return receive


def _is_write_tool_call(body: bytes) -> bool:
    """Whether `body` is (or, batched, contains) a JSON-RPC `tools/call` naming one of
    `_WRITE_TOOL_NAMES` - best-effort: anything that fails to parse as JSON, or does
    not look like a tool call at all, is "no", never an error of its own (the MCP
    endpoint itself is what validates the request body)."""
    if not body:
        return False
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    messages = payload if isinstance(payload, list) else [payload]
    for message in messages:
        if not isinstance(message, dict) or message.get("method") != "tools/call":
            continue
        params = message.get("params")
        if isinstance(params, dict) and params.get("name") in _WRITE_TOOL_NAMES:
            return True
    return False


async def _send_rate_limited(
    scope: Scope, receive: Receive, send: Send, retry_after: float
) -> None:
    seconds = max(1, math.ceil(retry_after)) if math.isfinite(retry_after) else 1
    response = PlainTextResponse(
        "rate limit exceeded", status_code=429, headers={"Retry-After": str(seconds)}
    )
    await response(scope, receive, send)


async def _send_payload_too_large(scope: Scope, receive: Receive, send: Send) -> None:
    response = PlainTextResponse("payload too large", status_code=413)
    await response(scope, receive, send)


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


_WWW_AUTHENTICATE_HEADER = "www-authenticate"


class _ScopeChallengeMiddleware:
    """Appends `scope="..."` to a `WWW-Authenticate: Bearer ...` 401 response header.

    `mcp_app`'s own `RequireAuthMiddleware`/`BearerAuthBackend`
    (`mcp.server.auth.middleware.bearer_auth`, read directly, see
    `memory_manager.auth.prm`'s module docstring) build that header without
    a `scope` parameter at all and give no hook to add one at the source -
    so this rewrites it in flight instead, the same ASGI
    `send`-message-rewriting idiom Starlette's own middleware use for
    response headers. Only ever touches a response that already carries
    `WWW-Authenticate` and is missing `scope=`; every other response,
    including this app's own 401s that carry no such header at all (the
    vault webhook's), passes through unchanged.
    """

    def __init__(self, app: ASGIApp, *, scope: str) -> None:
        self._app = app
        self._scope = scope

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_scope(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                challenge = headers.get(_WWW_AUTHENTICATE_HEADER)
                if challenge is not None and "scope=" not in challenge:
                    headers[_WWW_AUTHENTICATE_HEADER] = f'{challenge}, scope="{self._scope}"'
            await send(message)

        await self._app(scope, receive, send_with_scope)


async def _authorization_server_metadata(request: Request) -> Response:
    """GET handler for `auth.metadata.WELL_KNOWN_PATH`, shadowing the SDK's own
    (CIMD-less) route on `mcp_app` underneath (see that module's docstring for why).

    404 under the exact same condition the SDK's own route would not exist at all:
    no OAuth authorization server configured (`lifespan` only sets
    `app.state.oauth_auth_settings` when `oauth_provider is not None`).
    """
    auth: AuthSettings | None = request.app.state.oauth_auth_settings
    if auth is None:
        return PlainTextResponse("not found", status_code=404)

    config: ServerConfig = request.app.state.config
    metadata = build_authorization_server_metadata(auth, cimd_enabled=config.cimd_enabled)
    return PydanticJSONResponse(metadata, headers={"Cache-Control": "public, max-age=3600"})


async def _healthz(_request: Request) -> Response:
    """ADR-0002 §13: a modified deployment's `/healthz` must point back at its source."""
    return JSONResponse(
        {"status": "ok", "version": __version__, "commit": __commit__, "source": _SOURCE_URL}
    )


async def _readyz(request: Request) -> Response:
    """503 when draining (ADR-0009 §5), the vault clone is missing, or a configured
    database is unreachable.

    The draining check runs first and skips the database round trip entirely -
    once `GracefulShutdownServer.handle_exit` has set `app.state.draining`, this
    process is shutting down regardless of what the database says, so there is
    nothing to gain from asking it.
    """
    if request.app.state.draining:
        return JSONResponse({"ready": False, "draining": True}, status_code=503)

    services: Services = request.app.state.services

    database_ready = True
    if services.pool is not None:
        try:
            await services.pool.fetchval("select 1")
        except Exception:
            # Any failure here means "not ready", not a crash of the endpoint itself.
            database_ready = False

    if services.vault_root is not None:
        vault_ready = (services.vault_root / ".git").is_dir()
    else:
        # The `postgres` backend (ADR-0007 §2, WP-18) has no vault clone to
        # check at all - "vault ready" collapses to "the database holding
        # every note is reachable", `database_ready` above.
        vault_ready = database_ready

    ready = vault_ready and database_ready
    body = {
        "ready": ready,
        "vault": vault_ready,
        "database": database_ready,
        "draining": False,
    }
    return JSONResponse(body, status_code=200 if ready else 503)


async def _vault_webhook(request: Request) -> Response:
    """Verify a GitHub/Gitea push webhook's HMAC signature and trigger a sync.

    404 when no `VAULT_WEBHOOK_SECRET` is configured at all, or when
    `Services.trigger_sync` is `None` - the `postgres` backend (ADR-0007
    §2, WP-18) has no vault and nothing a webhook could ever resync, so the
    endpoint does not exist there either (the same "does not exist, rather
    than existing just to reject every call" reasoning as the missing
    secret); 401 for a missing or wrong signature; 413 over the 1 MiB body
    cap; 202 once a verified webhook has triggered `Services.trigger_sync()`
    - deliberately *after* the sync has run, not fire-and-forget, so the
    response means what it says: the vault is caught up by the time the
    caller sees it.
    """
    config: ServerConfig = request.app.state.config
    secret = config.webhook_secret
    if secret is None:
        return PlainTextResponse("not found", status_code=404)

    services: Services = request.app.state.services
    if services.trigger_sync is None:
        return PlainTextResponse("not found", status_code=404)

    body = await _read_capped_body(request, _MAX_WEBHOOK_BODY_BYTES)
    if body is None:
        return PlainTextResponse("payload too large", status_code=413)

    if not _verify_webhook_signature(request.headers, body, secret):
        _logger.warning("rejected a %s request with an invalid or missing signature", WEBHOOK_PATH)
        return PlainTextResponse("invalid signature", status_code=401)

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
