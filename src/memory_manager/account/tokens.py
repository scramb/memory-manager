# SPDX-License-Identifier: AGPL-3.0-only
"""Self-service personal tokens on `/account` (ADR-0012, #135).

A signed-in user creates, lists and revokes their own `kind="personal"`
static bearer tokens (`auth.tokens`, ADR-0012, #134) - the self-service half
that ADR-0012 option A describes, on top of the owner-bound verification
`auth.owner_rights`/`auth.verifier` already enforce on every request. The
section is visible whenever the embedded authorization server runs at all
(`account.sections._tokens_enabled`: `not is_postgres_backend or
session.oid is not None`) - wider than every other enterprise-only
`/account` section (`account.export`/`account.delete`/`account.admin`'s own
`is_postgres_backend and session.oid is not None` gate), since a
`"git"`-backend `password`/`oidc` session has no personal namespace but can
still hold a personal token (ADR-0012's own "Client Integrations" use case:
a headless CLI or an Open WebUI filter needs a bearer token regardless of
backend). A `"postgres"`-backend session with no `oid` (there is none - every
`Authenticator` that can reach `STORAGE_BACKEND=postgres` sets one) is the
one case this still excludes, same reasoning `account.admin`'s own
`_admin_identity` gives for an analogous defensive check.

The owner a token is created for and bound to is the signed-in session's own
identity, never a form field: `session.oid` if set (`"entra"` login, both
backends), `session.subject` otherwise (`"password"`/`"oidc"` on `"git"`) -
`_owner_id` below. `roles` follows the same split: `session.roles` for
`"entra"` (already validated at login, ADR-0006 §3 - a user without a memory
role is denied there), or a fixed `("Memory.User",)` for `"password"`/
`"oidc"` (`_PERSONAL_ROLE_GIT` - `auth.tokens.create_token`'s own
`_validate_owner_and_roles` requires at least one role for any owner at all,
and roles have no effect in `"git"` mode's RLS-less world regardless of
which one is picked).

Namespaces: for `"git"`, the owner's own current namespaces
(`services.owner_rights_resolver.resolve(owner).namespaces`, the same live
lookup `auth.owner_rights` makes on every later verification) - the create
route re-runs the identical `intersect_namespaces` subset check `cli.py`'s
own `token create` already makes for a `"git"`-backend personal token, so a
submitted namespace outside that set is rejected with `400` before any
database write. For `"postgres"`, there is no per-token namespace narrowing
at all (#135's own "Nicht dabei"): every personal token is created with
`namespaces=(ALL_NAMESPACES,)` unconditionally - the real narrowing happens
inside Postgres itself, through row-level security reading `app.oid` live on
every query (`auth.owner_rights`'s own module docstring makes the identical
point for `OwnerRights.namespaces` there).

Scopes are the two fixed literals `READ_SCOPE`/`WRITE_SCOPE`
(`auth.scopes`) - a submitted set must be non-empty and drawn only from
those two; nothing in either backend narrows which scopes an owner may use
(`auth.owner_rights`'s own docstring: "no per-principal scope configuration
anywhere in this codebase today").

Expiry is mandatory (ADR-0012: a personal token is useless without one) and
bounded by `services.personal_token_max_days`, further bounded by
`services.static_token_max_days` too once `STORAGE_BACKEND=postgres`
(`_effective_max_days` - `auth.tokens.create_token`'s own docstring: "the
effective maximum ... is whichever of the two is smaller").

The token's `name` is generated here, never taken from the form
(`_generate_name`): a user-chosen label would show up in `static_tokens`'
`UNIQUE(name)` violation message to anyone who guessed another user's choice,
leaking that it exists. The free-text label a user actually wants travels as
`description` instead, which this section's own listing is the only place
that is ever shown back.

The plaintext is returned exactly once, in the create route's own response
body (`account.templates.token_created_page`) - never a redirect, never
logged, never part of the audit row `auth.tokens.create_token` already
writes (that row's own `detail` carries only `name`/`kind`). Revoke is
scoped to the caller's own personal tokens in SQL
(`auth.tokens.revoke_token(..., owner_oid=..., kind=KIND_PERSONAL)`) - a
foreign or unknown token name revokes nothing and reports the same `404` as
one that never existed at all, never a `403` that would confirm someone
else's token name is valid.

Every mutation's audit row is the one `auth.tokens.create_token`/
`revoke_token` already write internally (`op="token_create"`/
`"token_revoke"`, `actor` = the owner) - this module writes no audit row of
its own, unlike `account.admin`/`account.break_glass`, whose own SQL
functions carry no audit trail at all.
"""

