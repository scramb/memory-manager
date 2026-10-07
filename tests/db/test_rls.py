# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for namespace row-level security (ADR-0008 + addendum, #100).

Mirrors `tests/db/test_migrations.py:99-159`'s non-superuser shape: `mm`
(the connection `MM_TEST_DATABASE_URL` names) is a superuser locally, so
every test here creates its own non-superuser owner role and a database
owned by it, applies every migration as that role, and only ever queries
as either the owner or the request-serving app role created alongside it -
never as `mm` itself. `vector` is created by the admin (superuser)
connection first, exactly like CNPG's `postInitApplicationSQL` bootstrap
step the sibling test documents.

The app role is `NOLOGIN`: nothing here (and nothing in `db/rls.py`) ever
connects as it directly. Every "as the app role" query below is the owner
connection switching role for one transaction with `db.rls.request_identity`
- the shape #101 will wire into the request path.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from memory_manager.db.migrate import migrate
from memory_manager.db.rls import RoleRefused, grant_app_role, request_identity


@dataclass(frozen=True)
class Seed:
    """IDs the fixture only knows once it has inserted the rows (#100).

    The fixed identities themselves - alice (`oid-alice`, member of the
    payments group, reads proj-atlas), bob (`oid-bob`, writes proj-atlas,
    no group membership) and the break-glass admin (`oid-admin`, never in
    `users`/`project_members`) - are referenced by their literal oid
    strings in the tests below instead, since they never change.
    """

    break_glass_valid: int
    break_glass_expired: int


@dataclass(frozen=True)
class RlsDb:
    owner_url: str
    app_role: str
    foreign_role: str
    seed: Seed


async def _connect_as(url: str) -> asyncpg.Connection:
    return await asyncpg.connect(url)


@pytest_asyncio.fixture
async def rls_db(admin_database_url: str) -> AsyncIterator[RlsDb]:
    db_name = f"mm_test_rls_{secrets.token_hex(8)}"
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
        bootstrap_url = f"{base}/{db_name}"

        bootstrap_conn = await asyncpg.connect(bootstrap_url)
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

        now = datetime.now(UTC)

        async def seed_namespace(kind: str, external_key: str, alias: str) -> int:
            value = await owner_conn.fetchval(
                "insert into namespaces (kind, external_key, alias) values ($1, $2, $3) "
                "returning id",
                kind,
                external_key,
                alias,
            )
            return int(value)

        for oid, display_name in (
            ("oid-alice", "Alice"),
            ("oid-bob", "Bob"),
            ("oid-carol", "Carol"),
            ("oid-dave", "Dave"),  # deprovisioning target, TestDeprovisioning disables this one
        ):
            await owner_conn.execute(
                "insert into users (oid, tid, display_name) values ($1, 'tenant-1', $2)",
                oid,
                display_name,
            )

        await seed_namespace("user", "oid-alice", "alice")
        await seed_namespace("user", "oid-bob", "bob")
        carol_ns_id = await seed_namespace("user", "oid-carol", "carol")
        await seed_namespace("user", "oid-dave", "dave")
        group_ns_id = await seed_namespace("group", "grp-1", "payments")
        project_ns_id = await seed_namespace("project", "proj-1", "proj-atlas")
        await seed_namespace("org", "org", "org")

        await owner_conn.execute(
            "insert into user_groups (oid, group_id) values ($1, $2)", "oid-alice", "grp-1"
        )
        await owner_conn.execute(
            "insert into project_members (namespace_id, principal_kind, principal_id, role) "
            "values ($1, 'user', $2, 'reader')",
            project_ns_id,
            "oid-alice",
        )
        await owner_conn.execute(
            "insert into project_members (namespace_id, principal_kind, principal_id, role) "
            "values ($1, 'user', $2, 'writer')",
            project_ns_id,
            "oid-bob",
        )
        await owner_conn.execute(
            "insert into namespace_settings (namespace_id, group_write) values ($1, 'curators')",
            group_ns_id,
        )
        await owner_conn.execute(
            "insert into namespace_settings (namespace_id, project_write) values ($1, 'writers')",
            project_ns_id,
        )

        break_glass_valid = await owner_conn.fetchval(
            "insert into break_glass_grants "
            "(namespace_id, requester, reason, approved, expires_at) "
            "values ($1, $2, 'incident review', true, $3) returning id",
            carol_ns_id,
            "oid-admin",
            now + timedelta(hours=1),
        )
        break_glass_expired = await owner_conn.fetchval(
            "insert into break_glass_grants "
            "(namespace_id, requester, reason, approved, expires_at) "
            "values ($1, $2, 'incident review', true, $3) returning id",
            carol_ns_id,
            "oid-admin",
            now - timedelta(hours=1),
        )

        async def seed_note(note_id: str, namespace: str) -> None:
            path = f"{namespace}/fact/{note_id}.md"
            await owner_conn.execute(
                "insert into notes "
                "(id, path, namespace, type, slug, title, description, created, updated, "
                "file_hash) "
                "values ($1, $2, $3, 'fact', $1, 'title', 'description', $4, $4, 'deadbeef')",
                note_id,
                path,
                namespace,
                now,
            )
            await owner_conn.execute(
                "insert into chunks (note_id, ord, text) values ($1, 0, 'chunk text')", note_id
            )
            await owner_conn.execute(
                "insert into links (source_id, target_raw) values ($1, 'nowhere')", note_id
            )
            await owner_conn.execute(
                "insert into vault_notes "
                "(id, namespace, path, content, version, current_revision) "
                "values ($1, $2, $3, 'content', 'v1', 1)",
                note_id,
                namespace,
                path,
            )
            await owner_conn.execute(
                "insert into vault_revisions "
                "(note_id, revision, path, content, version, author, client) "
                "values ($1, 1, $2, 'content', 'v1', 'tester', 'pytest')",
                note_id,
                path,
            )

        for note_id, namespace in (
            ("note-alice", "alice"),
            ("note-bob", "bob"),
            ("note-payments", "payments"),
            ("note-atlas", "proj-atlas"),
            ("note-org", "org"),
            ("note-carol", "carol"),
            ("note-dave", "dave"),
        ):
            await seed_note(note_id, namespace)

        seed = Seed(
            break_glass_valid=break_glass_valid,
            break_glass_expired=break_glass_expired,
        )

        yield RlsDb(owner_url=owner_url, app_role=app_role, foreign_role=foreign_role, seed=seed)
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


