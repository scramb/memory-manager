# SPDX-License-Identifier: AGPL-3.0-only
"""The `/account` break-glass notice banner and its acknowledge route (#239,
ADR-0008 addendum 2026-10-08 "/account session and break-glass notification").

Distinct from `account.break_glass` (#237): that module is the *admin* side
(request/approve/deny/revoke, every route gated on `Memory.Admin`); this one
is the *affected user's* side - every authenticated session with an Entra
`oid` sees its own banner, regardless of role, the same `is_postgres_backend`/
`session.oid` gate `account.export`/`account.delete` already use (there is no
personal namespace, and so no grant to be notified about, without either).
`mm_break_glass_notices()`/`mm_break_glass_acknowledge()` (migration
`0023_break_glass_notice.sql`) are this module's own two functions, distinct
from `account.break_glass`'s five `mm_break_glass_*` ones: both resolve the
caller's own personal namespace from `app.oid` instead of taking a target
`oid`/`namespace_id` as a parameter, so a non-admin can see and acknowledge
their own notice but never anyone else's, let alone request/approve/deny/
revoke one.

Runs under the caller's own identity (`db.rls.request_identity`, the same
direct call every other `/account` module already makes instead of
`db.rls.request_connection` - see `account.sections`'s own docstring for why
a cookie-authenticated request carries no bearer token for that contextvar
to read).

Acknowledging is audited as one `account.break_glass_notice.acknowledge` row
- metadata only (the grant id), the same shape every other `/account`
action's own `AuditWriter.record` call already uses.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime

import asyncpg
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
    "ACKNOWLEDGE_PATH",
    "CSRF_FORM_ACKNOWLEDGE",
    "NoticeRow",
    "break_glass_notice_routes",
    "list_notices",
    "render_break_glass_notice",
]

ACKNOWLEDGE_PATH = "/account/break-glass/acknowledge"

#: The `account.sessions.csrf_token`/`verify_csrf` form label for the
#: acknowledge button - distinct from `account.break_glass`'s own four
#: admin-side labels (that module's own docstring: "every state-changing
#: form gets its own label").
CSRF_FORM_ACKNOWLEDGE = "account-break-glass-acknowledge"

_CLIENT = "account"


@dataclass(frozen=True)
class NoticeRow:
    """One row `mm_break_glass_notices()` reports - metadata only, never note
    content (same reasoning `account.break_glass.GrantRow`'s own docstring gives)."""

    id: int
    requester: str
    approved_by: str | None
    reason: str
    approved_at: datetime | None
    expires_at: datetime
    revoked_at: datetime | None


async def list_notices(
    pool: asyncpg.Pool, *, app_role: str, session: SessionInfo
) -> list[NoticeRow]:
    """Every unacknowledged, approved break-glass grant on the caller's own
    personal namespace (`mm_break_glass_notices()`) - for `OVERVIEW_SECTION`'s
    own banner. `[]` when `session.oid` is `None`: there is no personal
    namespace, and so nothing to be notified about, without one (same gate
    `account.sections._personal_note_count` already uses)."""
    if session.oid is None:
        return []
    async with (
        pool.acquire() as conn,
        rls.request_identity(
            conn, role=app_role, oid=session.oid, roles=session.roles
        ) as identified,
    ):
        rows = await identified.fetch("select * from mm_break_glass_notices()")
    return [
        NoticeRow(
            id=row["id"],
            requester=row["requester"],
            approved_by=row["approved_by"],
            reason=row["reason"],
            approved_at=row["approved_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )
        for row in rows
    ]


def render_break_glass_notice(rows: list[NoticeRow], *, session_id: str) -> str:
    """The banner `OVERVIEW_SECTION` shows for every row in `rows` - the empty
    string when there is nothing to acknowledge, so a session with no notice
    at all gains no extra markup."""
    if not rows:
        return ""
    token = sessions.csrf_token(session_id, CSRF_FORM_ACKNOWLEDGE)
    items: list[str] = []
    for row in rows:
        ended = (
            f" Access has already ended ({row.revoked_at.isoformat()})."
            if row.revoked_at is not None
            else f" Access expires {row.expires_at.isoformat()}."
        )
        items.append(
            "<li>"
            f"An administrator ({html.escape(row.approved_by or 'unknown')}) was granted "
            f"read access to your personal memory, requested by "
            f"{html.escape(row.requester)}. Reason: {html.escape(row.reason)}.{ended}"
            f'<form method="post" action="{html.escape(ACKNOWLEDGE_PATH)}">'
            f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{html.escape(token)}">'
            f'<input type="hidden" name="grant_id" value="{row.id}">'
            '<button type="submit">Acknowledge</button></form>'
            "</li>"
        )
    return "<section><h2>Break-glass notice</h2><ul>" + "".join(items) + "</ul></section>"


def break_glass_notice_routes(session_cookie: str) -> list[Route]:
    """`POST /account/break-glass/acknowledge` - mounted by `account.routes.
    page_routes` alongside the rest of the page. Takes the session cookie's
    name as a parameter rather than importing it directly, the same shape
    every other `/account` route module already uses."""

    async def _acknowledge(request: Request) -> Response:
        services: Services = request.app.state.services
        pool = services.pool
        if pool is None:  # pragma: no cover - defensive, see account.sessions' own pool requirement
            return PlainTextResponse("Acknowledging requires a database.", status_code=503)
        if not isinstance(services.storage, PostgresBackend) or services.app_role is None:
            return PlainTextResponse(
                "Acknowledging requires the Postgres backend.", status_code=403
            )

        session_id = request.cookies.get(session_cookie)
        if session_id is None:
            return PlainTextResponse("No active session.", status_code=403)
        info = await sessions.lookup(pool, session_id)
        if info is None:
            return PlainTextResponse("No active session.", status_code=403)
        if info.oid is None:
            return PlainTextResponse(
                "This login mode has no break-glass notice to acknowledge.", status_code=403
            )

        form = await request.form()
        token = str(form.get(CSRF_FIELD_NAME, ""))
        if not sessions.verify_csrf(session_id, CSRF_FORM_ACKNOWLEDGE, token):
            return PlainTextResponse("Invalid or missing CSRF token.", status_code=403)

        raw_grant_id = str(form.get("grant_id", ""))
        try:
            grant_id = int(raw_grant_id)
        except ValueError:
            return PlainTextResponse(
                f"grant_id must be an integer, got {raw_grant_id!r}.", status_code=400
            )

        async with (
            pool.acquire() as conn,
            rls.request_identity(
                conn, role=services.app_role, oid=info.oid, roles=info.roles
            ) as identified,
        ):
            try:
                await identified.execute("select mm_break_glass_acknowledge($1)", grant_id)
            except asyncpg.NoDataFoundError:
                return PlainTextResponse(
                    f"No unacknowledged break-glass grant with id {grant_id} for this user.",
                    status_code=404,
                )

        await AuditWriter(pool).record(
            actor=info.subject,
            client=_CLIENT,
            op="account.break_glass_notice.acknowledge",
            path=None,
            commit_sha=None,
            outcome="ok",
            detail={"grant_id": grant_id},
        )
        return RedirectResponse("/account", status_code=302)

    return [Route(ACKNOWLEDGE_PATH, endpoint=_acknowledge, methods=["POST"])]
