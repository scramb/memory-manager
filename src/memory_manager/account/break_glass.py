# SPDX-License-Identifier: AGPL-3.0-only
"""Break-glass read grants on `/account`'s admin area (#237, ADR-0008
"Break-glass" + its addendum 2026-10-08).

Postgres mode only, like every enterprise `/account` section
(`account.sections`'s own docstring on `is_postgres_backend`/`session.oid`),
additionally gated on `ADMIN_ROLE in session.roles` - a session without
`Memory.Admin` never even sees the section, let alone reaches one of its
routes (`account.sections._break_glass_enabled`, the same shape
`account.admin._admin_enabled` already uses).

An admin requests read access to one *person* (by Entra `oid`, the same
"target user" field `account.admin`'s own "revoke user access" form already
uses), with a reason - never a namespace alias directly, since there is
nothing to request if that person has never used memory-manager at all
(`mm_break_glass_request` resolves the oid to a personal namespace itself
and refuses if none exists). A second `Memory.Admin` (or, with
`BREAK_GLASS_APPROVERS=1`, the same one) approves, denies or revokes it.
Every mutation goes through one of the five `mm_break_glass_*`
`SECURITY DEFINER` functions `migrations/0021_break_glass_workflow.sql`
adds - this module never touches `break_glass_grants` with a bare
`INSERT`/`UPDATE`, the same "no general grant on the registry" shape
`account.admin` already follows for `namespaces`/`project_members`.

Each call runs under the *caller's own* identity (`db.rls.request_identity`,
the same direct call `account.admin`/`account.export`/`account.delete`
already make rather than `db.rls.request_connection` - those modules' own
docstrings explain why a cookie-authenticated `/account` request carries no
bearer token for that contextvar to read), so the SQL function's own
`'Memory.Admin' = any(app.roles)` check sees the real caller, never the
owner - the second, independent enforcement of the role (CLAUDE.md
"enforced twice, in Python and in SQL"); `_authorize_break_glass_form`
below is the first, in Python, the one every route actually relies on to
return a clean 403 rather than ever reaching the database for a non-admin.

The four-eyes rule is the same shape: `_authorize_break_glass_form` reads
`services.break_glass_approvers` (`config.break_glass_approvers_from_env`,
ADR-0008 default 2) and the approve route refuses a same-admin approval in
Python *before* calling `mm_break_glass_approve` - which takes the
identical count as its own `p_approver_count` argument and refuses it
again, independently, so a bug in this module's own check alone would not
be enough to let a self-approval through.

Every action is audited as one `admin.break_glass.*` row (request, approve,
deny, revoke) - metadata only (grant id, target oid, reason), never note
content, since none of these operations ever touch one; reading under a
grant happens only in the separate viewer #238 builds, not here.
"""

from __future__ import annotations

import html
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

import asyncpg
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from memory_manager.account import sessions
from memory_manager.account.sessions import SessionInfo
from memory_manager.account.templates import CSRF_FIELD_NAME
from memory_manager.app import Services
from memory_manager.audit import AuditWriter
from memory_manager.db import rls
from memory_manager.storage.postgres import PostgresBackend

__all__ = [
    "ADMIN_ROLE",
    "APPROVE_PATH",
    "CSRF_FORM_APPROVE",
    "CSRF_FORM_DENY",
    "CSRF_FORM_REQUEST",
    "CSRF_FORM_REVOKE",
    "DENY_PATH",
    "REQUEST_PATH",
    "REVOKE_PATH",
    "BreakGlassActionError",
    "BreakGlassFormError",
    "GrantRow",
    "break_glass_routes",
    "list_grants",
    "render_break_glass_section",
]

#: This module's own copy of the literal (`account/admin.py`'s own
#: `ADMIN_ROLE` docstring: "every module names its own role constants").
ADMIN_ROLE = "Memory.Admin"