class TestRowLevelSecurityFlags:
    async def test_force_rls_enabled_on_every_content_table(self, rls_db: RlsDb) -> None:
        conn = await _connect_as(rls_db.owner_url)
        try:
            rows = await conn.fetch(
                "select relname, relrowsecurity, relforcerowsecurity from pg_class "
                "where relname in ('vault_notes', 'vault_revisions', 'notes', 'chunks', 'links')"
            )
        finally:
            await conn.close()

        assert len(rows) == 5
        for row in rows:
            assert row["relrowsecurity"] is True
            assert row["relforcerowsecurity"] is True

    async def test_app_role_is_not_superuser_bypassrls_or_owner(self, rls_db: RlsDb) -> None:
        conn = await _connect_as(rls_db.owner_url)
        try:
            role_row = await conn.fetchrow(
                "select rolsuper, rolbypassrls from pg_roles where rolname = $1", rls_db.app_role
            )
            owner = await conn.fetchval("select current_user")
        finally:
            await conn.close()

        assert role_row is not None
        assert role_row["rolsuper"] is False
        assert role_row["rolbypassrls"] is False
        assert rls_db.app_role != owner

    async def test_functions_are_security_definer_with_pinned_search_path(
        self, rls_db: RlsDb
    ) -> None:
        conn = await _connect_as(rls_db.owner_url)
        try:
            rows = await conn.fetch(
                "select proname, prosecdef, proconfig from pg_proc "
                "where proname in ('mm_readable_ns', 'mm_writable_ns')"
            )
            foreign_has_execute = await conn.fetchval(
                "select has_function_privilege($1, 'mm_readable_ns()', 'EXECUTE')",
                rls_db.foreign_role,
            )
        finally:
            await conn.close()

        assert len(rows) == 2
        for row in rows:
            assert row["prosecdef"] is True
            assert row["proconfig"] is not None
            assert any(entry.startswith("search_path=") for entry in row["proconfig"])
        assert foreign_has_execute is False

    async def test_namespace_resolution_functions_are_security_definer_with_pinned_search_path(
        self, rls_db: RlsDb
    ) -> None:
        """`0009_namespace_resolution.sql`'s `mm_ensure_personal_ns`/
        `mm_principal_namespaces` get the same treatment `mm_readable_ns`/
        `mm_writable_ns` already get above - `SECURITY DEFINER`, a pinned
        `search_path`, and `EXECUTE` revoked from a role with no grant."""
        conn = await _connect_as(rls_db.owner_url)
        try:
            rows = await conn.fetch(
                "select proname, prosecdef, proconfig from pg_proc "
                "where proname in ('mm_ensure_personal_ns', 'mm_principal_namespaces')"
            )
            foreign_has_execute_ensure = await conn.fetchval(
                "select has_function_privilege($1, 'mm_ensure_personal_ns()', 'EXECUTE')",
                rls_db.foreign_role,
            )
            foreign_has_execute_resolve = await conn.fetchval(
                "select has_function_privilege($1, 'mm_principal_namespaces(text[])', 'EXECUTE')",
                rls_db.foreign_role,
            )
            app_role_has_execute_ensure = await conn.fetchval(
                "select has_function_privilege($1, 'mm_ensure_personal_ns()', 'EXECUTE')",
                rls_db.app_role,
            )
            app_role_has_execute_resolve = await conn.fetchval(
                "select has_function_privilege($1, 'mm_principal_namespaces(text[])', 'EXECUTE')",
                rls_db.app_role,
            )
        finally:
            await conn.close()

        assert len(rows) == 2
        for row in rows:
            assert row["prosecdef"] is True
            assert row["proconfig"] is not None
            assert any(entry.startswith("search_path=") for entry in row["proconfig"])
        assert foreign_has_execute_ensure is False
        assert foreign_has_execute_resolve is False
        assert app_role_has_execute_ensure is True
        assert app_role_has_execute_resolve is True

    async def test_grant_app_role_refuses_superuser_bypassrls_and_owner(
        self, rls_db: RlsDb
    ) -> None:
        conn = await _connect_as(rls_db.owner_url)
        try:
            owner = await conn.fetchval("select current_user")
            with pytest.raises(RoleRefused):
                await grant_app_role(conn, owner)
        finally:
            await conn.close()


