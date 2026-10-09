# SPDX-License-Identifier: AGPL-3.0-only
"""The `/account` section registry (#229, ADR-0008 addendum 2026-10-08).

`/account/routes.py`'s page shell renders `DEFAULT_SECTIONS` in order; each
`Section.enabled` decides, per request, whether it shows up at all -
`account.routes` never special-cases a section by name. This is the
extension point `GitHub #229`'s own issue text names for F-02's token
section (#135) and agent approvals (#179): a later work package appends its
own `Section` to this tuple, the shell itself never changes.

`OVERVIEW_SECTION` is the one section this task builds: identity (subject,
login mode, roles) plus, only with `STORAGE_BACKEND=postgres` *and* an Entra
`oid` (the `entra` login mode is the only one with a personal namespace at
all - `password`/`oidc` resolve namespaces from `LOGIN_NAMESPACES`/
`LOGIN_NAMESPACE_MAP` instead, ADR-0004, never through the ADR-0008
registry), the note count of the caller's own `me` namespace.

That count is computed without the MCP request path's own machinery
(`mcp.namespaces.resolve`, `db.rls.request_connection`): both read the
calling principal from `mcp.server.auth.middleware.auth_context.
get_access_token()`'s contextvar, which a plain cookie-authenticated
`/account` request never populates - there is no bearer token here at all.
`db.rls.request_identity` is the one piece of that machinery that takes an
identity as a plain argument instead of reading it from context, so this
module calls it directly, the same `mm_ensure_personal_ns()` the real
request path calls, then counts `vault_notes` the same way `storage.
postgres.PostgresBackend.namespace_usage`'s own query does (`left(path, 9)
<> '_archive/'` - archived notes count toward size, not toward count,
`#243`'s own rule, repeated here rather than imported since that query is
private to `storage.postgres`).
"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import asyncpg

from memory_manager.account.admin import ADMIN_ROLE, list_namespaces, render_admin_section
from memory_manager.account.break_glass import list_grants as list_break_glass_grants
from memory_manager.account.break_glass import render_break_glass_section
from memory_manager.account.break_glass_notice import list_notices as list_break_glass_notices
from memory_manager.account.break_glass_notice import render_break_glass_notice
from memory_manager.account.break_glass_viewer import VIEW_PATH as BREAK_GLASS_VIEW_PATH
from memory_manager.account.delete import (
    CONFIRM_FIELD_NAME,
    CONFIRM_PHRASE,
    CSRF_FORM_DELETE,
    DELETE_PATH,
)
from memory_manager.account.export import CSRF_FORM_EXPORT, EXPORT_PATH
from memory_manager.account.sessions import SessionInfo, csrf_token
from memory_manager.account.templates import CSRF_FIELD_NAME
from memory_manager.db import rls

__all__ = [
    "ADMIN_SECTION",
    "BREAK_GLASS_SECTION",
    "DEFAULT_SECTIONS",
    "DELETE_SECTION",
    "EXPORT_SECTION",
    "OVERVIEW_SECTION",
    "Section",
    "SectionContext",
    "render_sections",
]

#: `left(path, 9) <> '_archive/'` - `len("_archive/")`, the same literal
#: `storage.postgres`'s own `_SELECT_NAMESPACE_USAGE` filters archived notes with.
_ARCHIVE_PREFIX_LENGTH = 9


@dataclass(frozen=True)
class SectionContext:
    """What a `Section.render` call sees: the caller's own session, the pool to query
    with (always available - `account.sessions` already requires one), and whether
    this deployment's vault backend is `STORAGE_BACKEND=postgres` (`routes.py` passes
    `isinstance(services.storage, storage.postgres.PostgresBackend)` straight through,
    the same check `http.py`'s own `storage_quota_checker` wiring uses)."""

    session: SessionInfo
    pool: asyncpg.Pool
    is_postgres_backend: bool
    #: `services.app_role` (`None` for `STORAGE_BACKEND=git`, ADR-0008 addendum) -
    #: required to call `mm_ensure_personal_ns()`/an `mm_admin_*` function under the
    #: identity switch `db.rls.request_identity` performs; `OVERVIEW_SECTION` and
    #: `ADMIN_SECTION` are its only two readers today.
    app_role: str | None
    #: The raw (plaintext) session cookie value - needed only to mint a per-form
    #: CSRF token (`account.sessions.csrf_token`, keyed by the raw id, never the
    #: stored hash); `EXPORT_SECTION`'s own form is the only reader today.
    session_id: str


