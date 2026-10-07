# SPDX-License-Identifier: AGPL-3.0-only
"""The connection-path indexer against the `postgres` backend (ADR-0007 §4, #98).

Every `PostgresBackend` write here is wired with a real `Indexer` over
`VaultNotesSource()` as its `index_hook`/`index_commit_hook`
(`storage.postgres.PostgresBackend.__init__`), the same wiring
`app.py`'s `"postgres"` branch does: `notes`/`chunks`/`links` land in the
same transaction as the write (`TestConnectionPathIndexing`), embeddings
are scheduled only after that transaction commits and never block the
write (`TestBackgroundEmbeddings`), and `reindex_full` rebuilds the index
from `vault_notes` alone, byte-for-byte like the connection path already
left it (`TestReindexFull`). `TestCli` drives the same rebuild through
`memory-manager reindex --full`.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import asyncpg
import pytest
import pytest_asyncio
from storage.contract import note_bytes

from memory_manager import cli
from memory_manager.db.migrate import migrate
from memory_manager.index.embeddings import EmbeddingError
from memory_manager.index.indexer import Indexer, VaultNotesSource
from memory_manager.search import fulltext_search
from memory_manager.storage.base import VersionConflict, WriteFailed
from memory_manager.storage.postgres import PostgresBackend
from memory_manager.vault.note import parse
from memory_manager.vault.ulid import new_ulid

_CREATED = datetime(2024, 1, 1, tzinfo=UTC)


@pytest_asyncio.fixture
async def pool(test_database_url: str) -> AsyncIterator[asyncpg.Pool]:
    # A plain connection for the migration, not one from the pool below:
    # `migrate` takes an `asyncpg.Connection`, not a pool's connection proxy.
    migration_conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(migration_conn)
    finally:
        await migration_conn.close()

    created_pool = await asyncpg.create_pool(test_database_url)
    try:
        yield created_pool
    finally:
        await created_pool.close()


@pytest_asyncio.fixture
async def indexer(pool: asyncpg.Pool) -> AsyncIterator[Indexer]:
    """An `Indexer` over `VaultNotesSource()` - no provider, drained on teardown."""
    built = Indexer(pool, VaultNotesSource())
    try:
        yield built
    finally:
        await built.aclose()


@pytest.fixture
def backend(pool: asyncpg.Pool, indexer: Indexer) -> PostgresBackend:
    """A `PostgresBackend` wired exactly like `app.py`'s `"postgres"` branch wires one."""
    return PostgresBackend(
        pool, index_hook=indexer.index_on_connection, index_commit_hook=indexer.schedule_embeddings
    )


async def _notes_dump(pool: asyncpg.Pool) -> list[tuple[object, ...]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "select id, path, namespace, type, slug, title, description, tags, aliases, "
            "created, updated, valid_from, valid_to, supersedes, source, archived, file_hash "
            "from notes order by path"
        )
    return [tuple(dict(row).values()) for row in rows]


async def _chunks_dump(pool: asyncpg.Pool) -> list[tuple[object, ...]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "select note_id, ord, heading_path, text, lang from chunks order by note_id, ord"
        )
    return [tuple(dict(row).values()) for row in rows]


async def _links_dump(pool: asyncpg.Pool) -> list[tuple[object, ...]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "select source_id, target_raw, target_path from links order by source_id, target_raw"
        )
    return [tuple(dict(row).values()) for row in rows]


