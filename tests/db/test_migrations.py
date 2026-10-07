# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `migrate()` and the schema it creates (#24)."""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from memory_manager.db.migrate import migrate


async def _insert_note(conn: asyncpg.Connection, note_id: str) -> None:
    now = datetime.now(UTC)
    await conn.execute(
        """
        insert into notes (
            id, path, namespace, type, slug, title, description, created, updated, file_hash
        ) values ($1, $2, 'personal', 'fact', $3, 'title', 'description', $4, $4, 'deadbeef')
        """,
        note_id,
        f"personal/fact/{note_id}.md",
        note_id,
        now,
    )


class TestMigrate:
    async def test_applies_0001_on_a_fresh_database(self, conn: asyncpg.Connection) -> None:
        applied = await migrate(conn)

        assert applied == [
            "0001_index_schema",
            "0002_static_tokens",
            "0003_oauth",
            "0004_vault",
            "0006_shared_state",
        ]
        tables = {
            row["table_name"]
            for row in await conn.fetch(
                "select table_name from information_schema.tables where table_schema = 'public'"
            )
        }
        assert tables == {
            "schema_migrations",
            "notes",
            "chunks",
            "links",
            "audit_log",
            "static_tokens",
            "oauth_clients",
            "oauth_pending",
            "oauth_auth_codes",
            "oauth_tokens",
            "rate_limits",
            "vault_notes",
            "vault_revisions",
            "namespaces",
        }

    async def test_running_twice_applies_nothing_the_second_time(
        self, conn: asyncpg.Connection
    ) -> None:
        first = await migrate(conn)
        second = await migrate(conn)

        assert first == [
            "0001_index_schema",
            "0002_static_tokens",
            "0003_oauth",
            "0004_vault",
            "0006_shared_state",
        ]
        assert second == []

    async def test_concurrent_migrate_applies_each_migration_exactly_once(
        self, test_database_url: str
    ) -> None:
        conn_a = await asyncpg.connect(test_database_url)
        conn_b = await asyncpg.connect(test_database_url)
        try:
            applied_a, applied_b = await asyncio.gather(migrate(conn_a), migrate(conn_b))
        finally:
            await conn_a.close()
            await conn_b.close()

        assert sorted(applied_a + applied_b) == [
            "0001_index_schema",
            "0002_static_tokens",
            "0003_oauth",
            "0004_vault",
            "0006_shared_state",
        ]

    async def test_succeeds_for_a_non_superuser_role_once_vector_already_exists(
        self, admin_database_url: str
    ) -> None:
        """Mirrors the CNPG deployment shape (`deploy/database.yaml`, `charts/
        memory-manager/templates/cnpg-cluster.yaml`): `bootstrap.initdb.
        postInitApplicationSQL` creates the `vector` extension as the bootstrap
        (superuser) role; the app's own migration role is never a superuser.
        `create extension if not exists vector` must still succeed for that role
        once the extension already exists - a no-op requires no privilege a
        non-superuser role would be missing, confirmed against the local
        pgvector image before this test existed (`CREATE EXTENSION` ... NOTICE:
        extension "vector" already exists, skipping ... exit 0)."""
        db_name = f"mm_test_nonsuper_{secrets.token_hex(8)}"
        role_name = f"mm_test_role_{secrets.token_hex(8)}"
        role_password = secrets.token_urlsafe(16)

        admin_conn = await asyncpg.connect(admin_database_url)
        try:
            await admin_conn.execute(
                f"create role \"{role_name}\" login password '{role_password}' nosuperuser"
            )
            await admin_conn.execute(f'create database "{db_name}" owner "{role_name}"')

            parsed = urlsplit(admin_database_url)
            base, _, _ = admin_database_url.rpartition("/")
            bootstrap_url = f"{base}/{db_name}"

            # The bootstrap step a superuser performs once, up front - CNPG's own
            # `postInitApplicationSQL`, not this test's migration role.
            bootstrap_conn = await asyncpg.connect(bootstrap_url)
            try:
                await bootstrap_conn.execute("create extension if not exists vector")
            finally:
                await bootstrap_conn.close()

            role_url = urlunsplit(
                (
                    parsed.scheme,
                    f"{role_name}:{role_password}@{parsed.hostname}:{parsed.port}",
                    f"/{db_name}",
                    "",
                    "",
                )
            )
            role_conn = await asyncpg.connect(role_url)
            try:
                applied = await migrate(role_conn)
            finally:
                await role_conn.close()

            assert applied == [
                "0001_index_schema",
                "0002_static_tokens",
                "0003_oauth",
                "0004_vault",
                "0006_shared_state",
            ]
        finally:
            await admin_conn.execute(f'drop database if exists "{db_name}"')
            await admin_conn.execute(f'drop role if exists "{role_name}"')
            await admin_conn.close()

    async def test_tsv_lang_matches_german_and_english_stems(
        self, conn: asyncpg.Connection
    ) -> None:
        await migrate(conn)
        await _insert_note(conn, "note-de")
        await _insert_note(conn, "note-en")
        await conn.execute(
            "insert into chunks (note_id, ord, text, lang) values ($1, 0, $2, 'de')",
            "note-de",
            "In diesem Dorf stehen viele Häuser.",
        )
        await conn.execute(
            "insert into chunks (note_id, ord, text, lang) values ($1, 0, $2, 'en')",
            "note-en",
            "The village has many houses.",
        )

        de_match = await conn.fetchval(
            "select tsv_lang @@ to_tsquery('german', 'haus') from chunks where note_id = $1",
            "note-de",
        )
        en_match = await conn.fetchval(
            "select tsv_lang @@ to_tsquery('english', 'house') from chunks where note_id = $1",
            "note-en",
        )

        assert de_match is True
        assert en_match is True

    async def test_embedding_column_accepts_different_dimensions(
        self, conn: asyncpg.Connection
    ) -> None:
        await migrate(conn)
        await _insert_note(conn, "note-dims")

        await conn.execute(
            "insert into chunks (note_id, ord, text, embedding) values ($1, 0, 'a', $2::vector)",
            "note-dims",
            "[1,2,3]",
        )
        await conn.execute(
            "insert into chunks (note_id, ord, text, embedding) values ($1, 1, 'b', $2::vector)",
            "note-dims",
            "[1,2,3,4,5]",
        )

        dims = {
            row["dim"]
            for row in await conn.fetch(
                "select vector_dims(embedding) as dim from chunks where note_id = $1", "note-dims"
            )
        }
        assert dims == {3, 5}