@dataclass(frozen=True)
class Section:
    """One pluggable `/account` page section.

    `enabled` and `render` both take the same `SectionContext` - a section that needs
    nothing beyond identity (this task's `OVERVIEW_SECTION`) ignores most of it; a
    later enterprise-only section (export, delete-my-memory, the admin area, #230/
    #232/#234) gates itself with `ctx.is_postgres_backend` and otherwise never
    appears, without `routes.py` knowing that name at all.
    """

    name: str
    enabled: Callable[[SectionContext], bool]
    render: Callable[[SectionContext], Awaitable[str]]


async def _personal_note_count(ctx: SectionContext) -> int | None:
    """The caller's own `me` namespace note count, or `None` if it does not apply
    (not `STORAGE_BACKEND=postgres`, or a `password`/`oidc` session with no `oid` at
    all - see this module's docstring)."""
    if not ctx.is_postgres_backend or ctx.session.oid is None or ctx.app_role is None:
        return None
    async with (
        ctx.pool.acquire() as conn,
        rls.request_identity(
            conn, role=ctx.app_role, oid=ctx.session.oid, roles=ctx.session.roles
        ) as identified,
    ):
        alias = await identified.fetchval("select mm_ensure_personal_ns()")
        if alias is None:  # pragma: no cover - defensive, see mm_ensure_personal_ns's own docs
            return None
        count = await identified.fetchval(
            "select count(*) from vault_notes where namespace = $1 "
            "and left(path, $2) <> '_archive/'",
            alias,
            _ARCHIVE_PREFIX_LENGTH,
        )
        return int(count) if count is not None else 0


async def _break_glass_notice_html(ctx: SectionContext) -> str:
    """The break-glass banner every affected user sees until they acknowledge it
    (#239, `account.break_glass_notice`'s own module docstring) - the empty string
    under the same conditions `_personal_note_count` returns `None` for: there is no
    personal namespace, and so nothing to be notified about, without one."""
    if not ctx.is_postgres_backend or ctx.session.oid is None or ctx.app_role is None:
        return ""
    rows = await list_break_glass_notices(ctx.pool, app_role=ctx.app_role, session=ctx.session)
    return render_break_glass_notice(rows, session_id=ctx.session_id)


async def _render_overview(ctx: SectionContext) -> str:
    session = ctx.session
    rows = [
        f"<dt>Subject</dt><dd>{html.escape(session.subject)}</dd>",
        f"<dt>Login mode</dt><dd>{html.escape(session.login_mode)}</dd>",
    ]
    if session.roles:
        rows.append(f"<dt>Roles</dt><dd>{html.escape(', '.join(session.roles))}</dd>")
    note_count = await _personal_note_count(ctx)
    if note_count is not None:
        rows.append(f"<dt>Notes in your personal namespace</dt><dd>{note_count}</dd>")
    overview = f"<section><h2>Overview</h2><dl>{''.join(rows)}</dl></section>"
    return overview + await _break_glass_notice_html(ctx)


#: Always enabled - identity is shown regardless of login mode or storage backend;
#: the note-count row inside it is the part that only ever appears for
#: `STORAGE_BACKEND=postgres` with an Entra `oid` (`_personal_note_count`).
OVERVIEW_SECTION = Section(name="overview", enabled=lambda _ctx: True, render=_render_overview)


def _export_enabled(ctx: SectionContext) -> bool:
    """`STORAGE_BACKEND=postgres` plus an Entra `oid` - the same gate
    `_personal_note_count` uses: there is no personal namespace to export at all
    otherwise (#230, `account.export`'s own module docstring)."""
    return ctx.is_postgres_backend and ctx.session.oid is not None


async def _render_export(ctx: SectionContext) -> str:
    token = csrf_token(ctx.session_id, CSRF_FORM_EXPORT)
    return (
        "<section><h2>Export</h2>"
        "<p>Download every note in your personal namespace, archived notes "
        "included, as a Markdown ZIP.</p>"
        f'<form method="post" action="{html.escape(EXPORT_PATH)}">'
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{html.escape(token)}">'
        '<button type="submit">Download my memory</button>'
        "</form></section>"
    )


#: Enterprise only (`_export_enabled`) - a `password`/`oidc` session or a
#: `STORAGE_BACKEND=git` deployment never shows this section at all (#230).
EXPORT_SECTION = Section(name="export", enabled=_export_enabled, render=_render_export)