from __future__ import annotations

import html
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from memory_manager.account import sessions
from memory_manager.account.sessions import SessionInfo
from memory_manager.account.templates import (
    CSRF_FIELD_NAME,
    account_response,
    token_created_page,
)
from memory_manager.app import Services
from memory_manager.auth.owner_rights import OwnerRightsResolver, intersect_namespaces
from memory_manager.auth.scopes import READ_SCOPE, WRITE_SCOPE
from memory_manager.auth.tokens import (
    ALL_NAMESPACES,
    KIND_PERSONAL,
    TokenInfo,
    create_token,
    list_personal_tokens,
    revoke_token,
)
from memory_manager.storage.postgres import PostgresBackend

__all__ = [
    "CREATE_PATH",
    "CSRF_FORM_CREATE",
    "CSRF_FORM_REVOKE",
    "REVOKE_PATH",
    "list_tokens_for_owner",
    "render_tokens_section",
    "token_routes",
]

CREATE_PATH = "/account/tokens"
REVOKE_PATH = "/account/tokens/revoke"

#: One distinct `account.sessions.csrf_token`/`verify_csrf` form label per form -
#: same "every state-changing form gets its own label" shape every other `/account`
#: form already follows (`account.export.CSRF_FORM_EXPORT`, `account.admin.
#: CSRF_FORM_CREATE_NAMESPACE`).
CSRF_FORM_CREATE = "account-tokens-create"
CSRF_FORM_REVOKE = "account-tokens-revoke"

#: `_owner_roles`'s fixed role for a `"password"`/`"oidc"` owner (module docstring) -
#: this module's own copy of the literal (`account/admin.py`'s own `ADMIN_ROLE`
#: docstring: "every module names its own role constants").
_PERSONAL_ROLE_GIT = "Memory.User"

#: Scopes this form ever offers - the two fixed literals `auth.scopes` defines, in
#: the fixed order they are rendered in.
_OFFERED_SCOPES = (READ_SCOPE, WRITE_SCOPE)

#: `auth.tokens.create_token`'s own default (`DEFAULT_PERSONAL_MAX_EXPIRES_DAYS`),
#: used only as the account route's fallback when `expires_days` is missing from the
#: submitted form, before the 1..max range check below ever runs.
_DEFAULT_EXPIRES_DAYS = 90

_ACCOUNT_PATH = "/account"
_NAME_PREFIX = "personal-"
_NAME_ENTROPY_BYTES = 12


def _owner_id(session: SessionInfo) -> str:
    """The signed-in session's own identity to create/own/revoke a personal token
    under - `session.oid` if set (`"entra"`, both backends), `session.subject`
    otherwise (module docstring)."""
    return session.oid if session.oid is not None else session.subject


def _owner_roles(session: SessionInfo) -> tuple[str, ...]:
    """`session.roles` for `"entra"`, `(_PERSONAL_ROLE_GIT,)` otherwise (module
    docstring) - `auth.tokens.create_token`'s own `_validate_owner_and_roles`
    requires at least one role for any owner at all."""
    if session.login_mode == "entra":
        return session.roles
    return (_PERSONAL_ROLE_GIT,)


def _generate_name() -> str:
    """A unique, non-personal token name (module docstring: never a user-chosen
    label) - `auth.tokens`'s own `UNIQUE(name)` constraint is still the final word;
    a collision here is astronomically unlikely (96 bits of entropy) and, if it ever
    happened, surfaces as the same clean `400` every other `UniqueViolationError`
    does below, not a retry loop."""
    return f"{_NAME_PREFIX}{secrets.token_hex(_NAME_ENTROPY_BYTES)}"


