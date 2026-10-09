# SPDX-License-Identifier: AGPL-3.0-only
"""Admin area on `/account`: namespace creation, project members, namespace
settings (#234, ADR-0008 "`Memory.Admin` manages namespaces and ACLs. It grants
no content access") and the "revoke access" action (#235).

Postgres mode only, like every enterprise `/account` section
(`account.sections`'s own docstring on `is_postgres_backend`/`session.oid`) -
there is no `namespaces` registry worth administering for the `"git"` backend
either, even though the table itself is declared in the common migration
chain (`0004_vault.sql`). Additionally gated on `ADMIN_ROLE in session.roles`:
a session without `Memory.Admin` never even sees this section, let alone
reaches one of its routes (`account.sections._admin_enabled`).

`NamespaceKind.CREATABLE`: the only two kinds an admin may ever create here
(ADR-0008 A2, "an admin step to create group and project namespaces") - never
`user` (always lazy, `mm_ensure_personal_ns()`) or `org` (a fixed, reserved
alias). One Python enum, so a later, additive kind (`agent`, ADR-0013) is a
one-line change here, not a rewrite of every call site that currently spells
out `"group"`/`"project"` by hand.

Every mutation goes through one of the five `mm_admin_*` `SECURITY DEFINER`
functions `migrations/0020_admin_namespaces.sql` adds - this module never
touches `namespaces`/`project_members`/`namespace_settings` with a bare
`INSERT`/`UPDATE`/`DELETE`, the same "no general grant on the registry"
shape `mm_ensure_personal_ns()` already established. Each call runs under
the *caller's own* identity (`db.rls.request_identity`, the same direct call
`account.export`/`account.delete`/`account.sections` already make rather
than `db.rls.request_connection` - those modules' own docstrings explain why
a cookie-authenticated `/account` request carries no bearer token for that
contextvar to read), so the SQL function's own `'Memory.Admin' = any(app.
roles)` check sees the real caller, never the owner. That SQL check is the
second, independent enforcement of the role (CLAUDE.md "enforced twice, in
Python and in SQL") - `_authorize_admin_form` below is the first, in Python,
and the one every route actually relies on to return a clean 403 rather than
ever reaching the database at all for a non-admin.

Alias validation (`_validate_alias`) mirrors `migrate_git.py`'s own
`_validate_alias` almost exactly - same charset, same reserved set, extended
with the `agent-` prefix ADR-0013 reserves for a namespace kind that does not
exist yet (`docs/adr/0013-agent-identity.md`: "its default namespace is an
alias `agent-<name>`"). Checked here, in Python, *before* the SQL call - the
migration's own `namespaces_alias_format` CHECK constraint is the second,
independent enforcement of the charset (not the reserved words, which are a
Python-only policy, same as `migrate_git.py`'s).

Every change is audited as one `admin.namespace.*`/`admin.member.*`/
`admin.settings.*` row (the issue's own op names) - metadata only (kind,
alias, external_key, principal, role, the write-mode strings), never note
content, since none of these operations ever touch one.

"Revoke access" (#235) is a different shape from the namespace actions above:
it never calls a `mm_admin_*` SQL function at all, since its two targets -
`oauth_tokens`/`static_tokens` (`auth.users.revoke_all_credentials`) and
`account_sessions` (`account.sessions.revoke_all_for_oid`) - carry no row-level
security in the first place (only the vault's own content tables do,
`migrations/0005_rls.sql`); it runs both directly against `services.pool`, the
same way `auth.users.disable_user` already does, rather than through
`_admin_identity`'s caller-scoped connection. `_authorize_admin_form` is still
the only gate: a non-admin session never reaches either call. It deliberately
calls `revoke_all_credentials`, not `disable_user` - this action ends every
session and token a user already holds without touching `users.disabled_at`,
so the user can sign in again right away (`auth.users.revoke_all_credentials`'s
own docstring: "reusable by WP-26's admin ... action without disabling the
user"); disabling the account outright is Entra's own job, surfaced here only
as a documented remedy for Entra's own propagation delay (ADR-0006 §5). The
reason is mandatory - free text, the same convention `disable_user`'s own
`reason` parameter already follows - and the action is audited as one
`admin.user.revoke` row (actor, target oid, reason, the revoked counts),
metadata only, same as every other admin action above.

"Erase" (#236) is a third shape again: unlike every action above, it never
writes its own `AuditWriter.record` row at all. It calls one of `storage.
erasure.erase_note`/`erase_namespace`/`erase_user` through `services.
storage.erase` (`storage.base.StorageBackend.erase`'s own dispatch on
`target_kind`) - the same primitive `account.delete`'s self-service flow
already calls (that module's own docstring: "unlike a read (export), the
erasure primitive's own audit row already is the complete, authoritative
record of this write"), so a second, admin-scoped audit row here would only
duplicate it. A note is identified by its vault path, not its ULID id - an
admin never sees an id to type - so `_resolve_note_id` resolves it first,
directly against the owner pool: an id is not content (CLAUDE.md "ids/
paths/counts" is explicitly allowed), the same bypass of row-level security
`PostgresBackend.erase` itself already makes. The typed confirmation is the
target identifier itself (path, alias or oid), compared byte for byte
against a separate `confirm` field - not a fixed phrase like `account.
delete.CONFIRM_PHRASE`, since the issue's own Implementation checklist asks
for "typed confirmation of the target". A mismatch, an empty `target` or an
empty `reason` all return a clean `400` before `storage.erase` is ever
called - nothing is deleted on any of those paths.
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum

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
from memory_manager.auth import users
from memory_manager.db import rls
from memory_manager.storage.base import ErasureTargetKind, NotFound
from memory_manager.storage.postgres import PostgresBackend

__all__ = [
    "ADD_MEMBER_PATH",
    "ADMIN_ROLE",
    "CONFIRM_FIELD_NAME",
    "CREATABLE_KINDS",
    "CREATE_NAMESPACE_PATH",
    "CSRF_FORM_ADD_MEMBER",
    "CSRF_FORM_CREATE_NAMESPACE",
    "CSRF_FORM_ERASE",
    "CSRF_FORM_REMOVE_MEMBER",
    "CSRF_FORM_RENAME_NAMESPACE",
    "CSRF_FORM_REVOKE_USER",
    "CSRF_FORM_UPDATE_SETTINGS",
    "ERASE_PATH",
    "REMOVE_MEMBER_PATH",
    "RENAME_NAMESPACE_PATH",
    "REVOKE_USER_PATH",
    "UPDATE_SETTINGS_PATH",
    "AdminActionError",
    "AdminFormError",
    "NamespaceKind",
    "NamespaceRow",
    "admin_routes",
    "list_namespaces",
    "render_admin_section",
]

#: ADR-0008: Entra app roles are tenant-wide (`Memory.User`, `Memory.Curator`,
#: `Memory.Admin`) - this module's own copy of the literal `mcp/namespaces.py`
#: keeps private to itself, same "every module names its own role constants"
#: shape that module already follows for `Memory.Curator`.
ADMIN_ROLE = "Memory.Admin"

CREATE_NAMESPACE_PATH = "/account/admin/namespaces"
RENAME_NAMESPACE_PATH = "/account/admin/namespaces/alias"
ADD_MEMBER_PATH = "/account/admin/members"
REMOVE_MEMBER_PATH = "/account/admin/members/remove"
UPDATE_SETTINGS_PATH = "/account/admin/settings"
REVOKE_USER_PATH = "/account/admin/users/revoke"
ERASE_PATH = "/account/admin/erase"

#: One distinct `account.sessions.csrf_token`/`verify_csrf` form label per
#: admin form - same "every state-changing form gets its own label" shape
#: every other `/account` form already follows (`account.routes.
#: _CSRF_FORM_LOGOUT`, `account.export.CSRF_FORM_EXPORT`, `account.delete.
#: CSRF_FORM_DELETE`).
CSRF_FORM_CREATE_NAMESPACE = "account-admin-create-namespace"
CSRF_FORM_RENAME_NAMESPACE = "account-admin-rename-namespace"
CSRF_FORM_ADD_MEMBER = "account-admin-add-member"
CSRF_FORM_REMOVE_MEMBER = "account-admin-remove-member"
CSRF_FORM_UPDATE_SETTINGS = "account-admin-update-settings"
CSRF_FORM_REVOKE_USER = "account-admin-revoke-user"
CSRF_FORM_ERASE = "account-admin-erase"

#: The erase form's typed-confirmation field - same field name `account.delete.
#: CONFIRM_FIELD_NAME` already uses for its own typed phrase, reused here for a
#: typed *target* instead (the issue's own "typed confirmation of the target").
CONFIRM_FIELD_NAME = "confirm"

_ADMIN_CLIENT = "account"
_ACCOUNT_PATH = "/account"

#: `storage.base.ErasureTargetKind`'s own three literals, repeated as a
#: `frozenset` so `target_kind not in _ERASURE_TARGET_KINDS` narrows a plain
#: form string to that type for mypy - the same shape `storage.erasure_replay.
#: _TARGET_KINDS` already uses for the same literal.
_ERASURE_TARGET_KINDS: frozenset[ErasureTargetKind] = frozenset({"note", "namespace", "user"})

# Same charset as a namespace path segment (`vault/paths.py`'s own
# `_NAMESPACE_RE`, `migrate_git.py`'s own `_ALIAS_RE`) - an alias becomes a
# path segment once a note is ever written under it.
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
#: `me`/`org` (ADR-0008 A2: "`org` is reserved") - exact literals, never a prefix.
_RESERVED_ALIASES = frozenset({"me", "org"})
#: `mm_ensure_personal_ns()` always stores a personal namespace's alias under
#: this prefix (`mcp/namespaces.py`'s own `_INTERNAL_ALIAS_PREFIX`) - an admin
#: must never be able to claim or hijack one by creating a `u-*` alias by hand.
_INTERNAL_ALIAS_PREFIX = "u-"
#: ADR-0013 (`agent-identity`): a future `agent` token's default namespace is
#: always `agent-<name>` - reserved now so no admin-created alias can collide
#: with one that kind mints later.
_AGENT_ALIAS_PREFIX = "agent-"


class NamespaceKind(StrEnum):
    """The registry's `kind` column (`namespaces.kind` CHECK,
    `0004_vault.sql`), as one Python enum (this module's own docstring) -
    `USER`/`ORG` exist in the registry but are never admin-created, so
    `CREATABLE` below is the subset `mm_admin_create_namespace`'s own `kind
    not in ('group', 'project')` check accepts."""

    USER = "user"
    GROUP = "group"
    PROJECT = "project"
    ORG = "org"


#: `account/admin.py`'s own namespace-creation form only ever offers these two -
#: additive the day `agent` (ADR-0013) needs its own admin-creatable kind too.
CREATABLE_KINDS: tuple[NamespaceKind, ...] = (NamespaceKind.GROUP, NamespaceKind.PROJECT)

_PRINCIPAL_KINDS = ("user", "group")
_PROJECT_ROLES = ("reader", "writer", "owner")
_GROUP_WRITE_MODES = ("members", "curators")
_PROJECT_WRITE_MODES = ("readers", "writers")


class AdminFormError(ValueError):
    """A submitted admin form is invalid (bad kind, bad alias, ...) - reported to
    the caller as `400`, before any database round trip at all."""


class AdminActionError(Exception):
    """One `mm_admin_*` call failed in a way this module recognizes and reports
    with a clean status code - anything else propagates as an unhandled
    exception (a `500`), the same "only map the errors we actually expect"
    rule every other `/account` route in this codebase already follows by
    not catching anything at all."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class NamespaceRow:
    """One row `mm_admin_list_namespaces()` reports - counts only, never content
    (this module's own docstring, CLAUDE.md "counts only, never content")."""

    id: int
    kind: str
    external_key: str
    alias: str | None
    note_count: int


def _validate_alias(alias: str) -> None:
    """Raise `AdminFormError` for anything `mm_admin_create_namespace`/
    `mm_admin_rename_namespace_alias` would otherwise reject with a less
    friendly `CheckViolationError` - see this module's docstring for why
    each check here also has an independent enforcement in SQL."""
    if alias.startswith(_INTERNAL_ALIAS_PREFIX):
        raise AdminFormError(
            f"alias {alias!r} starts with {_INTERNAL_ALIAS_PREFIX!r}, which is "
            "reserved for personal namespaces"
        )
    if alias.startswith(_AGENT_ALIAS_PREFIX):
        raise AdminFormError(
            f"alias {alias!r} starts with {_AGENT_ALIAS_PREFIX!r}, which is reserved"
        )
    if alias in _RESERVED_ALIASES:
        raise AdminFormError(f"alias {alias!r} is reserved")
    if not _ALIAS_RE.match(alias):
        raise AdminFormError(
            f"alias {alias!r} does not match ^[a-z0-9][a-z0-9-]{{0,39}}$ - use "
            "lower-case letters, digits and hyphens only, max 40 chars"
        )


def _map_admin_error(exc: asyncpg.PostgresError) -> AdminActionError:
    """Turn the handful of Postgres errors `migrations/0020_admin_namespaces.sql`'s
    own functions can raise into a clean `AdminActionError` - re-raises anything
    else unchanged (this module's own docstring)."""
    if isinstance(exc, asyncpg.InsufficientPrivilegeError):
        return AdminActionError("Admin actions require the Memory.Admin role.", status_code=403)
    if isinstance(exc, asyncpg.UniqueViolationError):
        return AdminActionError("That alias or namespace already exists.", status_code=400)
    if isinstance(exc, asyncpg.CheckViolationError):
        return AdminActionError(
            "Alias must be lower-case letters, digits and hyphens, max 40 characters.",
            status_code=400,
        )
    if isinstance(exc, asyncpg.InvalidParameterValueError | asyncpg.NoDataFoundError):
        return AdminActionError(str(exc), status_code=400)
    raise exc


@asynccontextmanager
async def _admin_identity(
    pool: asyncpg.Pool, *, app_role: str, session: SessionInfo
) -> AsyncIterator[asyncpg.pool.PoolConnectionProxy | asyncpg.Connection]:
    """`rls.request_identity` for `session`, with every `mm_admin_*` error this
    module recognizes mapped to `AdminActionError` (`_map_admin_error`).

    `session.oid` must not be `None` - every route checks that in
    `_authorize_admin_form` before this is ever called (`Memory.Admin` is only
    ever granted to a real Entra principal, ADR-0008: "Entra app roles are
    tenant-wide").
    """
    if session.oid is None:  # pragma: no cover - defensive, see docstring above
        raise AdminActionError("Admin actions require an Entra identity.", status_code=403)
    try:
        async with (
            pool.acquire() as conn,
            rls.request_identity(
                conn, role=app_role, oid=session.oid, roles=session.roles
            ) as identified,
        ):
            yield identified
    except asyncpg.PostgresError as exc:
        raise _map_admin_error(exc) from exc


async def list_namespaces(
    pool: asyncpg.Pool, *, app_role: str, session: SessionInfo
) -> list[NamespaceRow]:
    """Every namespace plus its note count, for the admin section's own listing
    (`mm_admin_list_namespaces()`) - never note content."""
    async with _admin_identity(pool, app_role=app_role, session=session) as conn:
        rows = await conn.fetch(
            "select id, kind, external_key, alias, note_count from mm_admin_list_namespaces()"
        )
    return [
        NamespaceRow(
            id=row["id"],
            kind=row["kind"],
            external_key=row["external_key"],
            alias=row["alias"],
            note_count=row["note_count"],
        )
        for row in rows
    ]


@dataclass(frozen=True)
class _Authorized:
    pool: asyncpg.Pool
    app_role: str
    session: SessionInfo
    form: FormData
    #: `services.storage` itself - `_authorize_admin_form`'s own `isinstance`
    #: check already narrowed it to `PostgresBackend`; `_erase` is the one
    #: route that needs it directly, for `storage.erase` (`erase_namespace`/
    #: `erase_user`/`erase_note`'s own entry point), rather than one more
    #: `mm_admin_*` SQL function.
    storage: PostgresBackend


async def _authorize_admin_form(
    request: Request, *, session_cookie: str, csrf_form: str
) -> _Authorized | Response:
    """The checks every admin route makes before touching the database: a
    Postgres-backend deployment, an active session, `ADMIN_ROLE` in that
    session's roles, and a valid per-form CSRF token - in that order, same
    shape `account.delete`/`account.export`'s own route handlers already
    follow. Returns a `Response` to return immediately on the first failure,
    or the parsed form plus everything the caller needs to act on it.

    This is the first, Python-side enforcement of `Memory.Admin`
    (module docstring) - a non-admin session never reaches a single
    `mm_admin_*` call, let alone that function's own, independent check.
    """
    services: Services = request.app.state.services
    pool = services.pool
    if pool is None:  # pragma: no cover - defensive, see account.sessions' own pool requirement
        return PlainTextResponse("Admin actions require a database.", status_code=503)
    if not isinstance(services.storage, PostgresBackend) or services.app_role is None:
        return PlainTextResponse("Admin actions require the Postgres backend.", status_code=403)
    storage = services.storage

    session_id = request.cookies.get(session_cookie)
    if session_id is None:
        return PlainTextResponse("No active session.", status_code=403)
    info = await sessions.lookup(pool, session_id)
    if info is None:
        return PlainTextResponse("No active session.", status_code=403)
    if info.oid is None or ADMIN_ROLE not in info.roles:
        return PlainTextResponse("Admin actions require the Memory.Admin role.", status_code=403)

    form = await request.form()
    token = str(form.get(CSRF_FIELD_NAME, ""))
    if not sessions.verify_csrf(session_id, csrf_form, token):
        return PlainTextResponse("Invalid or missing CSRF token.", status_code=403)

    return _Authorized(
        pool=pool, app_role=services.app_role, session=info, form=form, storage=storage
    )


async def _resolve_note_id(pool: asyncpg.Pool, path: str) -> str | None:
    """`vault_notes.id` for `path` - the ULID `storage.erasure.erase_note`
    actually matches on, `path` itself being unique there (`0004_vault.sql`).

    A direct, owner-level lookup, the same bypass of row-level security
    `PostgresBackend.erase` itself already makes (`storage/postgres.py`'s own
    docstring: "erasure always runs as the owner ... the app role holds no
    grant at all") - `Memory.Admin` grants no content access (ADR-0008), but
    an id is not content (CLAUDE.md: "admins never see content, only ids/
    paths/counts"). `None` if nothing exists at that exact path - `_erase`
    reports that as a clean `404` before ever calling `storage.erase`.
    """
    note_id = await pool.fetchval("select id from vault_notes where path = $1", path)
    return str(note_id) if note_id is not None else None


def admin_routes(session_cookie: str) -> list[Route]:
    """Every `POST /account/admin/...` route - mounted by `account.routes.
    page_routes` alongside the rest of the page. Takes the session cookie's name
    as a parameter rather than importing it directly, the same shape `account.
    export.export_routes`/`account.delete.delete_routes` already use."""

    async def _create_namespace(request: Request) -> Response:
        authorized = await _authorize_admin_form(
            request, session_cookie=session_cookie, csrf_form=CSRF_FORM_CREATE_NAMESPACE
        )
        if isinstance(authorized, Response):
            return authorized
        pool, app_role, session, form = (
            authorized.pool,
            authorized.app_role,
            authorized.session,
            authorized.form,
        )

        kind = str(form.get("kind", ""))
        external_key = str(form.get("external_key", ""))
        alias = str(form.get("alias", ""))
        try:
            if kind not in CREATABLE_KINDS:
                raise AdminFormError(f"kind must be one of {tuple(CREATABLE_KINDS)}, got {kind!r}")
            if not external_key:
                raise AdminFormError("external_key must not be empty")
            _validate_alias(alias)
        except AdminFormError as exc:
            return PlainTextResponse(str(exc), status_code=400)

        try:
            async with _admin_identity(pool, app_role=app_role, session=session) as conn:
                await conn.fetchval(
                    "select mm_admin_create_namespace($1, $2, $3)", kind, external_key, alias
                )
        except AdminActionError as exc:
            return PlainTextResponse(str(exc), status_code=exc.status_code)

        await AuditWriter(pool).record(
            actor=session.subject,
            client=_ADMIN_CLIENT,
            op="admin.namespace.create",
            path=None,
            commit_sha=None,
            outcome="ok",
            detail={"kind": kind, "alias": alias},
        )
        return RedirectResponse(_ACCOUNT_PATH, status_code=302)

    async def _rename_namespace(request: Request) -> Response:
        authorized = await _authorize_admin_form(
            request, session_cookie=session_cookie, csrf_form=CSRF_FORM_RENAME_NAMESPACE
        )
        if isinstance(authorized, Response):
            return authorized
        pool, app_role, session, form = (
            authorized.pool,
            authorized.app_role,
            authorized.session,
            authorized.form,
        )

        old_alias = str(form.get("old_alias", ""))
        new_alias = str(form.get("new_alias", ""))
        try:
            _validate_alias(new_alias)
        except AdminFormError as exc:
            return PlainTextResponse(str(exc), status_code=400)

        try:
            async with _admin_identity(pool, app_role=app_role, session=session) as conn:
                await conn.execute(
                    "select mm_admin_rename_namespace_alias($1, $2)", old_alias, new_alias
                )
        except AdminActionError as exc:
            return PlainTextResponse(str(exc), status_code=exc.status_code)

        await AuditWriter(pool).record(
            actor=session.subject,
            client=_ADMIN_CLIENT,
            op="admin.namespace.rename",
            path=None,
            commit_sha=None,
            outcome="ok",
            detail={"old_alias": old_alias, "new_alias": new_alias},
        )
        return RedirectResponse(_ACCOUNT_PATH, status_code=302)

    async def _add_member(request: Request) -> Response:
        authorized = await _authorize_admin_form(
            request, session_cookie=session_cookie, csrf_form=CSRF_FORM_ADD_MEMBER
        )
        if isinstance(authorized, Response):
            return authorized
        pool, app_role, session, form = (
            authorized.pool,
            authorized.app_role,
            authorized.session,
            authorized.form,
        )

        alias = str(form.get("alias", ""))
        principal_kind = str(form.get("principal_kind", ""))
        principal_id = str(form.get("principal_id", ""))
        role = str(form.get("role", ""))
        if principal_kind not in _PRINCIPAL_KINDS:
            return PlainTextResponse(
                f"principal_kind must be one of {_PRINCIPAL_KINDS}, got {principal_kind!r}",
                status_code=400,
            )
        if role not in _PROJECT_ROLES:
            return PlainTextResponse(
                f"role must be one of {_PROJECT_ROLES}, got {role!r}", status_code=400
            )

        try:
            async with _admin_identity(pool, app_role=app_role, session=session) as conn:
                await conn.execute(
                    "select mm_admin_add_project_member($1, $2, $3, $4)",
                    alias,
                    principal_kind,
                    principal_id,
                    role,
                )
        except AdminActionError as exc:
            return PlainTextResponse(str(exc), status_code=exc.status_code)

        await AuditWriter(pool).record(
            actor=session.subject,
            client=_ADMIN_CLIENT,
            op="admin.member.add",
            path=None,
            commit_sha=None,
            outcome="ok",
            detail={
                "alias": alias,
                "principal_kind": principal_kind,
                "principal_id": principal_id,
                "role": role,
            },
        )
        return RedirectResponse(_ACCOUNT_PATH, status_code=302)

    async def _remove_member(request: Request) -> Response:
        authorized = await _authorize_admin_form(
            request, session_cookie=session_cookie, csrf_form=CSRF_FORM_REMOVE_MEMBER
        )
        if isinstance(authorized, Response):
            return authorized
        pool, app_role, session, form = (
            authorized.pool,
            authorized.app_role,
            authorized.session,
            authorized.form,
        )

        alias = str(form.get("alias", ""))
        principal_kind = str(form.get("principal_kind", ""))
        principal_id = str(form.get("principal_id", ""))
        if principal_kind not in _PRINCIPAL_KINDS:
            return PlainTextResponse(
                f"principal_kind must be one of {_PRINCIPAL_KINDS}, got {principal_kind!r}",
                status_code=400,
            )

        try:
            async with _admin_identity(pool, app_role=app_role, session=session) as conn:
                await conn.execute(
                    "select mm_admin_remove_project_member($1, $2, $3)",
                    alias,
                    principal_kind,
                    principal_id,
                )
        except AdminActionError as exc:
            return PlainTextResponse(str(exc), status_code=exc.status_code)

        await AuditWriter(pool).record(
            actor=session.subject,
            client=_ADMIN_CLIENT,
            op="admin.member.remove",
            path=None,
            commit_sha=None,
            outcome="ok",
            detail={"alias": alias, "principal_kind": principal_kind, "principal_id": principal_id},
        )
        return RedirectResponse(_ACCOUNT_PATH, status_code=302)

    async def _update_settings(request: Request) -> Response:
        authorized = await _authorize_admin_form(
            request, session_cookie=session_cookie, csrf_form=CSRF_FORM_UPDATE_SETTINGS
        )
        if isinstance(authorized, Response):
            return authorized
        pool, app_role, session, form = (
            authorized.pool,
            authorized.app_role,
            authorized.session,
            authorized.form,
        )

        alias = str(form.get("alias", ""))
        group_write_raw = str(form.get("group_write", ""))
        project_write_raw = str(form.get("project_write", ""))
        group_write = group_write_raw or None
        project_write = project_write_raw or None
        if group_write is not None and group_write not in _GROUP_WRITE_MODES:
            return PlainTextResponse(
                f"group_write must be one of {_GROUP_WRITE_MODES}, got {group_write!r}",
                status_code=400,
            )
        if project_write is not None and project_write not in _PROJECT_WRITE_MODES:
            return PlainTextResponse(
                f"project_write must be one of {_PROJECT_WRITE_MODES}, got {project_write!r}",
                status_code=400,
            )

        try:
            async with _admin_identity(pool, app_role=app_role, session=session) as conn:
                await conn.execute(
                    "select mm_admin_update_namespace_settings($1, $2, $3)",
                    alias,
                    group_write,
                    project_write,
                )
        except AdminActionError as exc:
            return PlainTextResponse(str(exc), status_code=exc.status_code)

        await AuditWriter(pool).record(
            actor=session.subject,
            client=_ADMIN_CLIENT,
            op="admin.settings.update",
            path=None,
            commit_sha=None,
            outcome="ok",
            detail={"alias": alias, "group_write": group_write, "project_write": project_write},
        )
        return RedirectResponse(_ACCOUNT_PATH, status_code=302)

    async def _revoke_user(request: Request) -> Response:
        authorized = await _authorize_admin_form(
            request, session_cookie=session_cookie, csrf_form=CSRF_FORM_REVOKE_USER
        )
        if isinstance(authorized, Response):
            return authorized
        pool, session, form = authorized.pool, authorized.session, authorized.form

        target_oid = str(form.get("oid", "")).strip()
        reason = str(form.get("reason", "")).strip()
        if not target_oid:
            return PlainTextResponse("oid must not be empty.", status_code=400)
        if not reason:
            return PlainTextResponse("reason must not be empty.", status_code=400)

        counts = await users.revoke_all_credentials(pool, target_oid)
        sessions_revoked = await sessions.revoke_all_for_oid(pool, target_oid)

        await AuditWriter(pool).record(
            actor=session.subject,
            client=_ADMIN_CLIENT,
            op="admin.user.revoke",
            path=None,
            commit_sha=None,
            outcome="ok",
            detail={
                "target_oid": target_oid,
                "reason": reason,
                "oauth_tokens_revoked": counts.oauth_tokens,
                "static_tokens_revoked": counts.static_tokens,
                "account_sessions_revoked": sessions_revoked,
            },
        )
        return RedirectResponse(_ACCOUNT_PATH, status_code=302)

    async def _erase(request: Request) -> Response:
        authorized = await _authorize_admin_form(
            request, session_cookie=session_cookie, csrf_form=CSRF_FORM_ERASE
        )
        if isinstance(authorized, Response):
            return authorized
        pool, storage, session, form = (
            authorized.pool,
            authorized.storage,
            authorized.session,
            authorized.form,
        )

        target_kind = str(form.get("target_kind", ""))
        target = str(form.get("target", "")).strip()
        confirm = str(form.get(CONFIRM_FIELD_NAME, ""))
        reason = str(form.get("reason", "")).strip()

        if target_kind not in _ERASURE_TARGET_KINDS:
            return PlainTextResponse(
                f"target_kind must be one of {sorted(_ERASURE_TARGET_KINDS)}, got {target_kind!r}",
                status_code=400,
            )
        if not target:
            return PlainTextResponse("target must not be empty.", status_code=400)
        if not reason:
            return PlainTextResponse("reason must not be empty.", status_code=400)
        if confirm != target:
            return PlainTextResponse(
                f'Confirmation did not match. Type "{target}" exactly to erase it.',
                status_code=400,
            )

        target_id = target
        if target_kind == "note":
            resolved = await _resolve_note_id(pool, target)
            if resolved is None:
                return PlainTextResponse(f"No note found at path {target!r}.", status_code=404)
            target_id = resolved

        try:
            await storage.erase(target_kind, target_id, actor=session.subject, reason=reason)
        except NotFound:
            return PlainTextResponse(f"{target_kind} {target!r} does not exist.", status_code=404)

        return RedirectResponse(_ACCOUNT_PATH, status_code=302)

    return [
        Route(CREATE_NAMESPACE_PATH, endpoint=_create_namespace, methods=["POST"]),
        Route(RENAME_NAMESPACE_PATH, endpoint=_rename_namespace, methods=["POST"]),
        Route(ADD_MEMBER_PATH, endpoint=_add_member, methods=["POST"]),
        Route(REMOVE_MEMBER_PATH, endpoint=_remove_member, methods=["POST"]),
        Route(UPDATE_SETTINGS_PATH, endpoint=_update_settings, methods=["POST"]),
        Route(REVOKE_USER_PATH, endpoint=_revoke_user, methods=["POST"]),
        Route(ERASE_PATH, endpoint=_erase, methods=["POST"]),
    ]


def _render_namespace_table(rows: list[NamespaceRow]) -> str:
    if not rows:
        return "<p>No namespaces yet.</p>"
    body_rows = "".join(
        "<tr>"
        f"<td>{html.escape(row.kind)}</td>"
        f"<td>{html.escape(row.alias) if row.alias is not None else ''}</td>"
        f"<td>{html.escape(row.external_key)}</td>"
        f"<td>{row.note_count}</td>"
        "</tr>"
        for row in rows
    )
    return (
        "<table><thead><tr><th>Kind</th><th>Alias</th><th>External key</th>"
        f"<th>Notes</th></tr></thead><tbody>{body_rows}</tbody></table>"
    )


def render_admin_section(rows: list[NamespaceRow], *, session_id: str) -> str:
    """The admin section's own markup: the namespace table plus one form per
    `mm_admin_*` mutation - called by `account.sections._render_admin`, which
    owns the `<section>` gating (`_admin_enabled`) this module does not decide
    on its own."""
    create_token = sessions.csrf_token(session_id, CSRF_FORM_CREATE_NAMESPACE)
    rename_token = sessions.csrf_token(session_id, CSRF_FORM_RENAME_NAMESPACE)
    add_member_token = sessions.csrf_token(session_id, CSRF_FORM_ADD_MEMBER)
    remove_member_token = sessions.csrf_token(session_id, CSRF_FORM_REMOVE_MEMBER)
    settings_token = sessions.csrf_token(session_id, CSRF_FORM_UPDATE_SETTINGS)
    revoke_user_token = sessions.csrf_token(session_id, CSRF_FORM_REVOKE_USER)
    erase_token = sessions.csrf_token(session_id, CSRF_FORM_ERASE)

    kind_options = "".join(
        f'<option value="{kind.value}">{kind.value}</option>' for kind in CREATABLE_KINDS
    )
    role_options = "".join(f'<option value="{role}">{role}</option>' for role in _PROJECT_ROLES)
    erasure_target_options = "".join(
        f'<option value="{kind}">{kind}</option>' for kind in sorted(_ERASURE_TARGET_KINDS)
    )

    return (
        "<section><h2>Admin</h2>"
        f"{_render_namespace_table(rows)}"
        "<h3>Create namespace</h3>"
        f'<form method="post" action="{html.escape(CREATE_NAMESPACE_PATH)}">'
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{html.escape(create_token)}">'
        f'<label>Kind <select name="kind">{kind_options}</select></label>'
        '<label>External key <input type="text" name="external_key"></label>'
        '<label>Alias <input type="text" name="alias"></label>'
        '<button type="submit">Create namespace</button>'
        "</form>"
        "<h3>Rename namespace alias</h3>"
        f'<form method="post" action="{html.escape(RENAME_NAMESPACE_PATH)}">'
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{html.escape(rename_token)}">'
        '<label>Current alias <input type="text" name="old_alias"></label>'
        '<label>New alias <input type="text" name="new_alias"></label>'
        '<button type="submit">Rename</button>'
        "</form>"
        "<h3>Add project member</h3>"
        f'<form method="post" action="{html.escape(ADD_MEMBER_PATH)}">'
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{html.escape(add_member_token)}">'
        '<label>Project alias <input type="text" name="alias"></label>'
        "<label>Principal kind "
        '<select name="principal_kind"><option value="user">user</option>'
        '<option value="group">group</option></select></label>'
        '<label>Principal id <input type="text" name="principal_id"></label>'
        f'<label>Role <select name="role">{role_options}</select></label>'
        '<button type="submit">Add member</button>'
        "</form>"
        "<h3>Remove project member</h3>"
        f'<form method="post" action="{html.escape(REMOVE_MEMBER_PATH)}">'
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{html.escape(remove_member_token)}">'
        '<label>Project alias <input type="text" name="alias"></label>'
        "<label>Principal kind "
        '<select name="principal_kind"><option value="user">user</option>'
        '<option value="group">group</option></select></label>'
        '<label>Principal id <input type="text" name="principal_id"></label>'
        '<button type="submit">Remove member</button>'
        "</form>"
        "<h3>Namespace settings</h3>"
        f'<form method="post" action="{html.escape(UPDATE_SETTINGS_PATH)}">'
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{html.escape(settings_token)}">'
        '<label>Alias <input type="text" name="alias"></label>'
        '<label>Group write <select name="group_write"><option value="">unchanged</option>'
        '<option value="members">members</option><option value="curators">curators</option>'
        "</select></label>"
        '<label>Project write <select name="project_write"><option value="">unchanged</option>'
        '<option value="readers">readers</option><option value="writers">writers</option>'
        "</select></label>"
        '<button type="submit">Update settings</button>'
        "</form>"
        "<h3>Revoke user access</h3>"
        f'<form method="post" action="{html.escape(REVOKE_USER_PATH)}">'
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{html.escape(revoke_user_token)}">'
        '<label>Entra object id <input type="text" name="oid"></label>'
        '<label>Reason <input type="text" name="reason"></label>'
        '<button type="submit">Revoke access</button>'
        "</form>"
        "<h3>Erase a note, a namespace or a user</h3>"
        f'<form method="post" action="{html.escape(ERASE_PATH)}">'
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{html.escape(erase_token)}">'
        f'<label>Target kind <select name="target_kind">{erasure_target_options}</select></label>'
        "<label>Target (note path / namespace alias / user oid) "
        '<input type="text" name="target"></label>'
        '<label>Reason <input type="text" name="reason"></label>'
        "<label>Type the target again to confirm "
        f'<input type="text" name="{CONFIRM_FIELD_NAME}"></label>'
        '<button type="submit">Erase</button>'
        "</form>"
        "</section>"
    )