def _delete_enabled(ctx: SectionContext) -> bool:
    """Same gate as `_export_enabled`: there is no personal namespace to delete at
    all otherwise (#232, `account.delete`'s own module docstring)."""
    return ctx.is_postgres_backend and ctx.session.oid is not None


async def _render_delete(ctx: SectionContext) -> str:
    token = csrf_token(ctx.session_id, CSRF_FORM_DELETE)
    phrase = html.escape(CONFIRM_PHRASE)
    return (
        "<section><h2>Delete my memory</h2>"
        "<p>Permanently delete every note in your personal namespace, "
        "archived notes included. This cannot be undone.</p>"
        f'<form method="post" action="{html.escape(DELETE_PATH)}">'
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{html.escape(token)}">'
        f'<label>Type "<code>{phrase}</code>" to confirm:'
        f'<input type="text" name="{CONFIRM_FIELD_NAME}" autocomplete="off"></label>'
        '<button type="submit">Delete my memory</button>'
        "</form></section>"
    )


#: Enterprise only (`_delete_enabled`) - a `password`/`oidc` session or a
#: `STORAGE_BACKEND=git` deployment never shows this section at all (#232).
DELETE_SECTION = Section(name="delete", enabled=_delete_enabled, render=_render_delete)


def _admin_enabled(ctx: SectionContext) -> bool:
    """`STORAGE_BACKEND=postgres` plus an Entra `oid` plus `Memory.Admin` in the
    session's own roles (#234, ADR-0008 "`Memory.Admin` manages namespaces and
    ACLs"). `Memory.Admin` is only ever granted to a real Entra principal, so the
    `oid` check is never the binding one here - kept for the same "degenerate
    empty-oid case never reaches the database" reasoning `_export_enabled`/
    `_delete_enabled` already follow."""
    return (
        ctx.is_postgres_backend and ctx.session.oid is not None and ADMIN_ROLE in ctx.session.roles
    )


async def _render_admin(ctx: SectionContext) -> str:
    if ctx.app_role is None:  # pragma: no cover - defensive, `open_services` always sets it
        # alongside a `PostgresBackend` (`app.py`'s own docstring) - `_admin_enabled`
        # already required `is_postgres_backend`.
        return ""
    rows = await list_namespaces(ctx.pool, app_role=ctx.app_role, session=ctx.session)
    return render_admin_section(rows, session_id=ctx.session_id)


#: Admin-only (`_admin_enabled`) - absent for every non-admin session and for
#: `STORAGE_BACKEND=git` regardless of role (#234).
ADMIN_SECTION = Section(name="admin", enabled=_admin_enabled, render=_render_admin)


def _break_glass_enabled(ctx: SectionContext) -> bool:
    """Same gate as `_admin_enabled` (#237, ADR-0008 "Break-glass") - a
    break-glass grant is itself an admin action, never available to anyone
    without `Memory.Admin`."""
    return (
        ctx.is_postgres_backend and ctx.session.oid is not None and ADMIN_ROLE in ctx.session.roles
    )


async def _render_break_glass(ctx: SectionContext) -> str:
    if ctx.app_role is None:  # pragma: no cover - defensive, `open_services` always sets it
        # alongside a `PostgresBackend` (`app.py`'s own docstring) -
        # `_break_glass_enabled` already required `is_postgres_backend`.
        return ""
    rows = await list_break_glass_grants(ctx.pool, app_role=ctx.app_role, session=ctx.session)
    return render_break_glass_section(
        rows, session_id=ctx.session_id, view_path=BREAK_GLASS_VIEW_PATH
    )


#: Admin-only (`_break_glass_enabled`) - absent for every non-admin session and for
#: `STORAGE_BACKEND=git` regardless of role (#237).
BREAK_GLASS_SECTION = Section(
    name="break_glass", enabled=_break_glass_enabled, render=_render_break_glass
)

#: `routes.py` renders exactly these, in order - a later work package appends its own
#: `Section` here (this module's own docstring).
DEFAULT_SECTIONS: tuple[Section, ...] = (
    OVERVIEW_SECTION,
    EXPORT_SECTION,
    DELETE_SECTION,
    ADMIN_SECTION,
    BREAK_GLASS_SECTION,
)


async def render_sections(
    ctx: SectionContext, sections: tuple[Section, ...] = DEFAULT_SECTIONS
) -> str:
    """Every `enabled` section in `sections`, rendered in order and concatenated."""
    parts: list[str] = []
    for section in sections:
        if section.enabled(ctx):
            parts.append(await section.render(ctx))
    return "".join(parts)