def _effective_max_days(*, is_postgres_backend: bool, static_max: int, personal_max: int) -> int:
    """The expiry ceiling this form enforces - `personal_max` alone for `"git"`,
    whichever of `static_max`/`personal_max` is smaller for `"postgres"`
    (`auth.tokens.create_token`'s own docstring: "the effective maximum ... is
    whichever of the two is smaller")."""
    return min(static_max, personal_max) if is_postgres_backend else personal_max


async def list_tokens_for_owner(pool: asyncpg.Pool, session: SessionInfo) -> list[TokenInfo]:
    """`session`'s own `KIND_PERSONAL` tokens - `account.sections._render_tokens`'s
    one read, a thin wrapper around `auth.tokens.list_personal_tokens` keyed by this
    module's own `_owner_id`."""
    return await list_personal_tokens(pool, _owner_id(session))


@dataclass(frozen=True)
class _Authorized:
    pool: asyncpg.Pool
    session: SessionInfo
    form: FormData
    is_postgres_backend: bool
    owner_rights_resolver: OwnerRightsResolver
    static_token_max_days: int
    personal_token_max_days: int


async def _authorize_tokens_form(
    request: Request, *, session_cookie: str, csrf_form: str
) -> _Authorized | Response:
    """The checks every token route makes before touching the database: a database,
    an active session - a `"postgres"`-backend one needs an `oid` too (module
    docstring's `_tokens_enabled` gate, enforced again here since a route is reachable
    directly, not only through a rendered section) - and a valid per-form CSRF token,
    same shape `account.export`/`account.admin`'s own route handlers already follow.
    Returns a `Response` to return immediately on the first failure, or the parsed
    form plus everything a route needs to act on it.
    """
    services: Services = request.app.state.services
    pool = services.pool
    if pool is None:  # pragma: no cover - defensive, see account.sessions' own pool requirement
        return PlainTextResponse("Personal tokens require a database.", status_code=503)
    is_postgres_backend = isinstance(services.storage, PostgresBackend)

    session_id = request.cookies.get(session_cookie)
    if session_id is None:
        return PlainTextResponse("No active session.", status_code=403)
    info = await sessions.lookup(pool, session_id)
    if info is None:
        return PlainTextResponse("No active session.", status_code=403)
    if is_postgres_backend and info.oid is None:
        return PlainTextResponse(
            "Personal tokens require an Entra identity on this backend.", status_code=403
        )

    form = await request.form()
    token = str(form.get(CSRF_FIELD_NAME, ""))
    if not sessions.verify_csrf(session_id, csrf_form, token):
        return PlainTextResponse("Invalid or missing CSRF token.", status_code=403)

    if services.owner_rights_resolver is None:  # pragma: no cover - open_services always sets one
        return PlainTextResponse("Personal tokens require a database.", status_code=503)

    return _Authorized(
        pool=pool,
        session=info,
        form=form,
        is_postgres_backend=is_postgres_backend,
        owner_rights_resolver=services.owner_rights_resolver,
        static_token_max_days=services.static_token_max_days,
        personal_token_max_days=services.personal_token_max_days,
    )


def _parse_scopes(form: FormData) -> tuple[str, ...] | None:
    """The submitted `scopes` values, or `None` if the set is empty or carries
    anything outside `_OFFERED_SCOPES`."""
    scopes = tuple(str(value) for value in form.getlist("scopes"))
    if not scopes or any(scope not in _OFFERED_SCOPES for scope in scopes):
        return None
    return scopes


def _parse_requested_namespaces(form: FormData) -> tuple[str, ...]:
    raw = str(form.get("namespaces", "")).strip()
    return tuple(ns.strip() for ns in raw.split(",") if ns.strip())