class TestReadableAndWritableSets:
    async def test_select_without_where_returns_only_readable_rows(self, rls_db: RlsDb) -> None:
        conn = await _connect_as(rls_db.owner_url)
        try:
            async with request_identity(
                conn, role=rls_db.app_role, oid="oid-alice", roles=["Memory.User"]
            ):
                namespaces = {
                    row["namespace"] for row in await conn.fetch("select namespace from notes")
                }
                vault_namespaces = {
                    row["namespace"]
                    for row in await conn.fetch("select namespace from vault_notes")
                }
        finally:
            await conn.close()

        # alice: own namespace, the payments group (member), proj-atlas (project
        # member) and org (any identity with a memory role) - not bob, not carol.
        assert namespaces == {"alice", "payments", "proj-atlas", "org"}
        assert vault_namespaces == namespaces

    async def test_chunks_and_links_follow_their_parent_notes_row(self, rls_db: RlsDb) -> None:
        conn = await _connect_as(rls_db.owner_url)
        try:
            async with request_identity(
                conn, role=rls_db.app_role, oid="oid-alice", roles=["Memory.User"]
            ):
                chunk_count = await conn.fetchval("select count(*) from chunks")
                link_count = await conn.fetchval("select count(*) from links")
                revision_count = await conn.fetchval("select count(*) from vault_revisions")
        finally:
            await conn.close()

        assert chunk_count == 4
        assert link_count == 4
        assert revision_count == 4

    async def test_update_and_delete_without_where_touch_only_writable_rows(
        self, rls_db: RlsDb
    ) -> None:
        conn = await _connect_as(rls_db.owner_url)
        try:
            async with request_identity(
                conn, role=rls_db.app_role, oid="oid-alice", roles=["Memory.User"]
            ):
                # alice's writable set is just {alice}: payments requires
                # Memory.Curator (group_write = 'curators'), proj-atlas requires
                # writer/owner (alice is a reader), org requires Curator/Admin.
                update_tag = await conn.execute("update notes set title = 'retitled'")
                delete_tag = await conn.execute("delete from chunks")
        finally:
            await conn.close()

        assert update_tag == "UPDATE 1"
        assert delete_tag == "DELETE 1"

        owner_conn = await _connect_as(rls_db.owner_url)
        try:
            retitled = await owner_conn.fetchval(
                "select namespace from notes where title = 'retitled'"
            )
            remaining_chunks = await owner_conn.fetchval("select count(*) from chunks")
        finally:
            await owner_conn.close()

        assert retitled == "alice"
        assert remaining_chunks == 6

    async def test_insert_into_a_foreign_namespace_fails(self, rls_db: RlsDb) -> None:
        conn = await _connect_as(rls_db.owner_url)
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with request_identity(
                    conn, role=rls_db.app_role, oid="oid-alice", roles=["Memory.User"]
                ):
                    await conn.execute(
                        "insert into notes "
                        "(id, path, namespace, type, slug, title, description, created, "
                        "updated, file_hash) "
                        "values ('note-foreign', 'bob/fact/note-foreign.md', 'bob', 'fact', "
                        "'note-foreign', 't', 'd', now(), now(), 'deadbeef')"
                    )
        finally:
            await conn.close()

    async def test_missing_identity_resolves_to_zero_rows_without_error(
        self, rls_db: RlsDb
    ) -> None:
        conn = await _connect_as(rls_db.owner_url)
        try:
            async with conn.transaction():
                await conn.execute("select set_config('role', $1, true)", rls_db.app_role)
                readable = await conn.fetchval("select mm_readable_ns()")
                count = await conn.fetchval("select count(*) from notes")
        finally:
            await conn.close()

        assert readable == []
        assert count == 0

    async def test_identity_from_a_previous_transaction_does_not_leak_without_error(
        self, rls_db: RlsDb
    ) -> None:
        conn = await _connect_as(rls_db.owner_url)
        try:
            async with request_identity(
                conn, role=rls_db.app_role, oid="oid-alice", roles=["Memory.User"]
            ):
                pass  # commits, reverting role and every app.* setting

            # Role reverted to the owner automatically; re-switch to the app role
            # for a second transaction, but deliberately never set app.oid again.
            async with conn.transaction():
                await conn.execute("select set_config('role', $1, true)", rls_db.app_role)
                leftover = await conn.fetchval("select current_setting('app.oid', true)")
                readable = await conn.fetchval("select mm_readable_ns()")
                count = await conn.fetchval("select count(*) from notes")
        finally:
            await conn.close()

        assert leftover == ""  # the documented empty-string quirk, not NULL
        assert readable == []
        assert count == 0

    async def test_non_superuser_owner_sees_everything(self, rls_db: RlsDb) -> None:
        conn = await _connect_as(rls_db.owner_url)
        try:
            count = await conn.fetchval("select count(*) from notes")
        finally:
            await conn.close()

        assert count == 7