REQUEST_PATH = "/account/admin/break-glass/request"
APPROVE_PATH = "/account/admin/break-glass/approve"
DENY_PATH = "/account/admin/break-glass/deny"
REVOKE_PATH = "/account/admin/break-glass/revoke"

#: One distinct `account.sessions.csrf_token`/`verify_csrf` form label per
#: action - same "every state-changing form gets its own label" shape every
#: other `/account` form already follows.
CSRF_FORM_REQUEST = "account-admin-break-glass-request"
CSRF_FORM_APPROVE = "account-admin-break-glass-approve"
CSRF_FORM_DENY = "account-admin-break-glass-deny"
CSRF_FORM_REVOKE = "account-admin-break-glass-revoke"

_ADMIN_CLIENT = "account"


class BreakGlassFormError(ValueError):
    """A submitted break-glass form is invalid (empty target/reason, a
    non-numeric grant id) - reported to the caller as `400`, before any
    database round trip at all."""


class BreakGlassActionError(Exception):
    """One `mm_break_glass_*` call failed in a way this module recognizes and
    reports with a clean status code - anything else propagates as an
    unhandled exception (a `500`), the same "only map the errors we
    actually expect" rule `account.admin._map_admin_error` already follows.
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _map_break_glass_error(exc: asyncpg.PostgresError) -> BreakGlassActionError:
    """Turn the handful of Postgres errors `migrations/0021_break_glass_workflow.sql`'s
    own functions can raise into a clean `BreakGlassActionError` - re-raises
    anything else unchanged (this module's own docstring)."""
    if isinstance(exc, asyncpg.InsufficientPrivilegeError):
        return BreakGlassActionError(
            "Break-glass actions require the Memory.Admin role, and approval "
            "requires a second admin unless BREAK_GLASS_APPROVERS=1.",
            status_code=403,
        )
    if isinstance(exc, asyncpg.NoDataFoundError):
        return BreakGlassActionError("No matching break-glass grant.", status_code=404)
    if isinstance(exc, asyncpg.InvalidParameterValueError):
        return BreakGlassActionError(str(exc), status_code=400)
    raise exc


@dataclass(frozen=True)
class GrantRow:
    """One row `mm_break_glass_list()` reports - metadata only, never note
    content (this module's own docstring)."""

    id: int
    alias: str
    target_oid: str
    requester: str
    reason: str
    requested_at: datetime
    approved: bool
    approved_by: str | None
    approved_at: datetime | None
    expires_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class _Authorized:
    pool: asyncpg.Pool
    app_role: str
    session: SessionInfo
    form: FormData
    #: `services.break_glass_approvers` (`config.break_glass_approvers_from_env`,
    #: ADR-0008 default 2) - `_approve`'s own four-eyes check and its
    #: `mm_break_glass_approve` call both need it.
    break_glass_approvers: int


async def _authorize_break_glass_form(
    request: Request, *, session_cookie: str, csrf_form: str
) -> _Authorized | Response:
    """The checks every break-glass route makes before touching the
    database: a Postgres-backend deployment, an active session, `ADMIN_ROLE`
    in that session's roles, and a valid per-form CSRF token - in that
    order, same shape `account.admin._authorize_admin_form` already
    follows. Returns a `Response` to return immediately on the first
    failure, or the parsed form plus everything the caller needs to act on
    it.
    """
    services: Services = request.app.state.services
    pool = services.pool
    if pool is None:  # pragma: no cover - defensive, see account.sessions' own pool requirement
        return PlainTextResponse("Break-glass actions require a database.", status_code=503)
    if not isinstance(services.storage, PostgresBackend) or services.app_role is None:
        return PlainTextResponse(
            "Break-glass actions require the Postgres backend.", status_code=403
        )

    session_id = request.cookies.get(session_cookie)
    if session_id is None:
        return PlainTextResponse("No active session.", status_code=403)
    info = await sessions.lookup(pool, session_id)
    if info is None:
        return PlainTextResponse("No active session.", status_code=403)
    if info.oid is None or ADMIN_ROLE not in info.roles:
        return PlainTextResponse(
            "Break-glass actions require the Memory.Admin role.", status_code=403
        )

    form = await request.form()
    token = str(form.get(CSRF_FIELD_NAME, ""))
    if not sessions.verify_csrf(session_id, csrf_form, token):
        return PlainTextResponse("Invalid or missing CSRF token.", status_code=403)

    return _Authorized(
        pool=pool,
        app_role=services.app_role,
        session=info,
        form=form,
        break_glass_approvers=services.break_glass_approvers,
    )


@asynccontextmanager
async def _identity(
    pool: asyncpg.Pool, *, app_role: str, session: SessionInfo
) -> AsyncIterator[asyncpg.pool.PoolConnectionProxy | asyncpg.Connection]:
    """`rls.request_identity` for `session`, with every `mm_break_glass_*`
    error this module recognizes mapped to `BreakGlassActionError` - same
    shape `account.admin._admin_identity`."""
    if session.oid is None:  # pragma: no cover - defensive, every route checks this first
        raise BreakGlassActionError(
            "Break-glass actions require an Entra identity.", status_code=403
        )
    try:
        async with (
            pool.acquire() as conn,
            rls.request_identity(
                conn, role=app_role, oid=session.oid, roles=session.roles
            ) as identified,
        ):
            yield identified
    except asyncpg.PostgresError as exc:
        raise _map_break_glass_error(exc) from exc


def _parse_grant_id(raw: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise BreakGlassFormError(f"grant_id must be an integer, got {raw!r}") from exc


async def list_grants(pool: asyncpg.Pool, *, app_role: str, session: SessionInfo) -> list[GrantRow]:
    """Every break-glass grant, newest first (`mm_break_glass_list()`) - for the
    section's own pending-list/history table."""
    async with _identity(pool, app_role=app_role, session=session) as conn:
        rows = await conn.fetch("select * from mm_break_glass_list()")
    return [
        GrantRow(
            id=row["id"],
            alias=row["alias"],
            target_oid=row["target_oid"],
            requester=row["requester"],
            reason=row["reason"],
            requested_at=row["requested_at"],
            approved=row["approved"],
            approved_by=row["approved_by"],
            approved_at=row["approved_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )
        for row in rows
    ]


def break_glass_routes(session_cookie: str) -> list[Route]:
    """Every `POST /account/admin/break-glass/...` route - mounted by
    `account.routes.page_routes` alongside the rest of the page. Takes the
    session cookie's name as a parameter rather than importing it directly,
    the same shape `account.admin.admin_routes` already uses."""

    async def _request(request: Request) -> Response:
        authorized = await _authorize_break_glass_form(
            request, session_cookie=session_cookie, csrf_form=CSRF_FORM_REQUEST
        )
        if isinstance(authorized, Response):
            return authorized
        pool, app_role, session, form = (
            authorized.pool,
            authorized.app_role,
            authorized.session,
            authorized.form,
        )

        target_oid = str(form.get("oid", "")).strip()
        reason = str(form.get("reason", "")).strip()
        if not target_oid:
            return PlainTextResponse("oid must not be empty.", status_code=400)
        if not reason:
            return PlainTextResponse("reason must not be empty.", status_code=400)

        try:
            async with _identity(pool, app_role=app_role, session=session) as conn:
                grant_id = await conn.fetchval(
                    "select mm_break_glass_request($1, $2)", target_oid, reason
                )
        except BreakGlassActionError as exc:
            return PlainTextResponse(str(exc), status_code=exc.status_code)

        await AuditWriter(pool).record(
            actor=session.subject,
            client=_ADMIN_CLIENT,
            op="admin.break_glass.request",
            path=None,
            commit_sha=None,
            outcome="ok",
            detail={"grant_id": grant_id, "target_oid": target_oid, "reason": reason},
        )
        return RedirectResponse("/account", status_code=302)

    async def _approve(request: Request) -> Response:
        authorized = await _authorize_break_glass_form(
            request, session_cookie=session_cookie, csrf_form=CSRF_FORM_APPROVE
        )
        if isinstance(authorized, Response):
            return authorized
        pool, app_role, session, form = (
            authorized.pool,
            authorized.app_role,
            authorized.session,
            authorized.form,
        )

        try:
            grant_id = _parse_grant_id(str(form.get("grant_id", "")))
        except BreakGlassFormError as exc:
            return PlainTextResponse(str(exc), status_code=400)

        try:
            async with _identity(pool, app_role=app_role, session=session) as conn:
                # The Python-side half of the four-eyes check (module
                # docstring: "enforced twice, in Python and in SQL") - looked
                # up through `mm_break_glass_list()` rather than a bare
                # `SELECT`, since the app role holds no direct grant on
                # `break_glass_grants` at all (same "no general grant on the
                # registry" shape `account.admin` already follows).
                # `mm_break_glass_approve` below re-checks the identical rule
                # independently; a bug here alone would not be enough to let
                # a same-admin approval through.
                grant_row = await conn.fetchrow(
                    "select requester from mm_break_glass_list() where id = $1", grant_id
                )
                if grant_row is None:
                    return PlainTextResponse(
                        f"No break-glass grant with id {grant_id}.", status_code=404
                    )
                if authorized.break_glass_approvers > 1 and grant_row["requester"] == session.oid:
                    return PlainTextResponse(
                        "A second admin (not the requester) must approve this grant.",
                        status_code=403,
                    )
                await conn.execute(
                    "select mm_break_glass_approve($1, $2)",
                    grant_id,
                    authorized.break_glass_approvers,
                )
        except BreakGlassActionError as exc:
            return PlainTextResponse(str(exc), status_code=exc.status_code)

        await AuditWriter(pool).record(
            actor=session.subject,
            client=_ADMIN_CLIENT,
            op="admin.break_glass.approve",
            path=None,
            commit_sha=None,
            outcome="ok",
            detail={"grant_id": grant_id},
        )
        return RedirectResponse("/account", status_code=302)

    async def _deny(request: Request) -> Response:
        authorized = await _authorize_break_glass_form(
            request, session_cookie=session_cookie, csrf_form=CSRF_FORM_DENY
        )
        if isinstance(authorized, Response):
            return authorized
        pool, app_role, session, form = (
            authorized.pool,
            authorized.app_role,
            authorized.session,
            authorized.form,
        )

        try:
            grant_id = _parse_grant_id(str(form.get("grant_id", "")))
        except BreakGlassFormError as exc:
            return PlainTextResponse(str(exc), status_code=400)

        try:
            async with _identity(pool, app_role=app_role, session=session) as conn:
                await conn.execute("select mm_break_glass_deny($1)", grant_id)
        except BreakGlassActionError as exc:
            return PlainTextResponse(str(exc), status_code=exc.status_code)

        await AuditWriter(pool).record(
            actor=session.subject,
            client=_ADMIN_CLIENT,
            op="admin.break_glass.deny",
            path=None,
            commit_sha=None,
            outcome="ok",
            detail={"grant_id": grant_id},
        )
        return RedirectResponse("/account", status_code=302)

    async def _revoke(request: Request) -> Response:
        authorized = await _authorize_break_glass_form(
            request, session_cookie=session_cookie, csrf_form=CSRF_FORM_REVOKE
        )
        if isinstance(authorized, Response):
            return authorized
        pool, app_role, session, form = (
            authorized.pool,
            authorized.app_role,
            authorized.session,
            authorized.form,
        )

        try:
            grant_id = _parse_grant_id(str(form.get("grant_id", "")))
        except BreakGlassFormError as exc:
            return PlainTextResponse(str(exc), status_code=400)

        try:
            async with _identity(pool, app_role=app_role, session=session) as conn:
                await conn.execute("select mm_break_glass_revoke($1)", grant_id)
        except BreakGlassActionError as exc:
            return PlainTextResponse(str(exc), status_code=exc.status_code)

        await AuditWriter(pool).record(
            actor=session.subject,
            client=_ADMIN_CLIENT,
            op="admin.break_glass.revoke",
            path=None,
            commit_sha=None,
            outcome="ok",
            detail={"grant_id": grant_id},
        )
        return RedirectResponse("/account", status_code=302)

    return [
        Route(REQUEST_PATH, endpoint=_request, methods=["POST"]),
        Route(APPROVE_PATH, endpoint=_approve, methods=["POST"]),
        Route(DENY_PATH, endpoint=_deny, methods=["POST"]),
        Route(REVOKE_PATH, endpoint=_revoke, methods=["POST"]),
    ]


def _grant_status(row: GrantRow) -> str:
    if row.revoked_at is not None:
        return "denied" if not row.approved else "revoked"
    if row.approved:
        return "approved"
    return "pending"


def _hidden_csrf(token: str) -> str:
    return f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{html.escape(token)}">'


def _render_grants_table(
    rows: list[GrantRow], *, approve_token: str, deny_token: str, revoke_token: str
) -> str:
    if not rows:
        return "<p>No break-glass grants yet.</p>"
    body_rows: list[str] = []
    for row in rows:
        status = _grant_status(row)
        if status == "pending":
            action = (
                f'<form method="post" action="{html.escape(APPROVE_PATH)}">'
                f"{_hidden_csrf(approve_token)}"
                f'<input type="hidden" name="grant_id" value="{row.id}">'
                '<button type="submit">Approve</button></form>'
                f'<form method="post" action="{html.escape(DENY_PATH)}">'
                f"{_hidden_csrf(deny_token)}"
                f'<input type="hidden" name="grant_id" value="{row.id}">'
                '<button type="submit">Deny</button></form>'
            )
        elif status == "approved":
            action = (
                f'<form method="post" action="{html.escape(REVOKE_PATH)}">'
                f"{_hidden_csrf(revoke_token)}"
                f'<input type="hidden" name="grant_id" value="{row.id}">'
                '<button type="submit">Revoke</button></form>'
            )
        else:
            action = ""
        body_rows.append(
            "<tr>"
            f"<td>{row.id}</td>"
            f"<td>{html.escape(row.alias)}</td>"
            f"<td>{html.escape(row.target_oid)}</td>"
            f"<td>{html.escape(row.requester)}</td>"
            f"<td>{html.escape(row.reason)}</td>"
            f"<td>{status}</td>"
            f"<td>{row.expires_at.isoformat()}</td>"
            f"<td>{action}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Id</th><th>Alias</th><th>Target</th><th>Requester</th>"
        "<th>Reason</th><th>Status</th><th>Expires</th><th>Actions</th></tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table>"
    )


def render_break_glass_section(rows: list[GrantRow], *, session_id: str) -> str:
    """The break-glass section's own markup: the grant table plus the request
    form - called by `account.sections._render_break_glass`, which owns the
    `<section>` gating (`_break_glass_enabled`) this module does not decide
    on its own."""
    request_token = sessions.csrf_token(session_id, CSRF_FORM_REQUEST)
    approve_token = sessions.csrf_token(session_id, CSRF_FORM_APPROVE)
    deny_token = sessions.csrf_token(session_id, CSRF_FORM_DENY)
    revoke_token = sessions.csrf_token(session_id, CSRF_FORM_REVOKE)
    table = _render_grants_table(
        rows, approve_token=approve_token, deny_token=deny_token, revoke_token=revoke_token
    )

    return (
        "<section><h2>Break-glass</h2>"
        f"{table}"
        "<h3>Request access</h3>"
        f'<form method="post" action="{html.escape(REQUEST_PATH)}">'
        f"{_hidden_csrf(request_token)}"
        '<label>Entra object id <input type="text" name="oid"></label>'
        '<label>Reason <input type="text" name="reason"></label>'
        '<button type="submit">Request access</button>'
        "</form>"
        "</section>"
    )
