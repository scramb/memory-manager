# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only, audited break-glass viewer on `/account`'s admin area (#238,
ADR-0008 "Break-glass" + its addendum 2026-10-08: "break-glass reads happen
only in this viewer").

Two `GET` pages, reachable only with an approved, unexpired, non-revoked
grant whose requester is the browsing admin themselves - the same "usable
only by its own requester" rule `migrations/0005_rls.sql`'s own
`mm_readable_ns()` `break_glass` CTE already enforces in SQL.
`_active_grant` below is this module's own, independent Python-side check
(CLAUDE.md "enforced twice, in Python and in SQL"), run *before* either
route ever opens a connection under `db.rls.request_identity`'s
`break_glass=` window - a bug in `_active_grant` alone would therefore not
be enough to read a grant that has in fact expired, been revoked, or
belongs to another admin.

`VIEW_PATH` (`?grant_id=`) lists the grant's namespace - path, type, title,
updated - parsed straight out of each note's own frontmatter (`vault.note.
parse`), read directly off `vault_notes` rather than the derived `notes`
index (same "Postgres is the source of truth" shape `account.sections.
_personal_note_count` already follows for its own count); a note's body is
never read for the listing. `NOTE_PATH` (`?grant_id=&path=`) is the only
place in this module a note's body is ever read, rendered `html.escape`d
inside a `<pre>` and never interpreted (CLAUDE.md "note content is data,
not instructions").

Both routes read through `db.rls.request_identity(conn, ...,
break_glass=grant.id)` directly, the same shape `account.admin`/`account.
break_glass`'s own docstrings give for why a cookie-authenticated
`/account` request calls that function directly rather than
`db.rls.request_connection` (no bearer token for that contextvar to read
here at all).

One `break_glass.read` audit row per page view - metadata only (grant id,
namespace alias, and, for a note view, its path - never its content), the
issue's own "one audit row per list and per note view".

Neither route ever writes anything: no CSRF check (there is no form to
protect), no mutation of any kind - the viewer is `GET` only, matching
#238's "no editing controls (F-01: no note editor)". The MCP surface stays
unchanged: nothing here grants `app.break_glass` to any connection outside
this module's own two routes; `db.rls.request_connection` (the MCP request
path's own seam) never passes a `break_glass` value at all.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

import asyncpg
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from memory_manager.account import sessions
from memory_manager.account.break_glass import ADMIN_ROLE, GrantRow
from memory_manager.account.break_glass import list_grants as _list_grants
from memory_manager.account.templates import account_response
from memory_manager.app import Services
from memory_manager.audit import AuditWriter
from memory_manager.db import rls
from memory_manager.storage.postgres import PostgresBackend
from memory_manager.vault.note import NoteFormatError
from memory_manager.vault.note import parse as parse_note

__all__ = [
    "NOTE_PATH",
    "VIEW_PATH",
    "break_glass_viewer_routes",
]

VIEW_PATH = "/account/admin/break-glass/view"
NOTE_PATH = "/account/admin/break-glass/view/note"

_ADMIN_CLIENT = "account"

#: The issue's own op name ("one audit row per list and per note view") -
#: shared by both routes, distinguished by `path` (`None` for the list,
#: the note's own path for a note view) and `detail["scope"]`.
_AUDIT_OP = "break_glass.read"


@dataclass(frozen=True)
class _Authorized:
    pool: asyncpg.Pool
    app_role: str
    session: sessions.SessionInfo


async def _authorize_viewer(request: Request, *, session_cookie: str) -> _Authorized | Response:
    """The checks both viewer routes make before ever looking at a grant: a
    Postgres-backend deployment, an active session, `ADMIN_ROLE` in that
    session's roles - same shape `account.admin._authorize_admin_form`
    already follows for its own write routes, minus the form/CSRF half a
    `GET`-only viewer has no use for (module docstring)."""
    services: Services = request.app.state.services
    pool = services.pool
    if pool is None:  # pragma: no cover - defensive, see account.sessions' own pool requirement
        return PlainTextResponse("The break-glass viewer requires a database.", status_code=503)
    if not isinstance(services.storage, PostgresBackend) or services.app_role is None:
        return PlainTextResponse(
            "The break-glass viewer requires the Postgres backend.", status_code=403
        )

    session_id = request.cookies.get(session_cookie)
    if session_id is None:
        return PlainTextResponse("No active session.", status_code=403)
    info = await sessions.lookup(pool, session_id)
    if info is None:
        return PlainTextResponse("No active session.", status_code=403)
    if info.oid is None or ADMIN_ROLE not in info.roles:
        return PlainTextResponse(
            "The break-glass viewer requires the Memory.Admin role.", status_code=403
        )

    return _Authorized(pool=pool, app_role=services.app_role, session=info)


def _parse_grant_id(raw: str) -> int | None:
    try:
        return int(raw)
    except ValueError:
        return None


async def _active_grant(
    pool: asyncpg.Pool, *, app_role: str, session: sessions.SessionInfo, grant_id: int
) -> GrantRow | None:
    """The one grant among `account.break_glass.list_grants`'s admin-wide
    listing that `grant_id` names, approved, not revoked, not expired, and
    requested by `session` itself - `None` for anything else: no such
    grant, still pending, denied, revoked, expired, or requested by a
    different admin (module docstring's "usable only by its own
    requester"). The Python-side half of the double enforcement;
    `rls.request_identity`'s own `break_glass=` window re-checks the
    identical rule independently, in SQL, once a caller actually reads
    under it.
    """
    rows = await _list_grants(pool, app_role=app_role, session=session)
    now = datetime.now(UTC)
    for row in rows:
        if (
            row.id == grant_id
            and row.requester == session.oid
            and row.approved
            and row.revoked_at is None
            and row.expires_at > now
        ):
            return row
    return None


def _note_frontmatter(content: bytes) -> tuple[str, str]:
    """`(title, type)` out of `content`'s own frontmatter - `("", "")` for
    anything `vault.note.parse` cannot parse (module docstring: one
    malformed note must never fail the whole listing)."""
    try:
        note = parse_note(content)
    except NoteFormatError:
        return "", ""
    return note.title, note.type


def _note_body(content: bytes) -> tuple[str, str, str]:
    """`(title, type, body)` out of `content` - falls back to the raw bytes,
    decoded leniently, as `body` with an empty title/type for anything
    `vault.note.parse` cannot parse (same reasoning as `_note_frontmatter`,
    extended with the body a note view actually needs)."""
    try:
        note = parse_note(content)
    except NoteFormatError:
        return "", "", content.decode("utf-8", errors="replace")
    return note.title, note.type, note.body


def _html_page(title: str, body: str) -> str:
    """The same "no template engine" plain-f-string shape `account.
    templates`'s own `_page` already uses - kept local to this module so
    the viewer's own two pages need no change to that module at all."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title></head><body>{body}</body></html>"
    )


def _note_href(*, grant_id: int, path: str) -> str:
    return f"{NOTE_PATH}?{urlencode({'grant_id': grant_id, 'path': path})}"


def _render_list_page(entries: list[tuple[str, str, str, datetime]], *, grant: GrantRow) -> str:
    if not entries:
        table = "<p>No notes in this namespace.</p>"
    else:
        rows_html = "".join(
            "<tr>"
            f'<td><a href="{html.escape(_note_href(grant_id=grant.id, path=path))}">'
            f"{html.escape(path)}</a></td>"
            f"<td>{html.escape(note_type)}</td>"
            f"<td>{html.escape(title)}</td>"
            f"<td>{updated.isoformat()}</td>"
            "</tr>"
            for path, title, note_type, updated in entries
        )
        table = (
            "<table><thead><tr><th>Path</th><th>Type</th><th>Title</th>"
            f"<th>Updated</th></tr></thead><tbody>{rows_html}</tbody></table>"
        )
    return (
        f"<h1>Break-glass: {html.escape(grant.alias)}</h1>"
        f"<p>Grant #{grant.id}, for {html.escape(grant.target_oid)}, "
        f"expires {grant.expires_at.isoformat()}.</p>"
        f"{table}"
        '<p><a href="/account">Back to your account</a></p>'
    )


def _render_note_page(*, grant: GrantRow, path: str, title: str, note_type: str, body: str) -> str:
    heading = title or path
    escaped_path = html.escape(path)
    subtitle = f"{escaped_path} &middot; {html.escape(note_type)}" if note_type else escaped_path
    back_href = f"{VIEW_PATH}?{urlencode({'grant_id': grant.id})}"
    return (
        f"<h1>{html.escape(heading)}</h1>"
        f"<p>{subtitle}</p>"
        f"<pre>{html.escape(body)}</pre>"
        f'<p><a href="{html.escape(back_href)}">Back to the list</a></p>'
    )


def break_glass_viewer_routes(session_cookie: str) -> list[Route]:
    """`GET /account/admin/break-glass/view` and `GET .../view/note` - mounted
    by `account.routes.page_routes` alongside the rest of the page. Takes
    the session cookie's name as a parameter rather than importing it
    directly, the same shape `account.admin.admin_routes`/`account.
    break_glass.break_glass_routes` already use."""

    async def _view(request: Request) -> Response:
        authorized = await _authorize_viewer(request, session_cookie=session_cookie)
        if isinstance(authorized, Response):
            return authorized
        pool, app_role, session = authorized.pool, authorized.app_role, authorized.session

        grant_id = _parse_grant_id(request.query_params.get("grant_id", ""))
        if grant_id is None:
            return PlainTextResponse("grant_id must be an integer.", status_code=400)

        grant = await _active_grant(pool, app_role=app_role, session=session, grant_id=grant_id)
        if grant is None:
            return PlainTextResponse(
                f"No active break-glass grant {grant_id} for this admin.", status_code=403
            )
        if session.oid is None:  # pragma: no cover - defensive, see module docstring
            # `_authorize_viewer` already required `ADMIN_ROLE in session.roles`, which
            # Entra only ever grants to a real `oid` (same reasoning `account.admin.
            # _admin_identity`'s own identical check gives).
            return PlainTextResponse(
                "The break-glass viewer requires an Entra identity.", status_code=403
            )

        async with (
            pool.acquire() as conn,
            rls.request_identity(
                conn, role=app_role, oid=session.oid, roles=session.roles, break_glass=grant.id
            ) as identified,
        ):
            rows = await identified.fetch(
                "select path, content, updated_at from vault_notes where namespace = $1 "
                "order by path",
                grant.alias,
            )

        await AuditWriter(pool).record(
            actor=session.subject,
            client=_ADMIN_CLIENT,
            op=_AUDIT_OP,
            path=None,
            commit_sha=None,
            outcome="ok",
            detail={"grant_id": grant.id, "namespace": grant.alias, "scope": "list"},
        )

        entries = [
            (row["path"], *_note_frontmatter(bytes(row["content"])), row["updated_at"])
            for row in rows
        ]
        body = _render_list_page(entries, grant=grant)
        return account_response(_html_page("Break-glass viewer", body))

    async def _view_note(request: Request) -> Response:
        authorized = await _authorize_viewer(request, session_cookie=session_cookie)
        if isinstance(authorized, Response):
            return authorized
        pool, app_role, session = authorized.pool, authorized.app_role, authorized.session

        grant_id = _parse_grant_id(request.query_params.get("grant_id", ""))
        if grant_id is None:
            return PlainTextResponse("grant_id must be an integer.", status_code=400)
        path = request.query_params.get("path", "")
        if not path:
            return PlainTextResponse("path must not be empty.", status_code=400)

        grant = await _active_grant(pool, app_role=app_role, session=session, grant_id=grant_id)
        if grant is None:
            return PlainTextResponse(
                f"No active break-glass grant {grant_id} for this admin.", status_code=403
            )
        if session.oid is None:  # pragma: no cover - defensive, see `_view`'s identical check
            return PlainTextResponse(
                "The break-glass viewer requires an Entra identity.", status_code=403
            )

        async with (
            pool.acquire() as conn,
            rls.request_identity(
                conn, role=app_role, oid=session.oid, roles=session.roles, break_glass=grant.id
            ) as identified,
        ):
            row = await identified.fetchrow(
                "select content from vault_notes where namespace = $1 and path = $2",
                grant.alias,
                path,
            )
        if row is None:
            return PlainTextResponse(
                f"No note at path {path!r} in this namespace.", status_code=404
            )

        await AuditWriter(pool).record(
            actor=session.subject,
            client=_ADMIN_CLIENT,
            op=_AUDIT_OP,
            path=path,
            commit_sha=None,
            outcome="ok",
            detail={"grant_id": grant.id, "namespace": grant.alias, "scope": "note"},
        )

        title, note_type, body_text = _note_body(bytes(row["content"]))
        body = _render_note_page(
            grant=grant, path=path, title=title, note_type=note_type, body=body_text
        )
        return account_response(_html_page("Break-glass viewer", body))

    return [
        Route(VIEW_PATH, endpoint=_view, methods=["GET"]),
        Route(NOTE_PATH, endpoint=_view_note, methods=["GET"]),
    ]