def token_routes(session_cookie: str) -> list[Route]:
    """`POST /account/tokens` and `POST /account/tokens/revoke` - mounted by
    `account.routes.page_routes` alongside the rest of the page. Takes the session
    cookie's name as a parameter rather than importing it directly, the same shape
    `account.export.export_routes`/`account.admin.admin_routes` already use."""

    async def _create(request: Request) -> Response:
        authorized = await _authorize_tokens_form(
            request, session_cookie=session_cookie, csrf_form=CSRF_FORM_CREATE
        )
        if isinstance(authorized, Response):
            return authorized
        form, session = authorized.form, authorized.session
        owner = _owner_id(session)

        scopes = _parse_scopes(form)
        if scopes is None:
            return PlainTextResponse(
                f"scopes must be a non-empty subset of {list(_OFFERED_SCOPES)}.",
                status_code=400,
            )

        if authorized.is_postgres_backend:
            # #135's own "Nicht dabei": no per-token namespace narrowing in postgres
            # mode - offered, and stored, as "*"; row-level security does the real
            # narrowing on every query (module docstring).
            requested_namespaces: tuple[str, ...] = (ALL_NAMESPACES,)
        else:
            requested_namespaces = _parse_requested_namespaces(form)
            if not requested_namespaces:
                return PlainTextResponse("namespaces must not be empty.", status_code=400)
            owner_rights = await authorized.owner_rights_resolver.resolve(owner)
            narrowed = intersect_namespaces(requested_namespaces, owner_rights.namespaces)
            if narrowed is None:
                return PlainTextResponse(
                    f"namespaces must be a subset of {list(owner_rights.namespaces)!r}, "
                    f"got {list(requested_namespaces)!r}.",
                    status_code=400,
                )
            requested_namespaces = narrowed

        max_days = _effective_max_days(
            is_postgres_backend=authorized.is_postgres_backend,
            static_max=authorized.static_token_max_days,
            personal_max=authorized.personal_token_max_days,
        )
        expires_raw = str(form.get("expires_days", "")).strip()
        try:
            expires_days = int(expires_raw)
        except ValueError:
            return PlainTextResponse(
                f"expires_days is required and must be an integer between 1 and {max_days}.",
                status_code=400,
            )
        if not 1 <= expires_days <= max_days:
            return PlainTextResponse(
                f"expires_days must be between 1 and {max_days}, got {expires_days}.",
                status_code=400,
            )
        expires_at = datetime.now(UTC) + timedelta(days=expires_days)

        description = str(form.get("description", "")).strip() or None
        name = _generate_name()

        try:
            plaintext, info = await create_token(
                authorized.pool,
                name,
                scopes=scopes,
                namespaces=requested_namespaces,
                expires_at=expires_at,
                owner_oid=owner,
                roles=_owner_roles(session),
                kind=KIND_PERSONAL,
                created_by=owner,
                description=description,
                enterprise=authorized.is_postgres_backend,
                max_expires_days=authorized.static_token_max_days,
                personal_max_days=authorized.personal_token_max_days,
            )
        except (asyncpg.UniqueViolationError, asyncpg.CheckViolationError, ValueError) as exc:
            return PlainTextResponse(str(exc), status_code=400)

        return account_response(
            token_created_page(name=info.name, plaintext=plaintext, back_path=_ACCOUNT_PATH)
        )

    async def _revoke(request: Request) -> Response:
        authorized = await _authorize_tokens_form(
            request, session_cookie=session_cookie, csrf_form=CSRF_FORM_REVOKE
        )
        if isinstance(authorized, Response):
            return authorized
        owner = _owner_id(authorized.session)

        name = str(authorized.form.get("name", "")).strip()
        if not name:
            return PlainTextResponse("name must not be empty.", status_code=400)

        revoked = await revoke_token(
            authorized.pool, name, actor=owner, owner_oid=owner, kind=KIND_PERSONAL
        )
        if not revoked:
            # Indistinguishable from "never existed" (module docstring) - a
            # foreign owner's token name and an unknown one both land here.
            return PlainTextResponse(f"no active token named {name!r}.", status_code=404)

        return RedirectResponse(_ACCOUNT_PATH, status_code=302)

    return [
        Route(CREATE_PATH, endpoint=_create, methods=["POST"]),
        Route(REVOKE_PATH, endpoint=_revoke, methods=["POST"]),
    ]


def _render_scope_checkboxes() -> str:
    labels = {READ_SCOPE: "Read", WRITE_SCOPE: "Write"}
    return "".join(
        f'<label><input type="checkbox" name="scopes" value="{html.escape(scope)}" checked> '
        f"{html.escape(labels[scope])}</label>"
        for scope in _OFFERED_SCOPES
    )


