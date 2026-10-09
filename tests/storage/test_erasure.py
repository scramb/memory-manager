# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `storage.erasure` (ADR-0007 §3 + addendum 2026-10-08, #231).

Mirrors `tests/db/test_rls.py`'s own shape: a dedicated non-superuser owner
role and `NOLOGIN` app role, migrated with `backend="postgres"` (the one
`migrate()` mode that creates `erasure_log`, `tests/db/test_migrations.py`'s
own `TestMigrateBackendPostgres`). Every "as the app role" check below
switches the owner connection for one transaction with `db.rls.
request_identity`, the same technique `test_rls.py` uses - nothing here
ever connects as the app role directly, since it is `NOLOGIN`.

Every seeded note's slug, path, title, tags and body carry `_SENTINEL`; its
own `id` deliberately does not, so `erasure_log`/`jobs` - which record IDs
only, never content (CLAUDE.md) - can legitimately still name the id
afterwards while every content-bearing column is checked to have lost the
sentinel for good.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from memory_manager.db.migrate import migrate
from memory_manager.db.rls import grant_app_role, request_identity
from memory_manager.storage.base import NotFound
from memory_manager.storage.erasure import erase_namespace, erase_note, erase_user

_SENTINEL = "zzsentinelzz"


@dataclass(frozen=True)
class ErasureDb:
    owner_url: str
    app_role: str


@pytest_asyncio.fixture
async def erasure_db(admin_database_url: str) -> AsyncIterator[ErasureDb]:
    db_name = f"mm_test_erasure_{secrets.token_hex(8)}"
    owner_role = f"mm_test_owner_{secrets.token_hex(8)}"
    owner_password = secrets.token_urlsafe(16)
    app_role = f"mm_test_app_{secrets.token_hex(8)}"

    admin_conn = await asyncpg.connect(admin_database_url)
    owner_conn: asyncpg.Connection | None = None
    try:
        await admin_conn.execute(
            f"create role \"{owner_role}\" login password '{owner_password}' nosuperuser"
        )
        await admin_conn.execute(f'create database "{db_name}" owner "{owner_role}"')
        await admin_conn.execute(f'create role "{app_role}" nologin nosuperuser nobypassrls')
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
        await migrate(owner_conn, backend="postgres")
        await grant_app_role(owner_conn, app_role)

        yield ErasureDb(owner_url=owner_url, app_role=app_role)
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
        await admin_conn.execute(f'drop role if exists "{owner_role}"')
        await admin_conn.close()


async def _seed_namespace(
    conn: asyncpg.Connection, kind: str, external_key: str, alias: str
) -> None:
    await conn.execute(
        "insert into namespaces (kind, external_key, alias) values ($1, $2, $3)",
        kind,
        external_key,
        alias,
    )


async def _seed_user(conn: asyncpg.Connection, oid: str, display_name: str) -> None:
    await conn.execute(
        "insert into users (oid, tid, display_name) values ($1, 'tenant-1', $2)", oid, display_name
    )


async def _seed_note(
    conn: asyncpg.Connection,
    note_id: str,
    namespace: str,
    *,
    author_oid: str | None = None,
    namespace_kind: str = "user",
) -> str:
    """Insert one note across every table a real write touches, slug/path/
    title/tags/body all carrying `_SENTINEL`; `note_id` itself never does
    (module docstring). Returns the note's path."""
    slug = f"slug-{_SENTINEL}-{note_id}"
    path = f"{namespace}/fact/{slug}.md"
    now = datetime.now(UTC)
    content = f"body mentions {_SENTINEL}\n".encode()

    await conn.execute(
        "insert into vault_notes (id, namespace, path, content, version, current_revision) "
        "values ($1, $2, $3, $4, 'v1', 1)",
        note_id,
        namespace,
        path,
        content,
    )
    await conn.execute(
        "insert into vault_revisions "
        "(note_id, revision, path, content, version, author, client, author_oid) "
        "values ($1, 1, $2, $3, 'v1', 'tester', 'pytest', $4)",
        note_id,
        path,
        content,
        author_oid,
    )
    await conn.execute(
        "insert into notes "
        "(id, path, namespace, type, slug, title, description, tags, created, updated, file_hash) "
        "values ($1, $2, $3, 'fact', $4, $5, 'description', $6, $7, $7, 'deadbeef')",
        note_id,
        path,
        namespace,
        slug,
        f"title mentions {_SENTINEL}",
        [_SENTINEL],
        now,
    )
    await conn.execute(
        "insert into chunks (note_id, namespace, namespace_kind, ord, text) "
        "values ($1, $2, $3, 0, $4)",
        note_id,
        namespace,
        namespace_kind,
        f"chunk mentions {_SENTINEL}",
    )
    await conn.execute("insert into links (source_id, target_raw) values ($1, 'nowhere')", note_id)
    await conn.execute(
        "insert into jobs (id, kind, payload) values ($1, 'embed_note', $2::jsonb)",
        f"job-{note_id}",
        json.dumps({"note_id": note_id, "version": "v1"}),
    )
    await conn.execute(
        "insert into audit_log (actor, client, op, path, outcome, detail) "
        "values ($1, 'pytest', 'write', $2, 'ok', $3::jsonb)",
        author_oid or "stdio",
        path,
        json.dumps({"version": "v1"}),
    )
    return path


