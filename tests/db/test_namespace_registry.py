# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for namespace resolution and revision authorship (ADR-0008 addendum, #119).

Mirrors `tests/db/test_request_path.py:55-108`'s non-superuser owner shape:
`mm` (the connection `MM_TEST_DATABASE_URL` names) is a superuser locally, so
this fixture creates its own non-superuser owner role and database, applies
every migration as that role, and only ever queries as the owner or the
request-serving app role created alongside it - never as `mm` itself.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from memory_manager.db.migrate import migrate
from memory_manager.db.rls import grant_app_role, request_identity


@dataclass(frozen=True)
class NamespaceRegistryDb:
    owner_url: str
    app_role: str
    foreign_role: str


async def _connect_as(url: str) -> asyncpg.Connection:
    return await asyncpg.connect(url)


@pytest_asyncio.fixture
async def registry_db(admin_database_url: str) -> AsyncIterator[NamespaceRegistryDb]:
    db_name = f"mm_test_nsreg_{secrets.token_hex(8)}"
    owner_role = f"mm_test_owner_{secrets.token_hex(8)}"
    owner_password = secrets.token_urlsafe(16)
    app_role = f"mm_test_app_{secrets.token_hex(8)}"
    foreign_role = f"mm_test_foreign_{secrets.token_hex(8)}"

    admin_conn = await asyncpg.connect(admin_database_url)
    owner_conn: asyncpg.Connection | None = None
    try:
        await admin_conn.execute(
            f"create role \"{owner_role}\" login password '{owner_password}' nosuperuser"
        )
        await admin_conn.execute(f'create database "{db_name}" owner "{owner_role}"')
        await admin_conn.execute(f'create role "{app_role}" nologin nosuperuser nobypassrls')
        await admin_conn.execute(f'create role "{foreign_role}" nologin nosuperuser nobypassrls')
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
        owner_conn = await asyncpg.connect(owner_url)
        await migrate(owner_conn)
        await grant_app_role(owner_conn, app_role)

        await owner_conn.execute(
            "insert into users (oid, tid, display_name) values "
            "('oid-alice', 'tenant-1', 'Alice'), "
            "('oid-bob', 'tenant-1', 'Bob'), "
            "('oid-disabled', 'tenant-1', 'Disabled')"
        )
        await owner_conn.execute("update users set disabled_at = now() where oid = 'oid-disabled'")

        await owner_conn.execute(
            "insert into namespaces (kind, external_key, alias) values "
            "('user', 'oid-alice', 'alice')"
        )
        await owner_conn.execute(
            "insert into namespaces (kind, external_key, alias) values ('user', 'oid-bob', 'bob')"
        )
        group_ns_id = await owner_conn.fetchval(
            "insert into namespaces (kind, external_key, alias) values "
            "('group', 'grp-1', 'payments') returning id"
        )
        project_ns_id = await owner_conn.fetchval(
            "insert into namespaces (kind, external_key, alias) values "
            "('project', 'proj-1', 'proj-atlas') returning id"
        )
        await owner_conn.execute(
            "insert into namespaces (kind, external_key, alias) values ('org', 'org', 'org')"
        )

        # Alice is a direct reader of proj-atlas, but her group ('grp-1') is a
        # writer there - the resolver must report the stronger of the two.
        await owner_conn.execute(
            "insert into project_members (namespace_id, principal_kind, principal_id, role) "
            "values ($1, 'user', 'oid-alice', 'reader')",
            project_ns_id,
        )
        await owner_conn.execute(
            "insert into project_members (namespace_id, principal_kind, principal_id, role) "
            "values ($1, 'group', 'grp-1', 'writer')",
            project_ns_id,
        )
        await owner_conn.execute(
            "insert into namespace_settings (namespace_id, group_write) values ($1, 'curators')",
            group_ns_id,
        )
        await owner_conn.execute(
            "insert into namespace_settings (namespace_id, project_write) values ($1, 'readers')",
            project_ns_id,
        )

        # A note alice can write to (her own personal namespace), for the
        # `vault_revisions.author_oid` tests below.
        await owner_conn.execute(
            "insert into vault_notes "
            "(id, namespace, path, content, version, current_revision) "
            "values ('note-alice', 'alice', 'alice/fact/note-alice.md', 'content', 'v1', 1)"
        )

        yield NamespaceRegistryDb(owner_url=owner_url, app_role=app_role, foreign_role=foreign_role)
    finally:
        if owner_conn is not None:
            await owner_conn.close()
        await admin_conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity "
            "where datname = $1 and pid <> pg_backend_pid()",
            db_name,
        )
        await admin_conn.execute(f'drop database if exists "{db_name}"')
        await admin_conn.execute(f'drop role if exists "{app_role}"')
        await admin_conn.execute(f'drop role if exists "{foreign_role}"')
        await admin_conn.execute(f'drop role if exists "{owner_role}"')
        await admin_conn.close()