class TestConnectionPathIndexing:
    """Every write indexes itself, in the same transaction, on `backend`."""

    async def test_write_is_found_by_fulltext_search_immediately(
        self, pool: asyncpg.Pool, backend: PostgresBackend
    ) -> None:
        content = note_bytes(
            title="Giraffe Habits", body="Giraffes eat leaves high in acacia trees.\n"
        )
        await backend.write("personal/fact/giraffe.md", content, if_version="new", client="ci")

        hits = await fulltext_search(pool, "giraffe")
        assert {hit.path for hit in hits} == {"personal/fact/giraffe.md"}

    async def test_edit_updates_the_index_old_text_no_longer_matches(
        self, pool: asyncpg.Pool, backend: PostgresBackend
    ) -> None:
        note_id = new_ulid()
        original = note_bytes(id=note_id, title="Edit Target", body="Old phrase zephyrfoo.\n")
        written = await backend.write(
            "personal/fact/edit-me.md", original, if_version="new", client="ci"
        )

        edited = note_bytes(id=note_id, title="Edit Target", body="New phrase quokkabar.\n")
        await backend.write(
            "personal/fact/edit-me.md", edited, if_version=written.version, client="ci"
        )

        new_hits = await fulltext_search(pool, "quokkabar")
        old_hits = await fulltext_search(pool, "zephyrfoo")
        assert {hit.path for hit in new_hits} == {"personal/fact/edit-me.md"}
        assert old_hits == []

    async def test_archive_moves_the_index_row_and_excludes_it_from_search(
        self, pool: asyncpg.Pool, backend: PostgresBackend
    ) -> None:
        note_id = new_ulid()
        content = note_bytes(id=note_id, title="Archive Me", body="Unique archiveword123.\n")
        written = await backend.write(
            "personal/fact/archive-me.md", content, if_version="new", client="ci"
        )

        await backend.archive(
            "personal/fact/archive-me.md", if_version=written.version, client="ci"
        )

        async with pool.acquire() as conn:
            rows = await conn.fetch("select * from notes where id = $1", note_id)
        assert len(rows) == 1
        assert rows[0]["archived"] is True
        assert rows[0]["path"] == "_archive/personal/fact/archive-me.md"

        assert await fulltext_search(pool, "archiveword123") == []
        included = await fulltext_search(pool, "archiveword123", include_archived=True)
        assert {hit.path for hit in included} == {"_archive/personal/fact/archive-me.md"}

    async def test_supersede_indexes_both_the_old_and_the_new_note(
        self, pool: asyncpg.Pool, backend: PostgresBackend
    ) -> None:
        old_id = new_ulid()
        old_content = note_bytes(id=old_id, title="Old Note", body="Deprecated zzzoldword.\n")
        written = await backend.write(
            "personal/fact/old.md", old_content, if_version="new", client="ci"
        )

        new_id = new_ulid()
        new_content = note_bytes(id=new_id, title="New Note", body="Current zzznewword.\n")
        await backend.supersede(
            "personal/fact/old.md",
            "personal/fact/new.md",
            new_content,
            if_version=written.version,
            client="ci",
        )

        async with pool.acquire() as conn:
            old_row = await conn.fetchrow("select * from notes where id = $1", old_id)
            new_row = await conn.fetchrow("select * from notes where id = $1", new_id)
        assert old_row is not None
        assert old_row["path"] == "personal/fact/old.md"
        assert new_row is not None
        assert new_row["path"] == "personal/fact/new.md"

        old_hits = await fulltext_search(pool, "zzzoldword")
        new_hits = await fulltext_search(pool, "zzznewword")
        assert {hit.path for hit in old_hits} == {"personal/fact/old.md"}
        assert {hit.path for hit in new_hits} == {"personal/fact/new.md"}

    async def test_rejected_write_creates_no_index_rows(
        self, pool: asyncpg.Pool, backend: PostgresBackend
    ) -> None:
        await backend.write(
            "personal/fact/a.md", note_bytes(title="First"), if_version="new", client="ci"
        )

        with pytest.raises(VersionConflict):
            await backend.write(
                "personal/fact/a.md", note_bytes(title="Second"), if_version="new", client="ci"
            )

        async with pool.acquire() as conn:
            rows = await conn.fetch("select title from notes where path = $1", "personal/fact/a.md")
        assert [row["title"] for row in rows] == ["First"]

    async def test_index_hook_failure_rolls_back_the_whole_write(self, pool: asyncpg.Pool) -> None:
        """An `index_hook` error is mapped like any other `PostgresError`: `WriteFailed`,
        and the whole write - `vault_notes` included - rolls back with it (ADR-0007 §4).
        """

        async def failing_hook(conn: object, path: str, content: bytes) -> None:
            raise asyncpg.PostgresError("simulated index failure")

        backend = PostgresBackend(pool, index_hook=failing_hook)

        with pytest.raises(WriteFailed):
            await backend.write("personal/fact/a.md", note_bytes(), if_version="new", client="ci")

        async with pool.acquire() as conn:
            vault_count = await conn.fetchval(
                "select count(*) from vault_notes where path = $1", "personal/fact/a.md"
            )
            notes_count = await conn.fetchval(
                "select count(*) from notes where path = $1", "personal/fact/a.md"
            )
        assert vault_count == 0
        assert notes_count == 0

    async def test_write_resolves_an_existing_link_and_heals_a_dangling_one_later(
        self, pool: asyncpg.Pool, backend: PostgresBackend
    ) -> None:
        """A write's link resolution never has to read notes it has no reason to.

        A handful of unrelated filler notes are written first; neither
        resolving `source.md`'s link nor healing it once its target exists
        has anything to do with them - this only proves the *outcome* is
        still correct with other notes around, see `_entries_for`'s and
        `_heal_dangling_for_note`'s docstrings in `index/indexer.py` for how
        that stays true without ever reading the whole `notes` table.
        """
        for i in range(10):
            await backend.write(
                f"personal/fact/filler-{i}.md",
                note_bytes(title=f"Filler {i}"),
                if_version="new",
                client="ci",
            )

        source_content = note_bytes(title="Source Note", body="See [[target-slug]] for details.\n")
        source_id = parse(source_content).id
        await backend.write(
            "personal/fact/source.md", source_content, if_version="new", client="ci"
        )

        async with pool.acquire() as conn:
            target_path = await conn.fetchval(
                "select target_path from links where source_id = $1", source_id
            )
        assert target_path is None  # dangling: "target-slug" does not exist yet

        await backend.write(
            "personal/fact/target-slug.md",
            note_bytes(title="Target"),
            if_version="new",
            client="ci",
        )

        async with pool.acquire() as conn:
            target_path = await conn.fetchval(
                "select target_path from links where source_id = $1", source_id
            )
        assert target_path == "personal/fact/target-slug.md"


