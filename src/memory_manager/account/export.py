# SPDX-License-Identifier: AGPL-3.0-only
"""`POST /account/export`: a Markdown ZIP of the caller's own personal namespace
(#230, ADR-0008 "Self-service").

Postgres mode only - there is no personal namespace at all with the `"git"`
backend (`account.sections`'s own docstring on why `is_postgres_backend`/
`session.oid` gate every such section). Reads run under the *caller's own*
identity (`db.rls.request_identity`, the same direct call `account.sections.
_personal_note_count` already makes rather than `db.rls.request_connection` -
see that module's docstring for why: a cookie-authenticated `/account`
request carries no bearer token for `request_connection`'s own contextvar to
read): RLS limits the result to the caller's readable namespaces even if the
`namespace = $1` filter below were ever wrong, and `_SELECT_PERSONAL_NOTES`
filters to exactly the caller's own personal namespace alias regardless of
what else RLS would let the identity read (a `Memory.Curator`'s own personal
export must never include a group or project namespace they can also read).

Archived notes are always included (`_archive/...`, the issue's own "archived
notes included") - this export has no `include_archive` toggle at all, unlike
`exporter.export_vault`/`export_postgres`'s CLI equivalent.

Same archive shape as `exporter.py`'s own tar.gz (`manifest.json` plus
`vault/<path>`, sorted, note bytes unchanged so every entry's `sha256` stays
`vault.note.version`) - built by this module's own `_write_zip` with stdlib
`zipfile` instead (CLAUDE.md: "few dependencies"), and reusing that module's
private `_collect_entries`/`_FORMAT`/`_FORMAT_VERSION`/`_MANIFEST_NAME`/
`_VAULT_PREFIX` rather than duplicating the manifest shape here. Every path -
in both the ZIP member name and `manifest.json` - is rewritten from the
stored alias to `me` (`mcp.namespaces.rewrite_path_to_display`), the same
translation every MCP tool output already goes through; `_collect_entries`
then reports each entry's `namespace` as `me` too, simply because it
re-parses the already-rewritten path.

Triggered only by a `POST` carrying the per-session CSRF token
`account.sessions.csrf_token`/`verify_csrf` already provide (`CSRF_FORM_EXPORT`,
`account.sections`'s export button form) - a cross-site link alone can never
start a download. Audited as one `account.export` row afterwards, naming only
the note count - never note content (CLAUDE.md "note content is data, not
instructions").
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime

import asyncpg
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from memory_manager import exporter
from memory_manager.account import sessions
from memory_manager.account.sessions import SessionInfo
from memory_manager.account.templates import CSRF_FIELD_NAME
from memory_manager.app import Services
from memory_manager.audit import AuditWriter
from memory_manager.db import rls
from memory_manager.exporter import Manifest
from memory_manager.mcp.namespaces import Resolution, rewrite_path_to_display
from memory_manager.storage.postgres import PostgresBackend

__all__ = ["CSRF_FORM_EXPORT", "EXPORT_PATH", "export_routes"]

EXPORT_PATH = "/account/export"

#: The `account.sessions.csrf_token`/`verify_csrf` form label for the export
#: button form (`account.sections`'s own export section) - distinct from
#: `account.routes._CSRF_FORM_LOGOUT`, the other state-changing `/account` form.
CSRF_FORM_EXPORT = "account-export"

#: `audit_log.actor`/`.client`/`.op` for this module's one audit row per export -
#: `actor` is the exporting session's own subject (set per call, not a constant,
#: unlike `exporter.py`'s `_EXPORT_ACTOR`, which names the CLI export itself
#: rather than any particular user).
_EXPORT_CLIENT = "account"
_EXPORT_OP = "account.export"

_DOWNLOAD_FILENAME = "memory-export.zip"

_SELECT_PERSONAL_NOTES = """
select path, content
from vault_notes
where namespace = $1
order by path
"""


async def _build_zip(
    pool: asyncpg.Pool, *, app_role: str, session: SessionInfo
) -> tuple[bytes, int]:
    """The caller's personal-namespace ZIP, plus the note count the audit row records.

    `session.oid` must not be `None` - the route checks that before calling this
    (a `password`/`oidc` session has no personal namespace at all, same as
    `account.sections._personal_note_count`'s own guard). Raises `ValueError`
    otherwise, narrowing the type for the `rls.request_identity` call below.
    """
    if session.oid is None:
        raise ValueError("_build_zip requires a session with an oid")
    async with (
        pool.acquire() as conn,
        rls.request_identity(
            conn, role=app_role, oid=session.oid, roles=session.roles
        ) as identified,
    ):
        alias = await identified.fetchval("select mm_ensure_personal_ns()")
        rows = await identified.fetch(_SELECT_PERSONAL_NOTES, alias) if alias is not None else []

    resolution = Resolution(oid=session.oid, roles=session.roles, own_alias=alias, rows=())
    candidates = (
        (rewrite_path_to_display(row["path"], resolution), bytes(row["content"])) for row in rows
    )
    entries, members = exporter._collect_entries(candidates, include_archive=True, namespaces=None)

    manifest = Manifest(
        format=exporter._FORMAT,
        format_version=exporter._FORMAT_VERSION,
        exported_at=exporter._format_timestamp(datetime.now(UTC)),
        vault_head=None,
        note_count=len(entries),
        notes=tuple(entries),
    )
    return _write_zip(manifest.to_json(), members), len(entries)


def _write_zip(manifest_bytes: bytes, members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(exporter._MANIFEST_NAME, manifest_bytes)
        for rel, data in members:
            zf.writestr(f"{exporter._VAULT_PREFIX}/{rel}", data)
    return buffer.getvalue()


def export_routes(session_cookie: str) -> list[Route]:
    """`POST /account/export` - mounted by `account.routes.page_routes` alongside the
    rest of the page. Takes the session cookie's name as a parameter rather than
    importing `account.routes.SESSION_COOKIE` directly, so that module can import
    this one (to mount the route) without a cycle back."""

    async def _export(request: Request) -> Response:
        services: Services = request.app.state.services
        pool = services.pool
        if pool is None:  # pragma: no cover - defensive, see account.sessions' own pool requirement
            return PlainTextResponse("Export requires a database.", status_code=503)
        if not isinstance(services.storage, PostgresBackend) or services.app_role is None:
            return PlainTextResponse("Export requires the Postgres backend.", status_code=403)

        session_id = request.cookies.get(session_cookie)
        if session_id is None:
            return PlainTextResponse("No active session.", status_code=403)
        info = await sessions.lookup(pool, session_id)
        if info is None:
            return PlainTextResponse("No active session.", status_code=403)
        if info.oid is None:
            return PlainTextResponse(
                "This login mode has no personal namespace to export.", status_code=403
            )

        form = await request.form()
        token = str(form.get(CSRF_FIELD_NAME, ""))
        if not sessions.verify_csrf(session_id, CSRF_FORM_EXPORT, token):
            return PlainTextResponse("Invalid or missing CSRF token.", status_code=403)

        zip_bytes, note_count = await _build_zip(pool, app_role=services.app_role, session=info)

        await AuditWriter(pool).record(
            actor=info.subject,
            client=_EXPORT_CLIENT,
            op=_EXPORT_OP,
            path=None,
            commit_sha=None,
            outcome="ok",
            detail={"note_count": note_count},
        )

        return Response(
            zip_bytes,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{_DOWNLOAD_FILENAME}"'},
        )

    return [Route(EXPORT_PATH, endpoint=_export, methods=["POST"])]