async def _connect_as(url: str) -> asyncpg.Connection:
    return await asyncpg.connect(url)


async def _sentinel_count(conn: asyncpg.Connection) -> int:
    """How many rows across every content-bearing table still mention `_SENTINEL`."""
    counts = await conn.fetch(
        """
        select
            (select count(*) from vault_notes where convert_from(content, 'UTF8') like $1
                or path like $1) as vault_notes,
            (select count(*) from vault_revisions where convert_from(content, 'UTF8') like $1
                or path like $1) as vault_revisions,
            (select count(*) from notes where path like $1 or slug like $1 or title like $1
                or $2 = any(tags)) as notes,
            (select count(*) from chunks where text like $1) as chunks,
            (select count(*) from audit_log where path like $1 or detail::text like $1) as audit_log
        """,
        f"%{_SENTINEL}%",
        _SENTINEL,
    )
    return int(sum(dict(counts[0]).values()))


class TestEraseNote:
    async def test_removes_the_note_everywhere_and_redacts_its_audit_rows(
        self, erasure_db: ErasureDb
    ) -> None:
        conn = await _connect_as(erasure_db.owner_url)
        try:
            await _seed_namespace(conn, "user", "oid-alice", "alice")
            path = await _seed_note(conn, "note-1", "alice", author_oid="oid-alice")
            assert await _sentinel_count(conn) > 0

            result = await erase_note(conn, "note-1", actor="admin", reason="gdpr-request")

            assert result.target_kind == "note"
            assert result.target_ids == ("note-1",)
            assert result.row_counts["vault_notes"] == 1
            assert result.row_counts["vault_revisions"] == 1
            assert result.row_counts["notes"] == 1
            assert result.row_counts["chunks"] == 1
            assert result.row_counts["links"] == 1
            assert result.row_counts["jobs"] == 1
            assert result.row_counts["audit_log"] == 1
            assert await _sentinel_count(conn) == 0

            for table, column in (
                ("vault_notes", "id"),
                ("vault_revisions", "note_id"),
                ("notes", "id"),
                ("chunks", "note_id"),
                ("links", "source_id"),
                ("jobs", "payload->>'note_id'"),
            ):
                remaining = await conn.fetchval(
                    f"select count(*) from {table} where {column} = $1",  # noqa: S608
                    "note-1",
                )
                assert remaining == 0, table

            erasure_audit_row = await conn.fetchrow(
                "select path, detail from audit_log where client = 'erasure'"
            )
            assert erasure_audit_row is not None
            assert erasure_audit_row["path"] is None
            erasure_detail = json.loads(erasure_audit_row["detail"])
            assert erasure_detail["target_kind"] == "note"
            assert erasure_detail["erasure_log_id"] == result.erasure_log_id

            redacted = await conn.fetchrow(
                "select path, detail from audit_log where path = '[erased]'"
            )
            assert redacted is not None
            assert json.loads(redacted["detail"]) == {"version": "v1"}

            erasure_row = await conn.fetchrow(
                "select target_kind, target_ids, row_counts from erasure_log where id = $1",
                result.erasure_log_id,
            )
            assert erasure_row is not None
            assert erasure_row["target_kind"] == "note"
            assert erasure_row["target_ids"] == ["note-1"]
            assert path.startswith("alice/")
        finally:
            await conn.close()

    async def test_unknown_note_raises_not_found_and_writes_nothing(
        self, erasure_db: ErasureDb
    ) -> None:
        conn = await _connect_as(erasure_db.owner_url)
        try:
            with pytest.raises(NotFound):
                await erase_note(conn, "no-such-note", actor="admin", reason="gdpr-request")

            count = await conn.fetchval("select count(*) from erasure_log")
            assert count == 0
        finally:
            await conn.close()


