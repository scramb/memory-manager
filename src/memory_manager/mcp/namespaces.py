# SPDX-License-Identifier: AGPL-3.0-only
"""Resolve `me`/alias access and enforce ADR-0008's matrix in application code (#101).

This module is `mcp/server.py`'s own, independent computation of the ADR-0008
permission matrix (A2 + R2's "two independent computations" - Python here,
SQL in `migrations/0005_rls.sql`'s `mm_readable_ns`/`mm_writable_ns`). It never
trusts RLS to be the only thing standing between a caller and a namespace it
should not touch; it never relies on RLS either - a bug on either side is
still caught by the other.

`resolve` is the one DB round trip `mcp/server.py` makes per tool call, under
one `db.rls.request_connection` transaction (the request path's own identity
switch, #116): `select mm_ensure_personal_ns()` (lazy personal-namespace
creation, `migrations/0009_namespace_resolution.sql`) followed by `select *
from mm_principal_namespaces($1)` with the calling principal's own `groups`
claim. Its result, a `Resolution`, is then asked - entirely in Python, no
further query - what the caller may read (`readable`), write
(`writable`) and curate (`can_curate`): the exact ADR-0008 table, including
the per-namespace `group_write`/`project_write` settings and the
2026-10-07 addendum "curate requires write".

`rewrite_path_to_stored`/`rewrite_path_to_display` are the `me` <-> stored-alias
translation every tool input and output goes through (ADR-0008 A2: "`me`
always means the caller's personal namespace"). `to_stored` rejects an
internal `u-*` key outright - a client must always say `me`, never the
namespace id-derived alias `mm_ensure_personal_ns()` actually stores
(`PathRejected`, the same exception `vault.paths.parse_note_path` already
raises for every other unsafe path, mapped by `mcp/errors.py` the same way).
Both path-level functions are best-effort on anything that is not a parseable
note path (a bare ULID, for instance) - left unchanged for the deeper,
sharper error `storage.base`/`vault.paths` raise for it instead.

`author_oid_of` is the one piece `memory_archive`'s curate check needs that
`Resolution` cannot carry: a note's author, read fresh (its own
`request_connection`, under RLS) rather than cached in the resolution, since
it is per-note, not per-namespace (ADR-0008 addendum "curate is author-based",
#101/#115/#116: a note's author is the `author_oid` of its revision 1; `NULL`
- a note nobody's identity wrote, e.g. a system import - counts as foreign).
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from memory_manager.db import rls
from memory_manager.vault.paths import NotePath, PathRejected, parse_note_path

__all__ = [
    "ME_ALIAS",
    "Resolution",
    "author_oid_of",
    "resolve",
    "rewrite_path_to_display",
    "rewrite_path_to_stored",
]

#: The pseudo-alias every tool input/output uses for the caller's own personal
#: namespace, regardless of what `mm_ensure_personal_ns()` actually stored for
#: it (always `u-<namespace id>` in production; a test fixture may seed any
#: other string - the rewriting below does not care which).
ME_ALIAS = "me"

#: The prefix `mm_ensure_personal_ns()` always stores a personal namespace's
#: alias under - rejected as an *input* namespace unconditionally (ADR-0008
#: addendum: "internal keys `u-...` are rejected as input"), independent of
#: whether it happens to be the caller's own alias or anyone else's: a client
#: must always address its own personal namespace as `me`.
_INTERNAL_ALIAS_PREFIX = "u-"

_CURATOR_ROLE = "Memory.Curator"
_ADMIN_ROLE = "Memory.Admin"

_GROUP_KIND = "group"
_PROJECT_KIND = "project"
_ORG_KIND = "org"

#: `namespace_kind` labels ADR-0008 and #102 ask for, keyed by the registry's
#: own `kind` literal (`mm_principal_namespaces`'s `kind` column). Only
#: `'user'` renames - the personal namespace is always addressed as `me`, so
#: its result field says `'personal'`, never the registry's internal `'user'`
#: (ADR-0008 addendum, "additive field `namespace_kind`").
_KIND_LABELS = {
    "user": "personal",
    _GROUP_KIND: _GROUP_KIND,
    _PROJECT_KIND: _PROJECT_KIND,
    _ORG_KIND: _ORG_KIND,
}

_DEFAULT_GROUP_WRITE = "members"
_DEFAULT_PROJECT_WRITE = "writers"

_PROJECT_WRITER_ROLES = ("writer", "owner")


@dataclass(frozen=True)
class _NamespaceRow:
    """One row `mm_principal_namespaces` reported for the calling principal."""

    kind: str
    alias: str
    role: str | None
    group_write: str | None
    project_write: str | None


@dataclass(frozen=True)
class Resolution:
    """What the calling principal may read, write and curate (ADR-0008's matrix).

    `own_alias` is `None` only for the degenerate case `mm_ensure_personal_ns()`
    itself returns `None` for (an empty `oid`, `db.rls.Principal`'s own
    docstring) - every real caller `db.rls.request_connection` lets through
    carries a non-empty one. `rows` never contains a *foreign* personal
    namespace (`mm_principal_namespaces`'s own `personal` CTE only ever joins
    against the caller's own `oid`) - `own_alias` is the only path a personal
    namespace reaches `readable`/`writable`/`can_curate` through.
    """

    oid: str
    roles: tuple[str, ...]
    own_alias: str | None
    rows: tuple[_NamespaceRow, ...]

    def readable(self) -> set[str]:
        """Every namespace alias the caller may read, per ADR-0008's matrix.

        Personal: always the caller's own. Group/project: membership alone
        (`mm_principal_namespaces` only ever returns a row for a group the
        caller's own `groups` claim names, or a project it is a member of -
        see this module's docstring on the two independent computations).
        Org: only once the caller holds any memory role at all ("every user
        with a memory role") - `mm_principal_namespaces` itself returns the
        org row unconditionally for any active identity, unlike
        `mm_readable_ns()`'s own role check, so that filter has to happen
        here, in Python, not be assumed from the row's mere presence.
        """
        result: set[str] = set()
        if self.own_alias is not None:
            result.add(self.own_alias)
        for row in self.rows:
            is_org_with_role = row.kind == _ORG_KIND and bool(self.roles)
            if row.kind in (_GROUP_KIND, _PROJECT_KIND) or is_org_with_role:
                result.add(row.alias)
        return result

    def writable(self) -> set[str]:
        """Every namespace alias the caller may create/edit/supersede in.

        Group: `group_write` ('members', the default, or 'curators') gates
        every member alike - a `Memory.Curator` writes a 'curators'-only
        group even without it (ADR-0008's table says so explicitly). Project:
        `project_write` ('writers', the default, or 'readers') gates by the
        caller's own strongest project role. Org: `Memory.Curator` or
        `Memory.Admin` only. `group_write`/`project_write` come back `None`
        from the left join in `mm_principal_namespaces` when no
        `namespace_settings` row exists yet - treated as the column's own
        database default, exactly like `mm_writable_ns()`'s `coalesce` does.
        """
        result: set[str] = set()
        if self.own_alias is not None:
            result.add(self.own_alias)
        for row in self.rows:
            if row.kind == _GROUP_KIND:
                effective = row.group_write or _DEFAULT_GROUP_WRITE
                if effective == _DEFAULT_GROUP_WRITE or _CURATOR_ROLE in self.roles:
                    result.add(row.alias)
            elif row.kind == _PROJECT_KIND:
                effective = row.project_write or _DEFAULT_PROJECT_WRITE
                if effective == "readers" or row.role in _PROJECT_WRITER_ROLES:
                    result.add(row.alias)
            elif row.kind == _ORG_KIND:
                if _CURATOR_ROLE in self.roles or _ADMIN_ROLE in self.roles:
                    result.add(row.alias)
        return result

    def can_curate(self, alias: str) -> bool:
        """Whether the caller may curate (archive someone else's note in) `alias`.

        Requires `writable` first (ADR-0008 addendum 2026-10-07, #119: "curate
        requires write" - a `Memory.Curator` who may not write a namespace
        cannot curate it either), then the matrix's own, stricter curate
        column: the caller themself for `me` (trivially true - nobody else's
        note is ever stored there), `Memory.Curator` **and** member for a
        group, the project's own owner or a `Memory.Curator` member for a
        project, `Memory.Admin` for `org`.
        """
        if alias not in self.writable():
            return False
        if self.own_alias is not None and alias == self.own_alias:
            return True
        for row in self.rows:
            if row.alias != alias:
                continue
            if row.kind == _GROUP_KIND:
                return _CURATOR_ROLE in self.roles
            if row.kind == _PROJECT_KIND:
                return row.role == "owner" or _CURATOR_ROLE in self.roles
            if row.kind == _ORG_KIND:
                return _ADMIN_ROLE in self.roles
        return False

    def to_stored(self, value: str) -> str:
        """`value` (a bare namespace alias, `me` included) as the real stored alias.

        Raises `PathRejected` for an internal `u-*` key - unconditionally, not
        only for the caller's own: ADR-0008 addendum, "internal keys are
        rejected as input", independent of whose personal namespace it would
        resolve to.
        """
        if value.startswith(_INTERNAL_ALIAS_PREFIX):
            raise PathRejected(
                f"namespace {value!r} is an internal key, not an address a client may "
                f"use - use {ME_ALIAS!r} for your own personal namespace"
            )
        if value == ME_ALIAS:
            if self.own_alias is None:  # pragma: no cover - degenerate empty-oid case
                raise PathRejected("the caller has no personal namespace yet")
            return self.own_alias
        return value

    def to_display(self, value: str) -> str:
        """`value` (a real stored alias) as what a tool should ever show it as.

        `me` for the caller's own personal namespace, unchanged otherwise -
        a shared (group/project/org) namespace is always shown under its own
        alias, never translated.
        """
        if self.own_alias is not None and value == self.own_alias:
            return ME_ALIAS
        return value

    def kind_of(self, value: str) -> str | None:
        """`value` (a real stored alias, never `me`) as the result field `namespace_kind`
        asks for (#102): `'personal'`, `'group'`, `'project'` or `'org'`.

        The caller's own personal namespace is checked first and unconditionally
        returns `'personal'`, the same way `to_display` special-cases it - `rows`
        does carry a `kind='user'` row for it too (`mm_principal_namespaces`'s own
        `personal` CTE), but going through `own_alias` here avoids relying on that
        row being present. `None` only for an alias this resolution holds no row
        for at all - not a reachable case for a `memory_search`/`memory_index`
        result, since every path a caller can see came from a namespace
        `readable()` already named, but a safe fallback rather than a raise for
        any other, best-effort caller.
        """
        if self.own_alias is not None and value == self.own_alias:
            return _KIND_LABELS["user"]
        for row in self.rows:
            if row.alias == value:
                return _KIND_LABELS.get(row.kind, row.kind)
        return None


async def resolve(pool: asyncpg.Pool, *, role: str) -> Resolution:
    """Resolve the current request's principal into a `Resolution`.

    One `db.rls.request_connection` transaction (switched to `role` and the
    calling principal's identity): `select mm_ensure_personal_ns()` first (so
    a first-time caller's own row exists before `mm_principal_namespaces`
    could otherwise miss it), then `select * from mm_principal_namespaces($1)`
    with the principal's own `groups` claim. Raises `db.rls.NoPrincipal`
    before acquiring a connection at all if the current request carries none
    - the same structural guarantee `request_connection` itself gives every
    other caller.
    """
    async with rls.request_connection(pool, role=role) as conn:
        principal = rls.current_principal()
        if principal is None:  # pragma: no cover - request_connection already raised NoPrincipal
            raise rls.NoPrincipal("no principal resolved inside an open request connection")
        own_alias = await conn.fetchval("select mm_ensure_personal_ns()")
        rows = await conn.fetch(
            "select kind, alias, role, group_write, project_write from mm_principal_namespaces($1)",
            list(principal.groups),
        )
    return Resolution(
        oid=principal.oid,
        roles=principal.roles,
        own_alias=own_alias,
        rows=tuple(
            _NamespaceRow(
                kind=row["kind"],
                alias=row["alias"],
                role=row["role"],
                group_write=row["group_write"],
                project_write=row["project_write"],
            )
            for row in rows
        ),
    )


async def author_oid_of(pool: asyncpg.Pool, *, role: str, path: str) -> str | None:
    """The `author_oid` of `path`'s revision 1, or `None` if it has none (foreign/system).

    Its own `db.rls.request_connection` (ADR-0008 addendum: a note's author is
    the `author_oid` of its revision 1) - `path` is the real, stored vault
    path, never `me`; callers resolve that first. `None` both for a note that
    does not exist (nothing for `memory_archive` to curate-check in the first
    place - its own `NotFound` already covers that case earlier) and for one
    whose revision 1 carries no `author_oid` at all (a system-written note,
    e.g. a Git-to-Postgres import) - either way treated as "not the caller",
    i.e. foreign, by `Resolution.can_curate`'s caller.
    """
    async with rls.request_connection(pool, role=role) as conn:
        author_oid: str | None = await conn.fetchval(
            "select vr.author_oid from vault_revisions vr "
            "join vault_notes vn on vn.id = vr.note_id "
            "where vn.path = $1 and vr.revision = 1",
            path,
        )
        return author_oid


def rewrite_path_to_stored(path: str, resolution: Resolution) -> str:
    """`path` with its namespace segment translated from `me`/alias to the stored alias.

    Best-effort on anything that is not a parseable note path (a bare ULID,
    for instance) - returned unchanged, left to the deeper, sharper
    `PathRejected`/`NotFound` the actual read/write call raises for it
    instead. Raises `PathRejected` itself only for a path that *does* parse
    and whose namespace is an internal `u-*` key (`Resolution.to_stored`).
    """
    try:
        note_path = parse_note_path(path, allow_archive=True)
    except PathRejected:
        return path
    stored_namespace = resolution.to_stored(note_path.namespace)
    if stored_namespace == note_path.namespace:
        return path
    return NotePath(
        namespace=stored_namespace,
        type=note_path.type,
        slug=note_path.slug,
        archived=note_path.archived,
    ).relative


def rewrite_path_to_display(path: str, resolution: Resolution) -> str:
    """`path` with its namespace segment translated from the stored alias to `me`/alias.

    Best-effort the same way `rewrite_path_to_stored` is - never raises,
    since `Resolution.to_display` never does.
    """
    try:
        note_path = parse_note_path(path, allow_archive=True)
    except PathRejected:
        return path
    display_namespace = resolution.to_display(note_path.namespace)
    if display_namespace == note_path.namespace:
        return path
    return NotePath(
        namespace=display_namespace,
        type=note_path.type,
        slug=note_path.slug,
        archived=note_path.archived,
    ).relative