class TestEnsurePersonalNamespace:
    async def test_creates_a_row_only_for_the_calling_oid(
        self, registry_db: NamespaceRegistryDb
    ) -> None:
        conn = await _connect_as(registry_db.owner_url)
        try:
            async with request_identity(conn, role=registry_db.app_role, oid="oid-carol", roles=[]):
                alias = await conn.fetchval("select mm_ensure_personal_ns()")

            row = await conn.fetchrow(
                "select alias from namespaces where kind = 'user' and external_key = $1",
                "oid-carol",
            )
        finally:
            await conn.close()

        assert alias is not None
        assert alias.startswith("u-")
        assert row is not None
        assert row["alias"] == alias

    async def test_returns_the_existing_alias_idempotently(
        self, registry_db: NamespaceRegistryDb
    ) -> None:
        conn = await _connect_as(registry_db.owner_url)
        try:
            async with request_identity(conn, role=registry_db.app_role, oid="oid-dave", roles=[]):
                first = await conn.fetchval("select mm_ensure_personal_ns()")
            async with request_identity(conn, role=registry_db.app_role, oid="oid-dave", roles=[]):
                second = await conn.fetchval("select mm_ensure_personal_ns()")

            count = await conn.fetchval(
                "select count(*) from namespaces where kind = 'user' and external_key = $1",
                "oid-dave",
            )
        finally:
            await conn.close()

        assert first == second
        assert count == 1

    async def test_two_concurrent_calls_yield_exactly_one_row(
        self, registry_db: NamespaceRegistryDb
    ) -> None:
        async def call() -> str | None:
            conn = await _connect_as(registry_db.owner_url)
            try:
                async with request_identity(
                    conn, role=registry_db.app_role, oid="oid-concurrent", roles=[]
                ):
                    value = await conn.fetchval("select mm_ensure_personal_ns()")
                    return str(value) if value is not None else None
            finally:
                await conn.close()

        alias_a, alias_b = await asyncio.gather(call(), call())

        conn = await _connect_as(registry_db.owner_url)
        try:
            count = await conn.fetchval(
                "select count(*) from namespaces where kind = 'user' and external_key = $1",
                "oid-concurrent",
            )
        finally:
            await conn.close()

        assert alias_a is not None
        assert alias_a == alias_b
        assert count == 1

    async def test_empty_identity_returns_null_and_creates_no_row(
        self, registry_db: NamespaceRegistryDb
    ) -> None:
        conn = await _connect_as(registry_db.owner_url)
        try:
            async with request_identity(conn, role=registry_db.app_role, oid="", roles=[]):
                alias = await conn.fetchval("select mm_ensure_personal_ns()")

            count = await conn.fetchval(
                "select count(*) from namespaces where kind = 'user' and external_key = ''"
            )
        finally:
            await conn.close()

        assert alias is None
        assert count == 0