@dataclass
class _FakeProvider:
    """A deterministic embedding provider for background-task tests - no network."""

    model: str
    dimension: int = 4
    fail: bool = False
    hang: bool = False
    calls: list[list[str]] = field(default_factory=list)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.hang:
            await asyncio.sleep(3600)
        if self.fail:
            raise EmbeddingError("fake provider configured to fail")
        return [[float(len(text) + i) for i in range(self.dimension)] for text in texts]


class TestBackgroundEmbeddings:
    """`schedule_embeddings`: never awaited by the write, drained with a time limit."""

    async def test_failing_provider_leaves_the_write_successful_with_embedding_null(
        self, pool: asyncpg.Pool
    ) -> None:
        provider = _FakeProvider(model="fake-v1", fail=True)
        background_indexer = Indexer(pool, VaultNotesSource(), provider)
        backend = PostgresBackend(
            pool,
            index_hook=background_indexer.index_on_connection,
            index_commit_hook=background_indexer.schedule_embeddings,
        )

        note_id = new_ulid()
        content = note_bytes(id=note_id, title="Embed Fail", body="Some body text.\n")
        result = await backend.write(
            "personal/fact/embed-fail.md", content, if_version="new", client="ci"
        )
        assert result.version

        await background_indexer.aclose()

        async with pool.acquire() as conn:
            embedding = await conn.fetchval(
                "select embedding from chunks where note_id = $1", note_id
            )
        assert embedding is None

    async def test_hanging_provider_is_cancelled_by_aclose_timeout(
        self, pool: asyncpg.Pool
    ) -> None:
        provider = _FakeProvider(model="fake-v1", hang=True)
        background_indexer = Indexer(pool, VaultNotesSource(), provider)
        backend = PostgresBackend(
            pool,
            index_hook=background_indexer.index_on_connection,
            index_commit_hook=background_indexer.schedule_embeddings,
        )

        note_id = new_ulid()
        content = note_bytes(id=note_id, title="Embed Hang", body="Some body text.\n")
        result = await backend.write(
            "personal/fact/embed-hang.md", content, if_version="new", client="ci"
        )
        assert result.version  # the write itself never waits on the provider

        await background_indexer.aclose(timeout=0.05)

        async with pool.acquire() as conn:
            embedding = await conn.fetchval(
                "select embedding from chunks where note_id = $1", note_id
            )
        assert embedding is None


