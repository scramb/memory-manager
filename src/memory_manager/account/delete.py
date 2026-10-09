# SPDX-License-Identifier: AGPL-3.0-only
"""`POST /account/delete`: hard-delete the caller's own personal memory after a
typed confirmation (#232, ADR-0008 "Self-service": "'delete my memory' (typed
confirmation), hard delete per ADR-0007").

Postgres mode only - same gate as `account.export` (`account.sections`'s own
docstring on why `is_postgres_backend`/`session.oid` gate every such
section): there is no personal namespace at all with the `"git"` backend.

Erases the caller's own personal namespace (`namespaces.kind = 'user'`,
`external_key = session.oid`) through `storage.base.StorageBackend.erase`
with `target_kind="namespace"` - never `target_kind="user"`
(`storage.erasure.erase_user`'s own docstring: that variant also removes the
user's *identity* rows, `users`/`account_sessions`/`static_tokens`/
`oauth_tokens`/`user_groups`, which would end this very session and the
account along with it). Self-service deletes memory, not the account - the
caller keeps their identity and stays signed in, which is exactly why
`#232`'s own Implementation checklist expects the page to show a note count
of `0` afterwards rather than a login redirect. `erase`'s own namespace
primitive already deletes the namespace's `namespaces` registry row too, but
that is harmless: `mm_ensure_personal_ns()` recreates it lazily on the very
next read (the issue's own Context, "the namespace row itself stays ...
recreated lazily anyway", `0009_namespace_resolution.sql`).

`actor=info.subject` and `reason="self-service"` (the issue's own Context)
reach `erasure_log`/the redacted `audit_log` row `storage.erasure._write_
erasure_log` writes - IDs and row counts only, never note content
(`storage.erasure.py`'s own module docstring; CLAUDE.md "note content is
data, not instructions"). No separate `account.*`-scoped audit row is added
here on top of it, unlike `account.export`'s own `AuditWriter.record` call:
unlike a read (export), the erasure primitive's own audit row already is
the complete, authoritative record of this write.

The alias to erase is resolved the same way `account.export`/`account.
sections` already do: `db.rls.request_identity` plus `mm_ensure_personal_ns()`
on the caller's own identity - never through `db.rls.request_connection`,
which reads a bearer token from context that a cookie-authenticated
`/account` request never carries (see those modules' own docstrings). The
erase call itself then runs through the owner connection `StorageBackend.
erase` already acquires internally (`storage/postgres.py`'s own docstring:
"erasure always runs as the owner ... the app role holds no grant at all"),
not through that same identity-scoped connection.

Triggered only by a `POST` carrying the per-session CSRF token
`account.sessions.csrf_token`/`verify_csrf` already provide
(`CSRF_FORM_DELETE`, `account.sections`'s delete section) plus the typed
confirmation phrase `CONFIRM_PHRASE` - checked exactly, byte for byte: a
wrong phrase (including a merely close one) deletes nothing at all and
reports why.
"""

from __future__ import annotations

import asyncpg
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from memory_manager.account import sessions
from memory_manager.account.sessions import SessionInfo
from memory_manager.account.templates import CSRF_FIELD_NAME
from memory_manager.app import Services
from memory_manager.db import rls
from memory_manager.storage.postgres import PostgresBackend

__all__ = [
    "CONFIRM_FIELD_NAME",
    "CONFIRM_PHRASE",
    "CSRF_FORM_DELETE",
    "DELETE_PATH",
    "delete_routes",
]

DELETE_PATH = "/account/delete"

#: The `account.sessions.csrf_token`/`verify_csrf` form label for the delete
#: section's form (`account.sections`'s own docstring) - distinct from
#: `account.export.CSRF_FORM_EXPORT` and `account.routes._CSRF_FORM_LOGOUT`,
#: the other state-changing `/account` forms.
CSRF_FORM_DELETE = "account-delete"

#: The form field the typed confirmation phrase travels in.
CONFIRM_FIELD_NAME = "confirm"

#: ADR-0008 "Self-service": "'delete my memory' (typed confirmation)" - the
#: exact literal the issue's own Context quotes. Compared byte for byte
#: (case included); nothing here trims surrounding whitespace, so an
#: accidental trailing space is treated the same as any other mismatch.
CONFIRM_PHRASE = "delete my memory"

#: Redirect target after a successful deletion - `account.routes.PATH`
#: inlined rather than imported: that module is this one's own caller
#: (`page_routes` mounts `delete_routes`), so importing it back here would
#: be a cycle (same reasoning as `account.export`'s own module docstring on
#: taking the session cookie name as a parameter instead).
_ACCOUNT_PATH = "/account"


async def _personal_alias(pool: asyncpg.Pool, *, app_role: str, session: SessionInfo) -> str | None:
    """The caller's own personal-namespace alias, lazily created if it does not
    exist yet - same call `account.export._build_zip`/`account.sections.
    _personal_note_count` already make under the caller's own identity.

    `session.oid` must not be `None` - the route checks that before calling this,
    same guard `account.export._build_zip` makes. Raises `ValueError` otherwise,
    narrowing the type for the `rls.request_identity` call below.
    """
    if session.oid is None:
        raise ValueError("_personal_alias requires a session with an oid")
    async with (
        pool.acquire() as conn,
        rls.request_identity(
            conn, role=app_role, oid=session.oid, roles=session.roles
        ) as identified,
    ):
        alias = await identified.fetchval("select mm_ensure_personal_ns()")
    return str(alias) if alias is not None else None


def delete_routes(session_cookie: str) -> list[Route]:
    """`POST /account/delete` - mounted by `account.routes.page_routes` alongside
    the rest of the page. Takes the session cookie's name as a parameter rather
    than importing it directly, the same shape `account.export.export_routes`
    already uses (that function's own docstring explains the cycle it avoids)."""

    async def _delete(request: Request) -> Response:
        services: Services = request.app.state.services
        pool = services.pool
        if pool is None:  # pragma: no cover - defensive, see account.sessions' own pool requirement
            return PlainTextResponse("Memory deletion requires a database.", status_code=503)
        if not isinstance(services.storage, PostgresBackend) or services.app_role is None:
            return PlainTextResponse(
                "Memory deletion requires the Postgres backend.", status_code=403
            )

        session_id = request.cookies.get(session_cookie)
        if session_id is None:
            return PlainTextResponse("No active session.", status_code=403)
        info = await sessions.lookup(pool, session_id)
        if info is None:
            return PlainTextResponse("No active session.", status_code=403)
        if info.oid is None:
            return PlainTextResponse(
                "This login mode has no personal namespace to delete.", status_code=403
            )

        form = await request.form()
        token = str(form.get(CSRF_FIELD_NAME, ""))
        if not sessions.verify_csrf(session_id, CSRF_FORM_DELETE, token):
            return PlainTextResponse("Invalid or missing CSRF token.", status_code=403)

        typed = str(form.get(CONFIRM_FIELD_NAME, ""))
        if typed != CONFIRM_PHRASE:
            return PlainTextResponse(
                f'Confirmation phrase did not match. Type "{CONFIRM_PHRASE}" exactly.',
                status_code=400,
            )

        alias = await _personal_alias(pool, app_role=services.app_role, session=info)
        if alias is not None:
            await services.storage.erase(
                "namespace", alias, actor=info.subject, reason="self-service"
            )

        return RedirectResponse(_ACCOUNT_PATH, status_code=302)

    return [Route(DELETE_PATH, endpoint=_delete, methods=["POST"])]