class TestEraseNamespace:
    async def test_removes_every_note_in_the_namespace_and_leaves_others_untouched(
        self, erasure_db: ErasureDb
    ) -> None:
        conn = await _connect_as(erasure_db.owner_url)
        try:
            await _seed_namespace(conn, "user", "oid-alice", "alice")
            await _seed_namespace(conn, "user", "oid-bob", "bob")
            await _seed_note(conn, "note-1", "alice", author_oid="oid-alice")
            await _seed_note(conn, "note-2", "alice", author_oid="oid-alice")
            await _seed_note(conn, "note-bob", "bob", author_oid="oid-bob")

            result = await erase_namespace(conn, "alice", actor="admin", reason="decommission")

            assert result.row_counts["vault_notes"] == 2
            assert result.row_counts["notes"] == 2
            assert result.row_counts["chunks"] == 2
            assert result.row_counts["links"] == 2
            assert result.row_counts["jobs"] == 2
            assert result.row_counts["audit_log"] == 2
            assert result.row_counts["namespaces"] == 1

            remaining_alice = await conn.fetchval(
                "select count(*) from vault_notes where namespace = 'alice'"
            )
            assert remaining_alice == 0

            remaining_bob = await conn.fetchval(
                "select count(*) from vault_notes where namespace = 'bob'"
            )
            assert remaining_bob == 1
            bob_audit = await conn.fetchval(
                "select count(*) from audit_log where path like 'bob/%' and path <> '[erased]'"
            )
            assert bob_audit == 1

            alice_namespace = await conn.fetchval(
                "select count(*) from namespaces where alias = 'alice'"
            )
            assert alice_namespace == 0
            bob_namespace = await conn.fetchval(
                "select count(*) from namespaces where alias = 'bob'"
            )
            assert bob_namespace == 1
        finally:
            await conn.close()


class TestEraseUser:
    async def test_erases_the_personal_namespace_and_identity_but_keeps_shared_notes(
        self, erasure_db: ErasureDb
    ) -> None:
        conn = await _connect_as(erasure_db.owner_url)
        try:
            await _seed_namespace(conn, "user", "oid-carol", "carol")
            await _seed_namespace(conn, "group", "grp-1", "payments")
            await _seed_user(conn, "oid-carol", "Carol")
            await _seed_user(conn, "oid-erin", "Erin")
            await _seed_note(conn, "note-personal", "carol", author_oid="oid-carol")
            shared_path = await _seed_note(
                conn, "note-shared", "payments", author_oid="oid-carol", namespace_kind="group"
            )

            await conn.execute(
                "insert into user_groups (oid, group_id) values ('oid-carol', 'grp-1')"
            )
            await conn.execute(
                "insert into static_tokens (name, token_hash, scopes, namespaces, owner_oid) "
                "values ('carol-token', 'deadbeef', '{}', '{*}', 'oid-carol')"
            )
            await conn.execute(
                "insert into oauth_clients (client_id, client_info) values ('client-1', '{}')"
            )
            await conn.execute(
                "insert into oauth_tokens "
                "(token_hash, kind, client_id, subject, namespaces, scopes, family_id, "
                "client_label, expires_at, user_oid) "
                "values ('th-1', 'access', 'client-1', 'oid-carol', '{}', '{}', 'fam-1', "
                "'cli', now() + interval '1 hour', 'oid-carol')"
            )
            await conn.execute(
                "insert into account_sessions "
                "(session_hash, subject, oid, roles, login_mode, created_at, "
                "last_seen_at, expires_at) "
                "values ('sh-carol', 'oid-carol', 'oid-carol', '{}', 'entra', "
                "now(), now(), now() + interval '8 hours')"
            )
            await conn.execute(
                "insert into account_sessions "
                "(session_hash, subject, oid, roles, login_mode, created_at, "
                "last_seen_at, expires_at) "
                "values ('sh-erin', 'oid-erin', 'oid-erin', '{}', 'entra', "
                "now(), now(), now() + interval '8 hours')"
            )

            result = await erase_user(conn, "oid-carol", actor="admin", reason="gdpr-erasure")

            assert result.target_kind == "user"
            assert result.row_counts["vault_notes"] == 1
            assert result.row_counts["notes"] == 1
            assert result.row_counts["user_groups"] == 1
            assert result.row_counts["static_tokens"] == 1
            assert result.row_counts["oauth_tokens"] == 1
            assert result.row_counts["account_sessions"] == 1
            assert result.row_counts["users"] == 1
            assert result.row_counts["namespaces"] == 1
            assert result.row_counts["vault_revisions_pseudonymized"] == 1

            carol_session_remaining = await conn.fetchval(
                "select count(*) from account_sessions where oid = 'oid-carol'"
            )
            assert carol_session_remaining == 0
            erin_session_remaining = await conn.fetchval(
                "select count(*) from account_sessions where oid = 'oid-erin'"
            )
            assert erin_session_remaining == 1

            personal_remaining = await conn.fetchval(
                "select count(*) from vault_notes where namespace = 'carol'"
            )
            assert personal_remaining == 0

            shared_remaining = await conn.fetchval(
                "select count(*) from vault_notes where id = 'note-shared'"
            )
            assert shared_remaining == 1

            author = await conn.fetchrow(
                "select author, author_oid from vault_revisions where note_id = 'note-shared'"
            )
            assert author is not None
            assert author["author"] == "erased"
            assert author["author_oid"] == "erased"

            still_referenced = await conn.fetchval(
                "select count(*) from vault_revisions where author_oid = 'oid-carol'"
            )
            assert still_referenced == 0

            user_row = await conn.fetchval("select count(*) from users where oid = 'oid-carol'")
            assert user_row == 0
            tokens_row = await conn.fetchval(
                "select count(*) from static_tokens where owner_oid = 'oid-carol'"
            )
            assert tokens_row == 0
            oauth_row = await conn.fetchval(
                "select count(*) from oauth_tokens where user_oid = 'oid-carol'"
            )
            assert oauth_row == 0
            groups_row = await conn.fetchval(
                "select count(*) from user_groups where oid = 'oid-carol'"
            )
            assert groups_row == 0
            carol_namespace = await conn.fetchval(
                "select count(*) from namespaces where kind = 'user' and external_key = 'oid-carol'"
            )
            assert carol_namespace == 0
            payments_namespace = await conn.fetchval(
                "select count(*) from namespaces where alias = 'payments'"
            )
            assert payments_namespace == 1
            assert shared_path.startswith("payments/")
        finally:
            await conn.close()

    async def test_user_with_no_personal_namespace_erases_identity_only(
        self, erasure_db: ErasureDb
    ) -> None:
        conn = await _connect_as(erasure_db.owner_url)
        try:
            await _seed_user(conn, "oid-dave", "Dave")

            result = await erase_user(conn, "oid-dave", actor="admin", reason="gdpr-erasure")

            assert result.row_counts["vault_notes"] == 0
            assert result.row_counts["users"] == 1
            assert result.row_counts["namespaces"] == 0

            remaining = await conn.fetchval("select count(*) from users where oid = 'oid-dave'")
            assert remaining == 0
        finally:
            await conn.close()


