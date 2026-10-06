# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `migrate()` and the schema it creates (#24)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

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

        assert applied == ["0001_index_schema", "0002_static_tokens", "0003_oauth"]
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
        }

    async def test_running_twice_applies_nothing_the_second_time(
        self, conn: asyncpg.Connection
    ) -> None:
        first = await migrate(conn)
        second = await migrate(conn)

        assert first == ["0001_index_schema", "0002_static_tokens", "0003_oauth"]
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
        ]

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