def _render_create_form(
    *,
    session_id: str,
    is_postgres_backend: bool,
    owner_namespaces: tuple[str, ...],
    max_days: int,
) -> str:
    token = sessions.csrf_token(session_id, CSRF_FORM_CREATE)
    if is_postgres_backend:
        namespaces_field = (
            "<label>Namespaces "
            '<input type="text" value="*" readonly> '
            "(this account's namespace access, enforced by the server)</label>"
        )
    else:
        default_namespaces = ", ".join(owner_namespaces)
        namespaces_field = (
            "<label>Namespaces (comma-separated, from "
            f"{html.escape(default_namespaces)}) "
            f'<input type="text" name="namespaces" value="{html.escape(default_namespaces)}">'
            "</label>"
        )
    return (
        "<h3>Create a personal token</h3>"
        f'<form method="post" action="{html.escape(CREATE_PATH)}">'
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{html.escape(token)}">'
        f"<fieldset><legend>Scopes</legend>{_render_scope_checkboxes()}</fieldset>"
        f"{namespaces_field}"
        f"<label>Expiry in days (1-{max_days}) "
        f'<input type="number" name="expires_days" min="1" max="{max_days}" '
        f'value="{min(_DEFAULT_EXPIRES_DAYS, max_days)}"></label>'
        "<label>Description (optional, for your own reference) "
        '<input type="text" name="description"></label>'
        '<button type="submit">Create token</button>'
        "</form>"
    )


def _format_datetime(value: datetime | None) -> str:
    return value.isoformat() if value is not None else "-"


def _action_cell(info: TokenInfo, *, revoke_token_csrf: str) -> str:
    if info.revoked_at is not None:
        return "revoked"
    return _revoke_form(info.name, revoke_token_csrf)


def _render_tokens_table(tokens: list[TokenInfo], *, revoke_token_csrf: str) -> str:
    if not tokens:
        return "<p>You have no personal tokens yet.</p>"
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(info.description or '-')}</td>"
        f"<td>{html.escape(', '.join(info.scopes))}</td>"
        f"<td>{html.escape(', '.join(info.namespaces))}</td>"
        f"<td>{_format_datetime(info.expires_at)}</td>"
        f"<td>{_format_datetime(info.last_used_at)}</td>"
        f"<td>{_action_cell(info, revoke_token_csrf=revoke_token_csrf)}</td>"
        "</tr>"
        for info in tokens
    )
    return (
        "<table><thead><tr><th>Description</th><th>Scopes</th><th>Namespaces</th>"
        "<th>Expires</th><th>Last used</th><th>Actions</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _revoke_form(name: str, csrf_value: str) -> str:
    return (
        f'<form method="post" action="{html.escape(REVOKE_PATH)}">'
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{html.escape(csrf_value)}">'
        f'<input type="hidden" name="name" value="{html.escape(name)}">'
        '<button type="submit">Revoke</button></form>'
    )


def render_tokens_section(
    tokens: list[TokenInfo],
    *,
    session_id: str,
    is_postgres_backend: bool,
    owner_namespaces: tuple[str, ...],
    max_days: int,
) -> str:
    """The token section's own markup: the owner's own personal tokens plus the
    create form - called by `account.sections._render_tokens`, which owns the
    `<section>` gating (`_tokens_enabled`) this module does not decide on its own."""
    revoke_csrf = sessions.csrf_token(session_id, CSRF_FORM_REVOKE)
    create_form = _render_create_form(
        session_id=session_id,
        is_postgres_backend=is_postgres_backend,
        owner_namespaces=owner_namespaces,
        max_days=max_days,
    )
    return (
        "<section><h2>Personal tokens</h2>"
        "<p>A personal token lets a client authenticate as you - a headless CLI, "
        "an IDE or an agent runtime that cannot run the usual sign-in flow.</p>"
        f"{_render_tokens_table(tokens, revoke_token_csrf=revoke_csrf)}"
        f"{create_form}"
        "</section>"
    )