class TestFailureRollsBackEverything:
    async def test_a_failure_after_every_delete_rolls_back_the_whole_transaction(
        self, erasure_db: ErasureDb, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Injects a failure into `_write_erasure_log`, the last thing `erase_note`
        does, after every content delete has already run inside the same
        transaction - proving they roll back together with it, never
        independently (the `erasure_log`/`audit_log` rows this call would
        have written are gone too, not just left empty)."""
        from memory_manager.storage import erasure as erasure_module

        conn = await _connect_as(erasure_db.owner_url)
        try:
            await _seed_namespace(conn, "user", "oid-alice", "alice")
            await _seed_note(conn, "note-1", "alice", author_oid="oid-alice")

            async def _boom(*args: object, **kwargs: object) -> int:
                raise RuntimeError("injected failure")

            monkeypatch.setattr(erasure_module, "_write_erasure_log", _boom)

            with pytest.raises(RuntimeError, match="injected failure"):
                await erase_note(conn, "note-1", actor="admin", reason="gdpr-request")

            remaining = await conn.fetchval("select count(*) from vault_notes where id = 'note-1'")
            assert remaining == 1
            log_count = await conn.fetchval("select count(*) from erasure_log")
            assert log_count == 0
            audit_count = await conn.fetchval(
                "select count(*) from audit_log where path = '[erased]'"
            )
            assert audit_count == 0
        finally:
            await conn.close()


class TestAppRoleCannotCallIt:
    async def test_app_role_has_no_privilege_on_the_tables_erasure_touches(
        self, erasure_db: ErasureDb
    ) -> None:
        conn = await _connect_as(erasure_db.owner_url)
        try:
            await _seed_namespace(conn, "user", "oid-alice", "alice")
            await _seed_note(conn, "note-1", "alice", author_oid="oid-alice")

            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with request_identity(
                    conn, role=erasure_db.app_role, oid="oid-alice", roles=["Memory.User"]
                ):
                    await erase_note(conn, "note-1", actor="oid-alice", reason="self-service")
        finally:
            await conn.close()