async def _content_table_counts(conn: asyncpg.Connection) -> dict[str, int]:
    """Row counts of the five RLS-protected tables, as the caller's current
    role/identity see them - literal queries, not string-built SQL (S608)."""
    return {
        "notes": await conn.fetchval("select count(*) from notes"),
        "vault_notes": await conn.fetchval("select count(*) from vault_notes"),
        "chunks": await conn.fetchval("select count(*) from chunks"),
        "links": await conn.fetchval("select count(*) from links"),
        "vault_revisions": await conn.fetchval("select count(*) from vault_revisions"),
    }


class TestDeprovisioning:
    async def test_disabling_a_user_empties_read_and_write_sets(self, rls_db: RlsDb) -> None:
        """ADR-0006's deprovisioning guarantee, enforced in the database: a
        disabled user (`users.disabled_at is not null`) loses read and write
        access to their own personal namespace, not just group/project/org
        access - even though `namespaces`/`project_members` never change."""
        conn = await _connect_as(rls_db.owner_url)
        try:
            async with request_identity(
                conn, role=rls_db.app_role, oid="oid-dave", roles=["Memory.User"]
            ):
                before_readable = await conn.fetchval("select mm_readable_ns()")
                before_writable = await conn.fetchval("select mm_writable_ns()")
                before_counts = await _content_table_counts(conn)

            assert "dave" in before_readable
            assert "dave" in before_writable
            # dave also reads `org` (any identity with a memory role), so the
            # readable set - and these raw counts - cover two notes, not one.
            assert before_counts == {
                "notes": 2,
                "vault_notes": 2,
                "chunks": 2,
                "links": 2,
                "vault_revisions": 2,
            }

            await conn.execute("update users set disabled_at = now() where oid = 'oid-dave'")

            async with request_identity(
                conn, role=rls_db.app_role, oid="oid-dave", roles=["Memory.User"]
            ):
                after_readable = await conn.fetchval("select mm_readable_ns()")
                after_writable = await conn.fetchval("select mm_writable_ns()")
                after_counts = await _content_table_counts(conn)

            assert after_readable == []
            assert after_writable == []
            assert after_counts == {
                "notes": 0,
                "vault_notes": 0,
                "chunks": 0,
                "links": 0,
                "vault_revisions": 0,
            }

            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with request_identity(
                    conn, role=rls_db.app_role, oid="oid-dave", roles=["Memory.User"]
                ):
                    await conn.execute(
                        "insert into notes "
                        "(id, path, namespace, type, slug, title, description, created, "
                        "updated, file_hash) "
                        "values ('note-dave-2', 'dave/fact/note-dave-2.md', 'dave', 'fact', "
                        "'note-dave-2', 't', 'd', now(), now(), 'deadbeef')"
                    )

            async with request_identity(
                conn, role=rls_db.app_role, oid="oid-dave", roles=["Memory.User"]
            ):
                update_tag = await conn.execute(
                    "update notes set title = 'dave-disabled' where namespace = 'dave'"
                )
        finally:
            await conn.close()

        assert update_tag == "UPDATE 0"


