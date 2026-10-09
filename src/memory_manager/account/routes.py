# SPDX-License-Identifier: AGPL-3.0-only
"""The `/account` page shell and its login/logout routes (#229, ADR-0008
addendum 2026-10-08, ADR-0012).

Login is not a fourth `Authenticator` implementation: `/account/login`
(`_start_login`) parks a pending login attempt of its own (`account.pending`,
a third `oauth_pending.kind`) and redirects to `auth.login.LOGIN_PATH`
(`/login`) with it - the exact same URL shape `auth.provider.
MemoryManagerOAuthProvider.authorize` already builds for an MCP client's
`/authorize` call. From there, the *existing* `/login` route
(`http.py`'s `login_routes(...)`, and - for `oidc`/`entra` - its shared
`{CALLBACK_PATH}`) handles both kinds of pending login identically: `http.py`
composes the `PendingAuthorizationLookup`/`AuthorizationCompleter` this
module exports (`pending_authorization_lookup`/`authorization_completer`)
with the OAuth provider's own ones, trying the OAuth-shaped (`kind =
'authorize'`) lookup first and this module's (`kind = 'account_login'`)
second - a `pending_id` is never valid in both tables, so there is no
ambiguity, and the three real `Authenticator`s (`PasswordAuthenticator`,
`OidcAuthenticator`, `EntraAuthenticator`) run completely unchanged: none of
them know an account login is even happening.

The one thing an `AuthorizationCompleter` cannot do - set a cookie on the
eventual response - is bridged with a `ContextVar` (`_pending_session_id`):
`authorization_completer`'s own `complete` sets it, as a side effect, to the
plaintext session id `account.sessions.create` just minted;
`with_session_cookie` (applied by `http.py` to the Route objects
`login_routes`/`oidc_routes`/`entra_routes` return) reads it back right
after the wrapped endpoint returns and, if set, attaches the `Set-Cookie`
header ADR-0008's addendum specifies (`HttpOnly`, `Secure`, `SameSite=Strict`,
`Path=/account`) to the `RedirectResponse` those endpoints already built -
without either of them, or `auth.login`, ever being told a cookie exists.
Safe across concurrent requests: Starlette runs each request in its own
`asyncio` task, so a `ContextVar` set inside one request's `complete` call is
never visible to another request's.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from typing import cast

import asyncpg
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from memory_manager.account import admin as account_admin
from memory_manager.account import break_glass as account_break_glass
from memory_manager.account import break_glass_viewer as account_break_glass_viewer
from memory_manager.account import delete as account_delete
from memory_manager.account import export as account_export
from memory_manager.account import pending as account_pending
from memory_manager.account import sessions
from memory_manager.account.sections import DEFAULT_SECTIONS, SectionContext, render_sections
from memory_manager.account.templates import CSRF_FIELD_NAME, account_page, account_response
from memory_manager.app import Services
from memory_manager.auth.login import (
    LOGIN_PATH,
    PENDING_PARAM,
    AuthorizationCompleter,
    LoginPrincipal,
    PendingAuthorization,
    PendingAuthorizationLookup,
)
from memory_manager.storage.postgres import PostgresBackend

__all__ = [
    "LOGIN_PATH_START",
    "LOGOUT_PATH",
    "PATH",
    "SESSION_COOKIE",
    "PoolCell",
    "authorization_completer",
    "page_routes",
    "pending_authorization_lookup",
    "with_session_cookie",
]

PATH = "/account"
LOGIN_PATH_START = "/account/login"
LOGOUT_PATH = "/account/logout"

#: `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/account` (ADR-0008 addendum
#: 2026-10-08) - set once, by `with_session_cookie`, never by a route handler
#: directly (see the module docstring for why that cannot happen inline).
SESSION_COOKIE = "mm_session"

#: What `account.pending.pending_authorization` shows as the "you will be
#: redirected to ..." destination (`auth.templates._redirect_notice`) for the
#: `password`/`oidc`/`entra` pages - a relative path renders as plain text there
#: rather than a host, which is accurate: there is no third-party redirect at all.
_DISPLAY_REDIRECT_URI = PATH

#: The `account.sessions.csrf_token`/`verify_csrf` form label for the one state-
#: changing form this page has today (`POST /account/logout`) - distinct from
#: `account.templates.CSRF_FIELD_NAME`, the HTML field the token travels in.
_CSRF_FORM_LOGOUT = "account-logout"


class PoolCell:
    """Holds the pool `authorization_completer`/`pending_authorization_lookup`'s
    closures read on every call - built before `services.pool` exists, filled in by
    `http.py`'s `lifespan`, the same "build now, fill in later" shape
    `_OAuthProviderCell` uses for the OAuth provider (that class's own docstring)."""

    pool: asyncpg.Pool | None = None


_pending_session_id: ContextVar[str | None] = ContextVar(
    "_account_pending_session_id", default=None
)


def pending_authorization_lookup(cell: PoolCell) -> PendingAuthorizationLookup:
    """A `PendingAuthorizationLookup` for an account-login `pending_id` - `None` if
    `cell.pool` is not set yet, or the id is unknown/expired/already completed.

    `http.py` tries the OAuth provider's own lookup first and this one second - see
    the module docstring for why a `pending_id` is never ambiguous between the two.
    """

    async def lookup(pending_id: str) -> PendingAuthorization | None:
        pool = cell.pool
        if pool is None:
            return None
        if not await account_pending.pending_exists(pool, pending_id):
            return None
        return PendingAuthorization(
            id=pending_id,
            client_id="account",
            client_name="your account",
            scopes=(),
            resource=None,
            redirect_uri=_DISPLAY_REDIRECT_URI,
        )

    return lookup


def authorization_completer(cell: PoolCell, *, login_mode: str) -> AuthorizationCompleter:
    """An `AuthorizationCompleter` that turns a completed account login into a new
    browser session, instead of an OAuth authorization code.

    `login_mode` is fixed per deployment (`LOGIN_MODE`, `http.py`'s own
    `isinstance(authenticator, ...)` switch at mount time) - never read from
    `principal`, which is `None` for `password`/`oidc` either way. Returns `PATH`
    (`/account`) on success, the same not-a-client-redirect-uri shape `pending_
    authorization_lookup` already shows during the login itself.
    """

    async def complete(
        pending_id: str,
        subject: str,
        namespaces: Sequence[str],
        principal: LoginPrincipal | None = None,
    ) -> str | None:
        # `namespaces` unused: an account session carries no namespace grant at all -
        # kept named to match `AuthorizationCompleter.__call__`'s own signature exactly
        # (mypy checks callback-protocol parameter names, not just positions/types).
        _ = namespaces
        pool = cell.pool
        if pool is None:
            return None
        if not await account_pending.complete_pending(pool, pending_id):
            return None
        session_id = await sessions.create(
            pool,
            subject=subject,
            login_mode=login_mode,
            oid=principal.oid if principal is not None else None,
            roles=principal.roles if principal is not None else (),
        )
        _pending_session_id.set(session_id)
        return PATH

    return complete


def with_session_cookie(routes: Sequence[Route]) -> list[Route]:
    """Wrap every `Route` in `routes` so a session `authorization_completer` minted
    during the request (via `_pending_session_id`) ends up as a `Set-Cookie` header on
    the response those routes already built - see the module docstring for why this
    cannot happen inside `authorization_completer` itself."""
    wrapped: list[Route] = []
    for route in routes:
        inner = cast(Callable[[Request], Awaitable[Response]], route.endpoint)

        async def endpoint(
            request: Request, _inner: Callable[[Request], Awaitable[Response]] = inner
        ) -> Response:
            token = _pending_session_id.set(None)
            try:
                response = await _inner(request)
                session_id = _pending_session_id.get()
                if session_id is not None:
                    response.set_cookie(
                        SESSION_COOKIE,
                        session_id,
                        path=PATH,
                        httponly=True,
                        secure=True,
                        samesite="strict",
                    )
                return response
            finally:
                _pending_session_id.reset(token)

        wrapped.append(Route(route.path, endpoint=endpoint, methods=list(route.methods or [])))
    return wrapped


def _require_pool(request: Request) -> asyncpg.Pool | None:
    services: Services = request.app.state.services
    return services.pool


async def _start_login(request: Request) -> Response:
    pool = _require_pool(request)
    if pool is None:  # pragma: no cover - defensive, see account.sessions' own pool requirement
        return PlainTextResponse("Account login requires a database.", status_code=503)
    pending_id = await account_pending.create_pending(pool)
    return RedirectResponse(f"{LOGIN_PATH}?{PENDING_PARAM}={pending_id}", status_code=302)


async def _account_page(request: Request) -> Response:
    pool = _require_pool(request)
    if pool is None:  # pragma: no cover - defensive, see account.sessions' own pool requirement
        return PlainTextResponse("The account page requires a database.", status_code=503)
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id is None:
        return RedirectResponse(LOGIN_PATH_START, status_code=302)
    info = await sessions.lookup(pool, session_id)
    if info is None:
        return RedirectResponse(LOGIN_PATH_START, status_code=302)

    services: Services = request.app.state.services
    ctx = SectionContext(
        session=info,
        pool=pool,
        is_postgres_backend=isinstance(services.storage, PostgresBackend),
        app_role=services.app_role,
        session_id=session_id,
    )
    sections_html = await render_sections(ctx, DEFAULT_SECTIONS)
    csrf = sessions.csrf_token(session_id, _CSRF_FORM_LOGOUT)
    body = account_page(sections_html=sections_html, logout_path=LOGOUT_PATH, csrf_token=csrf)
    return account_response(body)


async def _logout(request: Request) -> Response:
    pool = _require_pool(request)
    if pool is None:  # pragma: no cover - defensive, see account.sessions' own pool requirement
        return PlainTextResponse("Logout requires a database.", status_code=503)
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id is None:
        return PlainTextResponse("No active session.", status_code=403)

    form = await request.form()
    token = str(form.get(CSRF_FIELD_NAME, ""))
    if not sessions.verify_csrf(session_id, _CSRF_FORM_LOGOUT, token):
        return PlainTextResponse("Invalid or missing CSRF token.", status_code=403)

    await sessions.revoke(pool, session_id)
    response = RedirectResponse(LOGIN_PATH_START, status_code=302)
    response.delete_cookie(SESSION_COOKIE, path=PATH, secure=True, httponly=True, samesite="strict")
    return response


def page_routes() -> list[Route]:
    """`GET /account`, `GET /account/login`, `POST /account/logout`, `POST
    /account/export`, `POST /account/delete`, plus every `POST /account/admin/...`
    route (`account.admin.admin_routes`), plus every `POST /account/admin/
    break-glass/...` route (`account.break_glass.break_glass_routes`), plus
    the two `GET /account/admin/break-glass/view...` routes (`account.
    break_glass_viewer.break_glass_viewer_routes`, #238) - mounted by
    `http.py` whenever an `Authenticator` is configured, the same
    condition `/login` itself is mounted under (an account with no login
    method configured at all makes no sense). None of these carry a session
    cookie of their own to set (each only ever reads or clears one); only
    the shared `/login`/`{CALLBACK_PATH}` routes need `with_session_cookie`.
    `account.export.export_routes`/`account.delete.delete_routes`/
    `account.admin.admin_routes`/`account.break_glass.break_glass_routes`/
    `account.break_glass_viewer.break_glass_viewer_routes` take
    `SESSION_COOKIE` as a parameter rather than importing it directly,
    so those modules stay free to be imported here without a cycle back
    (their own docstrings).
    """
    return [
        Route(PATH, endpoint=_account_page, methods=["GET"]),
        Route(LOGIN_PATH_START, endpoint=_start_login, methods=["GET"]),
        Route(LOGOUT_PATH, endpoint=_logout, methods=["POST"]),
        *account_export.export_routes(SESSION_COOKIE),
        *account_delete.delete_routes(SESSION_COOKIE),
        *account_admin.admin_routes(SESSION_COOKIE),
        *account_break_glass.break_glass_routes(SESSION_COOKIE),
        *account_break_glass_viewer.break_glass_viewer_routes(SESSION_COOKIE),
    ]