class TestReindexFull:
    async def test_reindex_full_from_vault_notes_matches_the_connection_path_result(
        self, pool: asyncpg.Pool, backend: PostgresBackend
    ) -> None:
        await backend.write(
            "personal/fact/a.md",
            note_bytes(title="A", body="See [[b]].\n"),
            if_version="new",
            client="ci",
        )
        await backend.write(
            "personal/fact/b.md",
            note_bytes(title="B", body="No links here.\n"),
            if_version="new",
            client="ci",
        )

        connection_path_notes = await _notes_dump(pool)
        connection_path_chunks = await _chunks_dump(pool)
        connection_path_links = await _links_dump(pool)

        async with pool.acquire() as conn:
            await conn.execute("truncate table notes cascade")

        rebuilder = Indexer(pool, VaultNotesSource())
        await rebuilder.reindex_full()

        assert await _notes_dump(pool) == connection_path_notes
        assert await _chunks_dump(pool) == connection_path_chunks
        assert await _links_dump(pool) == connection_path_links


@pytest.fixture
def cli_database_url(admin_database_url: str) -> Iterator[str]:
    """A fresh database created/dropped with its own event loop.

    `cli.main` runs its own `asyncio.run`, so this fixture must not depend
    on the async `test_database_url` fixture - nesting event loops fails.
    """
    db_name = f"mm_test_{secrets.token_hex(8)}"

    async def _create() -> None:
        connection = await asyncpg.connect(admin_database_url)
        try:
            await connection.execute(f'create database "{db_name}"')
        finally:
            await connection.close()

    asyncio.run(_create())
    base, _, _ = admin_database_url.rpartition("/")
    url = f"{base}/{db_name}"
    try:
        yield url
    finally:

        async def _drop() -> None:
            connection = await asyncpg.connect(admin_database_url)
            try:
                await connection.execute(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    "where datname = $1 and pid <> pg_backend_pid()",
                    db_name,
                )
                await connection.execute(f'drop database if exists "{db_name}"')
            finally:
                await connection.close()

        asyncio.run(_drop())


def _seed_vault_note(database_url: str, content: bytes) -> None:
    """Migrate `database_url`, then write `content` into `vault_notes` directly,
    through a plain `PostgresBackend(pool)` with no index wired in - this fixture
    seeds the source of truth `cli.main`'s own `reindex --full` call then reads from,
    not the index under test.
    """

    async def _seed() -> None:
        migration_conn = await asyncpg.connect(database_url)
        try:
            await migrate(migration_conn)
        finally:
            await migration_conn.close()

        pool = await asyncpg.create_pool(database_url)
        try:
            await PostgresBackend(pool).write(
                "personal/fact/seeded.md", content, if_version="new", client="ci"
            )
        finally:
            await pool.close()

    asyncio.run(_seed())


class TestCli:
    def test_reindex_full_runs_without_vault_dir_on_the_postgres_backend(
        self, cli_database_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_vault_note(cli_database_url, note_bytes(title="Seeded"))
        monkeypatch.setenv("STORAGE_BACKEND", "postgres")
        monkeypatch.setenv("DATABASE_URL", cli_database_url)
        monkeypatch.delenv("VAULT_DIR", raising=False)

        exit_code = cli.main(["reindex", "--full"])

        assert exit_code == 0

        async def _check() -> int:
            connection = await asyncpg.connect(cli_database_url)
            try:
                count = await connection.fetchval("select count(*) from notes")
                assert isinstance(count, int)
                return count
            finally:
                await connection.close()

        assert asyncio.run(_check()) == 1
