# SPDX-License-Identifier: AGPL-3.0-only
"""Generated role x namespace-kind x action matrix (ADR-0008, #101).

Mirrors `tests/db/test_request_path.py:55-108`'s non-superuser owner shape
(`mm`, the connection `MM_TEST_DATABASE_URL` names, is a superuser locally -
`grant_app_role`/`check_app_role`'s own checks are only meaningful against a
non-superuser owner): `matrix_db` creates its own dedicated, non-superuser
owner role and database, and its own non-owner app role, once for the whole
module (`scope="module"`) - every one of this module's ~130 parametrized
cases reuses the same seeded database rather than paying a fresh
create-database/migrate round trip per case.

`_ORACLE` is this test's own, hand-written table of expected outcomes,
derived directly from ADR-0008's permission matrix and its 2026-10-07
"curate requires write" addendum - never computed by calling
`memory_manager.mcp.namespaces`'s own `Resolution.readable`/`writable`/
`can_curate`. What *is* exercised, for every `(case, role)` pair, is the
real thing: `namespaces.resolve` against the real seeded Postgres database
(switched to the app role and the parametrised principal, via
`db.rls.request_connection`) and `namespaces.author_oid_of` against two real
seeded notes (one authored by the case's own principal, one by a different
oid) - so a oracle/implementation mismatch is caught the same way a bug in
either the Python matrix or the SQL functions it calls would be.

The principal for each case is injected the way `tests/db/test_request_path.
py`/`tests/mcp/conftest.py`'s `postgres_backend_principal` both do:
monkeypatching `db.rls.get_access_token` with a fake `AccessToken` carrying
`oid`/`roles`/`groups` claims - there is no real HTTP auth middleware in
this module to carry a bearer token's claims otherwise.

The end-to-end section at the bottom drives the real MCP tools
(`build_server(matrix_db.services)`, an in-memory `mcp.Client`, like
`tests/mcp/test_search_tool.py`'s own postgres-backend test) rather than
`namespaces.py`'s functions directly - this is what actually exercises
`mcp/server.py`'s own glue (`_rewrite_error_result`, `_require_archive_access`,
`_require_writable`, the `me` rewriting of every tool's input and output),
which the module-level matrix above never touches at all.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio
from mcp import Client
from mcp.server.auth.provider import AccessToken

from memory_manager.app import Services, open_services
from memory_manager.db import rls
from memory_manager.mcp import namespaces
from memory_manager.mcp.server import build_server
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.note import version as note_version
from memory_manager.vault.paths import PathRejected
from memory_manager.vault.ulid import new_ulid

pytestmark = pytest.mark.asyncio(loop_scope="module")

_MEMORY_USER = "Memory.User"
_MEMORY_CURATOR = "Memory.Curator"
_MEMORY_ADMIN = "Memory.Admin"
_ROLES = (_MEMORY_USER, _MEMORY_CURATOR, _MEMORY_ADMIN)

# -- fixed identities and registry entries the whole module shares -----------

_OID_OWN = "oid-matrix-own"
_OID_FOREIGN = "oid-matrix-foreign"
_OID_OUTSIDER = "oid-matrix-outsider"
_OID_READER_ONLY = "oid-matrix-reader-only"
_OID_OWNER_MEMBER = "oid-matrix-owner-member"
_OID_MISMATCH = "oid-matrix-mismatch"
_OID_PROJECT_FILLER = "oid-matrix-filler"
_OID_FOREIGN_AUTHOR = "oid-matrix-foreign-author"

_ALL_OIDS = (
    _OID_OWN,
    _OID_FOREIGN,
    _OID_OUTSIDER,
    _OID_READER_ONLY,
    _OID_OWNER_MEMBER,
    _OID_MISMATCH,
    _OID_PROJECT_FILLER,
)

_GRP_DEFAULT_KEY = "grp-matrix-default"
_GRP_CURATOR_KEY = "grp-matrix-curator"

_ALIAS_OWN = "own-personal"
_ALIAS_FOREIGN = "foreign-personal"
_ALIAS_GRP_DEFAULT = "grp-members"
_ALIAS_GRP_CURATOR = "grp-curators"
_ALIAS_PROJ_READERS = "proj-readers"
_ALIAS_PROJ_WRITERS_W = "proj-writers-w"
_ALIAS_PROJ_WRITERS_R = "proj-writers-r"
_ALIAS_PROJ_WRITERS_O = "proj-writers-o"
_ALIAS_PROJ_NONMEMBER = "proj-nonmember"
_ALIAS_ORG = "org"


@dataclass(frozen=True)
class _Target:
    """Which principal and which namespace alias one matrix case exercises."""

    oid: str
    groups: tuple[str, ...]
    alias: str


_TARGETS: dict[str, _Target] = {
    "own_personal": _Target(_OID_OWN, (), _ALIAS_OWN),
    "foreign_personal_alias": _Target(_OID_OWN, (), _ALIAS_FOREIGN),
    "group_member_default_write": _Target(_OID_OWN, (_GRP_DEFAULT_KEY,), _ALIAS_GRP_DEFAULT),
    "group_member_curator_write": _Target(_OID_OWN, (_GRP_CURATOR_KEY,), _ALIAS_GRP_CURATOR),
    "group_non_member": _Target(_OID_OUTSIDER, (), _ALIAS_GRP_DEFAULT),
    "project_reader_write_readers": _Target(_OID_OWN, (), _ALIAS_PROJ_READERS),
    "project_writer_write_writers": _Target(_OID_OWN, (), _ALIAS_PROJ_WRITERS_W),
    "project_reader_write_writers": _Target(_OID_READER_ONLY, (), _ALIAS_PROJ_WRITERS_R),
    "project_owner_write_writers": _Target(_OID_OWNER_MEMBER, (), _ALIAS_PROJ_WRITERS_O),
    "project_non_member": _Target(_OID_OUTSIDER, (), _ALIAS_PROJ_NONMEMBER),
    "org": _Target(_OID_OWN, (), _ALIAS_ORG),
}

# (read, write, archive_own, archive_foreign) - hand-derived from ADR-0008's
# table plus the "curate requires write" addendum, see the module docstring.
_ORACLE: dict[str, dict[str, tuple[bool, bool, bool, bool]]] = {
    "own_personal": {
        _MEMORY_USER: (True, True, True, True),
        _MEMORY_CURATOR: (True, True, True, True),
        _MEMORY_ADMIN: (True, True, True, True),
    },
    "foreign_personal_alias": {
        _MEMORY_USER: (False, False, False, False),
        _MEMORY_CURATOR: (False, False, False, False),
        _MEMORY_ADMIN: (False, False, False, False),
    },
    "group_member_default_write": {
        _MEMORY_USER: (True, True, True, False),
        _MEMORY_CURATOR: (True, True, True, True),
        _MEMORY_ADMIN: (True, True, True, False),
    },
    "group_member_curator_write": {
        _MEMORY_USER: (True, False, False, False),
        _MEMORY_CURATOR: (True, True, True, True),
        _MEMORY_ADMIN: (True, False, False, False),
    },
    "group_non_member": {
        _MEMORY_USER: (False, False, False, False),
        _MEMORY_CURATOR: (False, False, False, False),
        _MEMORY_ADMIN: (False, False, False, False),
    },
    "project_reader_write_readers": {
        _MEMORY_USER: (True, True, True, False),
        _MEMORY_CURATOR: (True, True, True, True),
        _MEMORY_ADMIN: (True, True, True, False),
    },
    "project_writer_write_writers": {
        _MEMORY_USER: (True, True, True, False),
        _MEMORY_CURATOR: (True, True, True, True),
        _MEMORY_ADMIN: (True, True, True, False),
    },
    # The 2026-10-07 addendum's own example: a reader-only member of a
    # `project_write='writers'` project cannot write at all, Curator or not.
    "project_reader_write_writers": {
        _MEMORY_USER: (True, False, False, False),
        _MEMORY_CURATOR: (True, False, False, False),
        _MEMORY_ADMIN: (True, False, False, False),
    },
    "project_owner_write_writers": {
        _MEMORY_USER: (True, True, True, True),
        _MEMORY_CURATOR: (True, True, True, True),
        _MEMORY_ADMIN: (True, True, True, True),
    },
    "project_non_member": {
        _MEMORY_USER: (False, False, False, False),
        _MEMORY_CURATOR: (False, False, False, False),
        _MEMORY_ADMIN: (False, False, False, False),
    },
    "org": {
        _MEMORY_USER: (True, False, False, False),
        _MEMORY_CURATOR: (True, True, True, False),
        _MEMORY_ADMIN: (True, True, True, True),
    },
}


@dataclass(frozen=True)
class MatrixDb:
    services: Services
    pool: asyncpg.Pool
    app_role: str


async def _seed(owner_url: str) -> None:
    """Every fixed identity, namespace, membership and matrix-case note (module docstring)."""
    conn = await asyncpg.connect(owner_url)
    try:
        for oid in _ALL_OIDS:
            await conn.execute(
                "insert into users (oid, tid, display_name) values ($1, 'tenant-matrix', $1)",
                oid,
            )

        await conn.execute(
            "insert into namespaces (kind, external_key, alias) values "
            "('user', $1, $2), ('user', $3, $4)",
            _OID_OWN,
            _ALIAS_OWN,
            _OID_FOREIGN,
            _ALIAS_FOREIGN,
        )

        grp_default_id = await conn.fetchval(
            "insert into namespaces (kind, external_key, alias) values ('group', $1, $2) "
            "returning id",
            _GRP_DEFAULT_KEY,
            _ALIAS_GRP_DEFAULT,
        )
        grp_curator_id = await conn.fetchval(
            "insert into namespaces (kind, external_key, alias) values ('group', $1, $2) "
            "returning id",
            _GRP_CURATOR_KEY,
            _ALIAS_GRP_CURATOR,
        )
        await conn.execute(
            "insert into namespace_settings (namespace_id, group_write) values ($1, 'curators')",
            grp_curator_id,
        )
        # `grp_default_id`'s `namespace_settings` row is deliberately absent -
        # `group_write` defaults to `'members'`, exercised the same way a
        # namespace an admin never configured would be.
        del grp_default_id

        # Real `user_groups` membership (the RLS side's own source of truth,
        # ADR-0008 addendum: "RLS uses user_groups") - `oid-matrix-mismatch`
        # is deliberately *not* added here, see `test_claims_group_
        # membership_without_user_groups_row_fails_closed` below.
        await conn.execute(
            "insert into user_groups (oid, group_id) values ($1, $2), ($1, $3)",
            _OID_OWN,
            _GRP_DEFAULT_KEY,
            _GRP_CURATOR_KEY,
        )

        proj_readers_id = await conn.fetchval(
            "insert into namespaces (kind, external_key, alias) values ('project', $1, $2) "
            "returning id",
            "proj-readers-key",
            _ALIAS_PROJ_READERS,
        )
        await conn.execute(
            "insert into namespace_settings (namespace_id, project_write) values ($1, 'readers')",
            proj_readers_id,
        )
        await conn.execute(
            "insert into project_members (namespace_id, principal_kind, principal_id, role) "
            "values ($1, 'user', $2, 'reader')",
            proj_readers_id,
            _OID_OWN,
        )

        proj_writers_w_id = await conn.fetchval(
            "insert into namespaces (kind, external_key, alias) values ('project', $1, $2) "
            "returning id",
            "proj-writers-w-key",
            _ALIAS_PROJ_WRITERS_W,
        )
        await conn.execute(
            "insert into project_members (namespace_id, principal_kind, principal_id, role) "
            "values ($1, 'user', $2, 'writer')",
            proj_writers_w_id,
            _OID_OWN,
        )

        proj_writers_r_id = await conn.fetchval(
            "insert into namespaces (kind, external_key, alias) values ('project', $1, $2) "
            "returning id",
            "proj-writers-r-key",
            _ALIAS_PROJ_WRITERS_R,
        )
        await conn.execute(
            "insert into project_members (namespace_id, principal_kind, principal_id, role) "
            "values ($1, 'user', $2, 'reader')",
            proj_writers_r_id,
            _OID_READER_ONLY,
        )

        proj_writers_o_id = await conn.fetchval(
            "insert into namespaces (kind, external_key, alias) values ('project', $1, $2) "
            "returning id",
            "proj-writers-o-key",
            _ALIAS_PROJ_WRITERS_O,
        )
        await conn.execute(
            "insert into project_members (namespace_id, principal_kind, principal_id, role) "
            "values ($1, 'user', $2, 'owner')",
            proj_writers_o_id,
            _OID_OWNER_MEMBER,
        )

        proj_nonmember_id = await conn.fetchval(
            "insert into namespaces (kind, external_key, alias) values ('project', $1, $2) "
            "returning id",
            "proj-nonmember-key",
            _ALIAS_PROJ_NONMEMBER,
        )
        await conn.execute(
            "insert into project_members (namespace_id, principal_kind, principal_id, role) "
            "values ($1, 'user', $2, 'writer')",
            proj_nonmember_id,
            _OID_PROJECT_FILLER,
        )

        await conn.execute(
            "insert into namespaces (kind, external_key, alias) values ('org', 'org', $1)",
            _ALIAS_ORG,
        )

        for case, target in _TARGETS.items():
            await _seed_note(
                conn,
                note_id=f"note-{case}-own",
                namespace=target.alias,
                path=f"{target.alias}/fact/{case}-own.md",
                author_oid=target.oid,
            )
            await _seed_note(
                conn,
                note_id=f"note-{case}-foreign",
                namespace=target.alias,
                path=f"{target.alias}/fact/{case}-foreign.md",
                author_oid=_OID_FOREIGN_AUTHOR,
            )

        # Dedicated notes for the end-to-end `memory_archive` cases below -
        # never read by `test_matrix` above, so a test that actually moves
        # one into `_archive/` never disturbs another test's expectations.
        # Real, parseable content (`_valid_note`): `storage.archive` parses
        # the note it archives (`storage.rules.prepare_archive`), unlike the
        # placeholder content every other seeded note above carries, which
        # is only ever read back out through raw SQL, never a real tool.
        own_content, own_version = _valid_note("E2E Own")
        await _seed_note(
            conn,
            note_id="note-e2e-own",
            namespace=_ALIAS_GRP_DEFAULT,
            path=f"{_ALIAS_GRP_DEFAULT}/fact/e2e-own.md",
            author_oid=_OID_OWN,
            content=own_content,
            version=own_version,
        )
        foreign_content, foreign_version = _valid_note("E2E Foreign")
        await _seed_note(
            conn,
            note_id="note-e2e-foreign",
            namespace=_ALIAS_GRP_DEFAULT,
            path=f"{_ALIAS_GRP_DEFAULT}/fact/e2e-foreign.md",
            author_oid=_OID_FOREIGN_AUTHOR,
            content=foreign_content,
            version=foreign_version,
        )

        # A clean-slug note under the foreign user's personal namespace, for
        # the end-to-end "another user's personal namespace is unreadable"
        # case - `_TARGETS`'s own `foreign_personal_alias` notes use the case
        # name (with underscores) as part of their slug and are never read
        # through a real tool, so their invalid slug never surfaces there.
        await _seed_note(
            conn,
            note_id="note-e2e-foreign-personal-read",
            namespace=_ALIAS_FOREIGN,
            path=f"{_ALIAS_FOREIGN}/fact/e2e-foreign-read.md",
            author_oid=_OID_FOREIGN,
        )
    finally:
        await conn.close()


#: Placeholder bytes for notes only ever read through raw SQL (`author_oid_of`,
#: the `test_matrix` oracle) - never through a real tool, which would parse them
#: as a note and reject this (`vault.note.parse`). `_seed_note`'s own `content`
#: parameter overrides this for the end-to-end archive notes below, which *are*
#: read through the real `memory_archive` tool and must parse.
_PLACEHOLDER_CONTENT = b"seed content"
_PLACEHOLDER_VERSION = "v1"

_SEED_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _valid_note(title: str) -> tuple[bytes, str]:
    """A real, parseable note's serialized bytes and its own `vault.note.version` -
    for the end-to-end archive notes below, which `storage.archive` actually parses
    (`storage.rules.prepare_archive`), unlike the oracle matrix's placeholder notes.
    """
    note = Note(
        id=new_ulid(_SEED_NOW),
        title=title,
        description=f"{title} description.",
        type="fact",
        created=_SEED_NOW,
        updated=_SEED_NOW,
        body="Body.\n",
    )
    content = serialize(note)
    return content, note_version(content)


async def _seed_note(
    conn: asyncpg.Connection,
    *,
    note_id: str,
    namespace: str,
    path: str,
    author_oid: str,
    content: bytes = _PLACEHOLDER_CONTENT,
    version: str = _PLACEHOLDER_VERSION,
) -> None:
    await conn.execute(
        "insert into vault_notes (id, namespace, path, content, version, current_revision) "
        "values ($1, $2, $3, $4, $5, 1)",
        note_id,
        namespace,
        path,
        content,
        version,
    )
    await conn.execute(
        "insert into vault_revisions "
        "(note_id, revision, path, content, version, author, client, author_oid) "
        "values ($1, 1, $2, $3, $4, 'seed', 'pytest', $5)",
        note_id,
        path,
        content,
        version,
        author_oid,
    )


def _note_path(case: str, *, own: bool) -> str:
    target = _TARGETS[case]
    suffix = "own" if own else "foreign"
    return f"{target.alias}/fact/{case}-{suffix}.md"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def matrix_db(admin_database_url: str) -> AsyncIterator[MatrixDb]:
    """A dedicated non-superuser owner + non-owner app role, seeded once per module."""
    db_name = f"mm_test_matrix_{secrets.token_hex(8)}"
    owner_role = f"mm_test_owner_{secrets.token_hex(8)}"
    owner_password = secrets.token_urlsafe(16)
    app_role = f"mm_test_app_{secrets.token_hex(8)}"

    admin_conn = await asyncpg.connect(admin_database_url)
    try:
        await admin_conn.execute(
            f"create role \"{owner_role}\" login password '{owner_password}' nosuperuser"
        )
        await admin_conn.execute(f'create database "{db_name}" owner "{owner_role}"')
        await admin_conn.execute(f'create role "{app_role}" nologin nosuperuser nobypassrls')
        await admin_conn.execute(f'grant "{app_role}" to "{owner_role}"')

        parsed = urlsplit(admin_database_url)
        base, _, _ = admin_database_url.rpartition("/")
        bootstrap_conn = await asyncpg.connect(f"{base}/{db_name}")
        try:
            await bootstrap_conn.execute("create extension if not exists vector")
        finally:
            await bootstrap_conn.close()

        owner_url = urlunsplit(
            (
                parsed.scheme,
                f"{owner_role}:{owner_password}@{parsed.hostname}:{parsed.port}",
                f"/{db_name}",
                "",
                "",
            )
        )

        environ = {
            "STORAGE_BACKEND": "postgres",
            "DATABASE_URL": owner_url,
            "DATABASE_APP_ROLE": app_role,
        }
        async with open_services(environ) as services:
            assert services.pool is not None
            await _seed(owner_url)
            yield MatrixDb(services=services, pool=services.pool, app_role=app_role)
    finally:
        await admin_conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity "
            "where datname = $1 and pid <> pg_backend_pid()",
            db_name,
        )
        await admin_conn.execute(f'drop database if exists "{db_name}"')
        await admin_conn.execute(f'drop role if exists "{app_role}"')
        await admin_conn.execute(f'drop role if exists "{owner_role}"')
        await admin_conn.close()


def _set_principal(
    monkeypatch: pytest.MonkeyPatch, *, oid: str, roles: list[str], groups: list[str]
) -> None:
    """Make `db.rls.current_principal()` (and, through it, `namespaces.resolve`/
    `mcp/server.py`'s own `_resolve_namespaces`) resolve to `oid`/`roles`/`groups`.
    """
    token = AccessToken(
        token="mm_x",  # noqa: S106 - a fake test token, not a credential
        client_id="static:test",
        scopes=[],
        claims={"oid": oid, "roles": roles, "groups": groups},
    )
    monkeypatch.setattr(rls, "get_access_token", lambda: token)


async def _resolve_for(
    matrix_db: MatrixDb,
    monkeypatch: pytest.MonkeyPatch,
    *,
    oid: str,
    roles: list[str],
    groups: list[str],
) -> namespaces.Resolution:
    """`_set_principal`, then `namespaces.resolve` against the real seeded database."""
    _set_principal(monkeypatch, oid=oid, roles=roles, groups=groups)
    return await namespaces.resolve(matrix_db.pool, role=matrix_db.app_role)


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("case", sorted(_ORACLE))
async def test_matrix(
    matrix_db: MatrixDb, monkeypatch: pytest.MonkeyPatch, case: str, role: str
) -> None:
    target = _TARGETS[case]
    expected_read, expected_write, expected_archive_own, expected_archive_foreign = _ORACLE[case][
        role
    ]

    resolved = await _resolve_for(
        matrix_db, monkeypatch, oid=target.oid, roles=[role], groups=list(target.groups)
    )

    assert (target.alias in resolved.readable()) == expected_read
    assert (target.alias in resolved.writable()) == expected_write

    author_own = await namespaces.author_oid_of(
        matrix_db.pool, role=matrix_db.app_role, path=_note_path(case, own=True)
    )
    author_foreign = await namespaces.author_oid_of(
        matrix_db.pool, role=matrix_db.app_role, path=_note_path(case, own=False)
    )
    if target.alias in resolved.readable():
        # The seeded "own" note is only visible under RLS's own, independent
        # computation (`mm_readable_ns`) once the namespace is actually
        # readable - exactly the cases this sanity check is meaningful for.
        assert author_own == target.oid

    writable = target.alias in resolved.writable()
    archive_own = writable and author_own == target.oid
    archive_foreign = writable and (
        author_foreign == target.oid or resolved.can_curate(target.alias)
    )

    assert archive_own == expected_archive_own
    assert archive_foreign == expected_archive_foreign


async def test_u_prefixed_namespace_is_rejected_as_input_even_for_the_owner(
    matrix_db: MatrixDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Internal `u-*` keys are rejected unconditionally (ADR-0008 addendum), not just for
    someone else's personal namespace - a client must always say `me`, never the alias
    `mm_ensure_personal_ns()` actually stored.
    """
    resolved = await _resolve_for(
        matrix_db, monkeypatch, oid=_OID_OWN, roles=[_MEMORY_USER], groups=[]
    )
    with pytest.raises(PathRejected):
        resolved.to_stored("u-999999")

    foreign_ns_id = await matrix_db.pool.fetchval(
        "select id from namespaces where kind = 'user' and external_key = $1", _OID_FOREIGN
    )
    with pytest.raises(PathRejected):
        resolved.to_stored(f"u-{foreign_ns_id}")


async def test_claims_group_membership_without_user_groups_row_fails_closed(
    matrix_db: MatrixDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0008 addendum: "Python uses token claims groups, RLS uses user_groups - a
    mismatch fails closed." `oid-matrix-mismatch` claims membership in the
    default-write group via its token, but has no matching `user_groups` row: the
    Python matrix (this module's own, independent computation) says writable, but
    the real write - governed by RLS's independent computation against
    `user_groups` - is still refused.
    """
    resolved = await _resolve_for(
        matrix_db,
        monkeypatch,
        oid=_OID_MISMATCH,
        roles=[_MEMORY_USER],
        groups=[_GRP_DEFAULT_KEY],
    )
    assert _ALIAS_GRP_DEFAULT in resolved.readable()
    assert _ALIAS_GRP_DEFAULT in resolved.writable()

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with rls.request_connection(matrix_db.pool, role=matrix_db.app_role) as conn:
            await conn.execute(
                "insert into vault_notes "
                "(id, namespace, path, content, version, current_revision) "
                "values ('note-mismatch', $1, $2, 'seed content', 'v1', 1)",
                _ALIAS_GRP_DEFAULT,
                f"{_ALIAS_GRP_DEFAULT}/fact/mismatch.md",
            )


# -- end-to-end: the real MCP tools through `build_server`, not `namespaces.py` directly ----

#: Never pre-seeded with a personal-namespace row - its own alias is created
#: lazily, in the real `u-<id>` shape (ADR-0008 addendum), the one case this
#: module's other fixtures (all pre-seeded with a readable literal alias) never
#: exercise.
_OID_E2E_LAZY = "oid-matrix-e2e-lazy"


def _note_text(*, title: str) -> str:
    return f"---\ntitle: {title}\ndescription: {title} description.\ntype: fact\n---\nBody.\n"


async def test_e2e_u_prefixed_namespace_is_rejected_cleanly_everywhere(
    matrix_db: MatrixDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `u-*` namespace never crashes a tool call with a generic, structureless
    error - `memory_write`/`memory_read`/`memory_search` all reject it the same,
    clean way `mcp/errors.py` already reports every other invalid path.
    """
    _set_principal(monkeypatch, oid=_OID_OWN, roles=[_MEMORY_USER], groups=[])
    async with Client(build_server(matrix_db.services)) as client:
        write_result = await client.call_tool(
            "memory_write",
            {
                "path": "u-999999/fact/x.md",
                "content": _note_text(title="X"),
                "if_version": "new",
            },
        )
        assert write_result.is_error is True
        write_error = cast(dict[str, Any], write_result.structured_content)
        assert write_error["error"] == "InvalidNote"
        assert write_error["path"] == "u-999999/fact/x.md"

        read_result = await client.call_tool("memory_read", {"items": ["u-999999/fact/x.md"]})
        assert read_result.is_error is False
        read_items = cast(list[dict[str, Any]], read_result.structured_content["result"])
        assert len(read_items) == 1
        assert read_items[0]["item"] == "u-999999/fact/x.md"
        assert "content" not in read_items[0]
        assert read_items[0]["error"]["error"] == "PathRejected"
        assert "internal key" in read_items[0]["error"]["message"]

        search_result = await client.call_tool(
            "memory_search", {"query": "x", "namespaces": ["u-999999"]}
        )
        assert search_result.is_error is True


async def test_e2e_write_to_me_stores_under_the_real_alias_and_displays_me(
    matrix_db: MatrixDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`me` in, `me` out - `memory_write`'s own `path` input and its success
    result's `path` both say `me`, even though the row physically lands under
    `own-personal`, `oid-matrix-own`'s real, pre-seeded alias.
    """
    _set_principal(monkeypatch, oid=_OID_OWN, roles=[_MEMORY_USER], groups=[])
    async with Client(build_server(matrix_db.services)) as client:
        write_result = await client.call_tool(
            "memory_write",
            {
                "path": "me/fact/e2e-write.md",
                "content": _note_text(title="E2E Write"),
                "if_version": "new",
            },
        )
        assert write_result.is_error is False
        payload = cast(dict[str, Any], write_result.structured_content)
        assert payload["path"] == "me/fact/e2e-write.md"

        read_result = await client.call_tool("memory_read", {"items": ["me/fact/e2e-write.md"]})
        read_items = cast(list[dict[str, Any]], read_result.structured_content["result"])
        assert read_items[0]["path"] == "me/fact/e2e-write.md"

    stored_namespace = await matrix_db.pool.fetchval(
        "select namespace from vault_notes where path = $1", f"{_ALIAS_OWN}/fact/e2e-write.md"
    )
    assert stored_namespace == _ALIAS_OWN


async def test_e2e_another_users_personal_namespace_is_unreadable(
    matrix_db: MatrixDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`oid-matrix-own` can address neither `oid-matrix-foreign`'s personal namespace
    by its real alias nor find anything there through search - ADR-0008: a shared
    namespace is never implicitly readable, and there is no `me` for someone else.
    """
    _set_principal(monkeypatch, oid=_OID_OWN, roles=[_MEMORY_USER], groups=[])
    async with Client(build_server(matrix_db.services)) as client:
        read_result = await client.call_tool(
            "memory_read",
            {"items": [f"{_ALIAS_FOREIGN}/fact/e2e-foreign-read.md"]},
        )
        read_items = cast(list[dict[str, Any]], read_result.structured_content["result"])
        assert "content" not in read_items[0]
        assert read_items[0]["error"]["error"] == "NotFound"

        search_result = await client.call_tool(
            "memory_search", {"query": "e2e", "namespaces": [_ALIAS_FOREIGN]}
        )
        search_payload = cast(dict[str, Any], search_result.structured_content)
        assert search_payload["results"] == []


async def test_e2e_version_conflict_on_a_me_note_never_shows_the_internal_key(
    matrix_db: MatrixDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_OID_E2E_LAZY`'s own alias is only ever created lazily, in the real
    `u-<id>` shape - a `VersionConflict` on a note written at `me/...` must still
    show `me`, in both `path` and `message`, never the internal `u-<id>` alias.
    """
    _set_principal(monkeypatch, oid=_OID_E2E_LAZY, roles=[_MEMORY_USER], groups=[])
    async with Client(build_server(matrix_db.services)) as client:
        created = await client.call_tool(
            "memory_write",
            {
                "path": "me/fact/conflict-test.md",
                "content": _note_text(title="Conflict Test"),
                "if_version": "new",
            },
        )
        assert created.is_error is False

        conflict = await client.call_tool(
            "memory_write",
            {
                "path": "me/fact/conflict-test.md",
                "content": _note_text(title="Conflict Test Again"),
                # "new" against a path that already exists is itself a
                # `VersionConflict` (`_MEMORY_WRITE_DESCRIPTION`) - and needs
                # no `id`/`created`/`updated` fields the way an update would.
                "if_version": "new",
            },
        )
        assert conflict.is_error is True
        conflict_payload = cast(dict[str, Any], conflict.structured_content)
        assert conflict_payload["error"] == "VersionConflict"
        assert conflict_payload["path"] == "me/fact/conflict-test.md"
        assert "u-" not in conflict_payload["path"]
        assert "u-" not in conflict_payload["message"]
        assert "me/fact/conflict-test.md" in conflict_payload["message"]


async def test_e2e_archive_foreign_note_in_group_needs_curator_own_note_needs_only_write(
    matrix_db: MatrixDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0008's own example, end to end: a plain member of a `group_write='members'`
    group may archive their own note (write is enough) but not someone else's
    (curate, which this member lacks without `Memory.Curator`) - and a `Memory.
    Curator` member may.
    """
    foreign_path = f"{_ALIAS_GRP_DEFAULT}/fact/e2e-foreign.md"
    own_path = f"{_ALIAS_GRP_DEFAULT}/fact/e2e-own.md"
    foreign_version = await matrix_db.pool.fetchval(
        "select version from vault_notes where path = $1", foreign_path
    )
    own_version = await matrix_db.pool.fetchval(
        "select version from vault_notes where path = $1", own_path
    )

    _set_principal(monkeypatch, oid=_OID_OWN, roles=[_MEMORY_USER], groups=[_GRP_DEFAULT_KEY])
    async with Client(build_server(matrix_db.services)) as client:
        refused = await client.call_tool(
            "memory_archive", {"path": foreign_path, "if_version": foreign_version}
        )
        assert refused.is_error is True

        own_archived = await client.call_tool(
            "memory_archive", {"path": own_path, "if_version": own_version}
        )
        assert own_archived.is_error is False
        own_payload = cast(dict[str, Any], own_archived.structured_content)
        assert own_payload["archived_path"] == f"_archive/{own_path}"

    _set_principal(monkeypatch, oid=_OID_OWN, roles=[_MEMORY_CURATOR], groups=[_GRP_DEFAULT_KEY])
    async with Client(build_server(matrix_db.services)) as client:
        curator_archived = await client.call_tool(
            "memory_archive", {"path": foreign_path, "if_version": foreign_version}
        )
        assert curator_archived.is_error is False
        curator_payload = cast(dict[str, Any], curator_archived.structured_content)
        assert curator_payload["archived_path"] == f"_archive/{foreign_path}"