class TestPrincipalNamespaces:
    async def test_never_returns_a_foreign_personal_row(
        self, registry_db: NamespaceRegistryDb
    ) -> None:
        conn = await _connect_as(registry_db.owner_url)
        try:
            async with request_identity(
                conn, role=registry_db.app_role, oid="oid-alice", roles=["Memory.User"]
            ):
                rows = await conn.fetch(
                    "select kind, alias from mm_principal_namespaces($1) where kind = 'user'",
                    [],
                )
        finally:
            await conn.close()

        assert [dict(row) for row in rows] == [{"kind": "user", "alias": "alice"}]

    async def test_groups_projects_and_org_with_strongest_project_role_and_settings(
        self, registry_db: NamespaceRegistryDb
    ) -> None:
        conn = await _connect_as(registry_db.owner_url)
        try:
            async with request_identity(
                conn, role=registry_db.app_role, oid="oid-alice", roles=["Memory.User"]
            ):
                rows = await conn.fetch(
                    "select kind, alias, role, group_write, project_write "
                    "from mm_principal_namespaces($1)",
                    ["grp-1"],
                )
        finally:
            await conn.close()

        by_alias = {row["alias"]: dict(row) for row in rows}

        assert set(by_alias) == {"alice", "payments", "proj-atlas", "org"}
        assert by_alias["payments"]["kind"] == "group"
        assert by_alias["payments"]["group_write"] == "curators"
        assert by_alias["proj-atlas"]["kind"] == "project"
        # alice is a direct reader, but her group is a writer: the stronger wins.
        assert by_alias["proj-atlas"]["role"] == "writer"
        assert by_alias["proj-atlas"]["project_write"] == "readers"
        assert by_alias["org"]["kind"] == "org"

    async def test_disabled_user_gets_no_rows(self, registry_db: NamespaceRegistryDb) -> None:
        conn = await _connect_as(registry_db.owner_url)
        try:
            async with request_identity(
                conn, role=registry_db.app_role, oid="oid-disabled", roles=["Memory.User"]
            ):
                rows = await conn.fetch("select * from mm_principal_namespaces($1)", [])
        finally:
            await conn.close()

        assert rows == []


class TestRevisionAuthorship:
    async def test_app_role_write_records_the_identitys_own_oid(
        self, registry_db: NamespaceRegistryDb
    ) -> None:
        conn = await _connect_as(registry_db.owner_url)
        try:
            async with request_identity(
                conn, role=registry_db.app_role, oid="oid-alice", roles=["Memory.User"]
            ):
                await conn.execute(
                    "insert into vault_revisions "
                    "(note_id, revision, path, content, version, author, client) "
                    "values ('note-alice', 1, 'alice/fact/note-alice.md', 'content', 'v1', "
                    "'alice', 'pytest')"
                )

            author_oid = await conn.fetchval(
                "select author_oid from vault_revisions where note_id = 'note-alice'"
            )
        finally:
            await conn.close()

        assert author_oid == "oid-alice"

    async def test_app_role_cannot_forge_a_different_author_oid(
        self, registry_db: NamespaceRegistryDb
    ) -> None:
        conn = await _connect_as(registry_db.owner_url)
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with request_identity(
                    conn, role=registry_db.app_role, oid="oid-alice", roles=["Memory.User"]
                ):
                    await conn.execute(
                        "insert into vault_revisions "
                        "(note_id, revision, path, content, version, author, client, "
                        "author_oid) "
                        "values ('note-alice', 1, 'alice/fact/note-alice.md', 'content', 'v1', "
                        "'alice', 'pytest', 'oid-bob')"
                    )
        finally:
            await conn.close()

    async def test_owner_write_leaves_author_oid_null(
        self, registry_db: NamespaceRegistryDb
    ) -> None:
        conn = await _connect_as(registry_db.owner_url)
        try:
            await conn.execute(
                "insert into vault_revisions "
                "(note_id, revision, path, content, version, author, client) "
                "values ('note-alice', 1, 'alice/fact/note-alice.md', 'content', 'v1', "
                "'system', 'reindex')"
            )
            author_oid = await conn.fetchval(
                "select author_oid from vault_revisions where note_id = 'note-alice'"
            )
        finally:
            await conn.close()

        assert author_oid is None


class TestFunctionGrants:
    async def test_app_role_has_execute_public_does_not(
        self, registry_db: NamespaceRegistryDb
    ) -> None:
        conn = await _connect_as(registry_db.owner_url)
        try:
            app_role_ensure = await conn.fetchval(
                "select has_function_privilege($1, 'mm_ensure_personal_ns()', 'EXECUTE')",
                registry_db.app_role,
            )
            app_role_resolve = await conn.fetchval(
                "select has_function_privilege($1, 'mm_principal_namespaces(text[])', 'EXECUTE')",
                registry_db.app_role,
            )
            foreign_ensure = await conn.fetchval(
                "select has_function_privilege($1, 'mm_ensure_personal_ns()', 'EXECUTE')",
                registry_db.foreign_role,
            )
            foreign_resolve = await conn.fetchval(
                "select has_function_privilege($1, 'mm_principal_namespaces(text[])', 'EXECUTE')",
                registry_db.foreign_role,
            )
        finally:
            await conn.close()

        assert app_role_ensure is True
        assert app_role_resolve is True
        assert foreign_ensure is False
        assert foreign_resolve is False