class TestGroupProjectOrgAndBreakGlass:
    async def test_group_write_requires_curator_when_setting_is_curators(
        self, rls_db: RlsDb
    ) -> None:
        conn = await _connect_as(rls_db.owner_url)
        try:
            async with request_identity(
                conn, role=rls_db.app_role, oid="oid-alice", roles=["Memory.User"]
            ):
                plain_member_writable = await conn.fetchval("select mm_writable_ns()")

            async with request_identity(
                conn, role=rls_db.app_role, oid="oid-alice", roles=["Memory.Curator"]
            ):
                curator_member_writable = await conn.fetchval("select mm_writable_ns()")
        finally:
            await conn.close()

        assert "payments" not in plain_member_writable
        assert "payments" in curator_member_writable

    async def test_org_write_requires_curator_or_admin_role(self, rls_db: RlsDb) -> None:
        conn = await _connect_as(rls_db.owner_url)
        try:
            async with request_identity(
                conn, role=rls_db.app_role, oid="oid-bob", roles=["Memory.User"]
            ):
                plain_user_writable = await conn.fetchval("select mm_writable_ns()")
                plain_user_readable = await conn.fetchval("select mm_readable_ns()")

            async with request_identity(
                conn, role=rls_db.app_role, oid="oid-bob", roles=["Memory.Curator"]
            ):
                curator_writable = await conn.fetchval("select mm_writable_ns()")
        finally:
            await conn.close()

        assert "org" in plain_user_readable
        assert "org" not in plain_user_writable
        assert "org" in curator_writable
        # bob's project membership (role 'writer', setting 'writers') is
        # unaffected by whichever of these roles he carries.
        assert "proj-atlas" in plain_user_writable
        assert "proj-atlas" in curator_writable

    async def test_break_glass_grants_read_but_never_write(self, rls_db: RlsDb) -> None:
        conn = await _connect_as(rls_db.owner_url)
        try:
            async with request_identity(
                conn,
                role=rls_db.app_role,
                oid="oid-admin",
                roles=["Memory.Admin"],
                break_glass=rls_db.seed.break_glass_valid,
            ):
                valid_readable = await conn.fetchval("select mm_readable_ns()")
                valid_writable = await conn.fetchval("select mm_writable_ns()")

            async with request_identity(
                conn,
                role=rls_db.app_role,
                oid="oid-admin",
                roles=["Memory.Admin"],
                break_glass=rls_db.seed.break_glass_expired,
            ):
                expired_readable = await conn.fetchval("select mm_readable_ns()")
        finally:
            await conn.close()

        assert "carol" in valid_readable
        assert "carol" not in valid_writable
        assert "carol" not in expired_readable


class TestPoolConnectionReuse:
    async def test_role_and_identity_do_not_survive_the_next_checkout(self, rls_db: RlsDb) -> None:
        pool = await asyncpg.create_pool(rls_db.owner_url, min_size=1, max_size=1)
        try:
            async with pool.acquire() as conn:
                owner = await conn.fetchval("select current_user")
                async with request_identity(
                    conn, role=rls_db.app_role, oid="oid-alice", roles=["Memory.User"]
                ):
                    assert await conn.fetchval("select current_user") == rls_db.app_role
                # Transaction committed: SET LOCAL ROLE and every app.* setting
                # already reverted on this same physical connection.
                assert await conn.fetchval("select current_user") == owner
                assert await conn.fetchval("select current_setting('app.oid', true)") == ""

                # A plain, session-level (non-local) SET is the contrasting
                # case: it survives past its own transaction, but the pool's
                # `RESET ALL` on release clears it anyway.
                await conn.execute("select set_config('app.oid', 'leaked', false)")
                assert await conn.fetchval("select current_setting('app.oid', true)") == "leaked"

            async with pool.acquire() as conn:
                # Same physical connection (max_size=1); the pool's release-time
                # reset already cleared the plain session-level setting above.
                assert await conn.fetchval("select current_setting('app.oid', true)") in (
                    None,
                    "",
                )
                assert await conn.fetchval("select current_user") == owner
        finally:
            await pool.close()
