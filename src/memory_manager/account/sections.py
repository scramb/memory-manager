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

from memory_manager.account.sessions import SessionInfo
from memory_manager.db import rls

__all__ = ["DEFAULT_SECTIONS", "OVERVIEW_SECTION", "Section", "SectionContext", "render_sections"]

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
    #: required to call `mm_ensure_personal_ns()` under the identity switch
    #: `db.rls.request_identity` performs; `OVERVIEW_SECTION` is the only reader today.
    app_role: str | None


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
    return f"<section><h2>Overview</h2><dl>{''.join(rows)}</dl></section>"


#: Always enabled - identity is shown regardless of login mode or storage backend;
#: the note-count row inside it is the part that only ever appears for
#: `STORAGE_BACKEND=postgres` with an Entra `oid` (`_personal_note_count`).
OVERVIEW_SECTION = Section(name="overview", enabled=lambda _ctx: True, render=_render_overview)

#: `routes.py` renders exactly these, in order - a later work package appends its own
#: `Section` here (this module's own docstring).
DEFAULT_SECTIONS: tuple[Section, ...] = (OVERVIEW_SECTION,)


async def render_sections(
    ctx: SectionContext, sections: tuple[Section, ...] = DEFAULT_SECTIONS
) -> str:
    """Every `enabled` section in `sections`, rendered in order and concatenated."""
    parts: list[str] = []
    for section in sections:
        if section.enabled(ctx):
            parts.append(await section.render(ctx))
    return "".join(parts)
