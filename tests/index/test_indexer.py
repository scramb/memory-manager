# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `Indexer` (#26)."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from memory_manager import cli
from memory_manager.db.migrate import migrate
from memory_manager.index.indexer import Indexer, IndexStats
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import new_ulid

_CREATED = datetime(2024, 1, 1, tzinfo=UTC)


def _note(
    *,
    note_id: str | None = None,
    title: str = "A note",
    description: str = "A description",
    note_type: str = "fact",
    body: str = "Some body text.",
    tags: tuple[str, ...] = (),
    aliases: tuple[str, ...] = (),
) -> Note:
    return Note(
        id=note_id or new_ulid(),
        title=title,
        description=description,
        type=note_type,
        created=_CREATED,
        updated=_CREATED,
        body=body,
        tags=tags,
        aliases=aliases,
    )


def _write(vault_dir: Path, rel: str, note: Note) -> None:
    path = vault_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(serialize(note))


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "vault"
    directory.mkdir()
    return directory


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


@pytest.fixture
def indexer(pool: asyncpg.Pool, vault_dir: Path) -> Indexer:
    return Indexer(pool, vault_dir)


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


class TestIndexPaths:
    async def test_indexes_a_new_note(
        self, indexer: Indexer, vault_dir: Path, pool: asyncpg.Pool
    ) -> None:
        note = _note(body="# Heading\n\nSome text with a [[other]] link.")
        _write(vault_dir, "personal/fact/a.md", note)

        stats = await indexer.index_paths(["personal/fact/a.md"])

        assert stats == IndexStats(indexed=1, unchanged=0, deleted=0, failed=0)
        async with pool.acquire() as conn:
            row = await conn.fetchrow("select * from notes where id = $1", note.id)
            assert row is not None
            assert row["path"] == "personal/fact/a.md"
            assert row["namespace"] == "personal"
            assert row["type"] == "fact"
            assert row["slug"] == "a"
            assert row["archived"] is False

            chunks = await conn.fetch("select * from chunks where note_id = $1", note.id)
            assert len(chunks) == 1

            links = await conn.fetch("select * from links where source_id = $1", note.id)
            assert len(links) == 1
            assert links[0]["target_raw"] == "other"
            assert links[0]["target_path"] is None  # dangling: "other" does not exist

    async def test_reindexing_unchanged_file_is_a_no_op(
        self, indexer: Indexer, vault_dir: Path, pool: asyncpg.Pool
    ) -> None:
        note = _note()
        _write(vault_dir, "personal/fact/a.md", note)
        await indexer.index_paths(["personal/fact/a.md"])
        async with pool.acquire() as conn:
            before = await conn.fetchval("select indexed_at from notes where id = $1", note.id)

        stats = await indexer.index_paths(["personal/fact/a.md"])

        assert stats == IndexStats(indexed=0, unchanged=1, deleted=0, failed=0)
        async with pool.acquire() as conn:
            after = await conn.fetchval("select indexed_at from notes where id = $1", note.id)
        assert after == before

    async def test_modifying_one_note_only_reindexes_that_one(
        self, indexer: Indexer, vault_dir: Path, pool: asyncpg.Pool
    ) -> None:
        note_a = _note(title="Original title")
        note_b = _note()
        _write(vault_dir, "personal/fact/a.md", note_a)
        _write(vault_dir, "personal/fact/b.md", note_b)
        await indexer.index_paths(["personal/fact/a.md", "personal/fact/b.md"])

        _write(vault_dir, "personal/fact/a.md", Note(**{**note_a.__dict__, "title": "New title"}))
        stats = await indexer.index_paths(["personal/fact/a.md", "personal/fact/b.md"])

        assert stats == IndexStats(indexed=1, unchanged=1, deleted=0, failed=0)
        async with pool.acquire() as conn:
            title_a = await conn.fetchval("select title from notes where id = $1", note_a.id)
            title_b = await conn.fetchval("select title from notes where id = $1", note_b.id)
        assert title_a == "New title"
        assert title_b == note_b.title

    async def test_deleting_a_file_removes_the_note_and_cascades(
        self, indexer: Indexer, vault_dir: Path, pool: asyncpg.Pool
    ) -> None:
        note = _note(body="Some text.")
        _write(vault_dir, "personal/fact/a.md", note)
        await indexer.index_paths(["personal/fact/a.md"])

        (vault_dir / "personal/fact/a.md").unlink()
        stats = await indexer.index_paths(["personal/fact/a.md"])

        assert stats == IndexStats(indexed=0, unchanged=0, deleted=1, failed=0)
        async with pool.acquire() as conn:
            assert await conn.fetchval("select count(*) from notes where id = $1", note.id) == 0
            assert (
                await conn.fetchval("select count(*) from chunks where note_id = $1", note.id) == 0
            )

    async def test_moving_a_note_to_archive_keeps_a_single_row(
        self, indexer: Indexer, vault_dir: Path, pool: asyncpg.Pool
    ) -> None:
        note = _note()
        _write(vault_dir, "personal/fact/a.md", note)
        await indexer.index_paths(["personal/fact/a.md"])

        (vault_dir / "personal/fact/a.md").unlink()
        _write(vault_dir, "_archive/personal/fact/a.md", note)
        stats = await indexer.index_paths(["_archive/personal/fact/a.md", "personal/fact/a.md"])

        assert stats.indexed == 1
        async with pool.acquire() as conn:
            rows = await conn.fetch("select * from notes where id = $1", note.id)
        assert len(rows) == 1
        assert rows[0]["path"] == "_archive/personal/fact/a.md"
        assert rows[0]["archived"] is True

    async def test_invalid_note_is_counted_failed_without_blocking_others(
        self, indexer: Indexer, vault_dir: Path, pool: asyncpg.Pool
    ) -> None:
        good = _note()
        _write(vault_dir, "personal/fact/good.md", good)
        bad_path = vault_dir / "personal/fact/bad.md"
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.write_bytes(b"not a note at all\n")

        stats = await indexer.index_paths(["personal/fact/good.md", "personal/fact/bad.md"])

        assert stats == IndexStats(indexed=1, unchanged=0, deleted=0, failed=1)
        async with pool.acquire() as conn:
            assert await conn.fetchval("select count(*) from notes where id = $1", good.id) == 1

    async def test_link_to_note_indexed_later_resolves_after_incremental_heal(
        self, indexer: Indexer, vault_dir: Path, pool: asyncpg.Pool
    ) -> None:
        note_a = _note(body="See [[b]] for more.")
        note_b = _note(body="Target note.")
        _write(vault_dir, "personal/fact/a.md", note_a)
        _write(vault_dir, "personal/fact/b.md", note_b)

        await indexer.index_paths(["personal/fact/a.md"])
        async with pool.acquire() as conn:
            target_path = await conn.fetchval(
                "select target_path from links where source_id = $1", note_a.id
            )
        assert target_path is None  # b does not exist yet

        await indexer.index_paths(["personal/fact/b.md"])
        async with pool.acquire() as conn:
            target_path = await conn.fetchval(
                "select target_path from links where source_id = $1", note_a.id
            )
        assert target_path == "personal/fact/b.md"

    async def test_links_differing_only_by_case_collapse_to_one_row(
        self, indexer: Indexer, vault_dir: Path, pool: asyncpg.Pool
    ) -> None:
        note = _note(body="See [[Other]] and [[other]].")
        _write(vault_dir, "personal/fact/a.md", note)

        await indexer.index_paths(["personal/fact/a.md"])

        async with pool.acquire() as conn:
            links = await conn.fetch("select * from links where source_id = $1", note.id)
        assert len(links) == 1
        assert links[0]["target_raw"] == "Other"  # first occurrence kept


class TestReindexFull:
    async def test_full_rebuild_matches_incremental_result(
        self, indexer: Indexer, vault_dir: Path, pool: asyncpg.Pool
    ) -> None:
        note_a = _note(body="See [[b]].")
        note_b = _note(body="No links here.")
        _write(vault_dir, "personal/fact/a.md", note_a)
        _write(vault_dir, "personal/fact/b.md", note_b)

        await indexer.index_paths(["personal/fact/a.md"])
        await indexer.index_paths(["personal/fact/b.md"])

        incremental_notes = await _notes_dump(pool)
        incremental_chunks = await _chunks_dump(pool)
        incremental_links = await _links_dump(pool)

        async with pool.acquire() as conn:
            await conn.execute("truncate table notes cascade")

        await indexer.reindex_full()

        assert await _notes_dump(pool) == incremental_notes
        assert await _chunks_dump(pool) == incremental_chunks
        assert await _links_dump(pool) == incremental_links

    async def test_full_rebuild_removes_rows_for_deleted_files(
        self, indexer: Indexer, vault_dir: Path, pool: asyncpg.Pool
    ) -> None:
        note_a = _note()
        note_b = _note()
        _write(vault_dir, "personal/fact/a.md", note_a)
        _write(vault_dir, "personal/fact/b.md", note_b)
        await indexer.reindex_full()

        (vault_dir / "personal/fact/b.md").unlink()
        stats = await indexer.reindex_full()

        assert stats.deleted == 1
        async with pool.acquire() as conn:
            assert await conn.fetchval("select count(*) from notes") == 1
            assert await conn.fetchval("select count(*) from notes where id = $1", note_a.id) == 1

    async def test_full_rebuild_resolves_forward_references(
        self, indexer: Indexer, vault_dir: Path, pool: asyncpg.Pool
    ) -> None:
        note_a = _note(body="See [[b]].")
        note_b = _note(body="Target.")
        _write(vault_dir, "personal/fact/a.md", note_a)
        _write(vault_dir, "personal/fact/b.md", note_b)

        await indexer.reindex_full()

        async with pool.acquire() as conn:
            target_path = await conn.fetchval(
                "select target_path from links where source_id = $1", note_a.id
            )
        assert target_path == "personal/fact/b.md"


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


class TestCli:
    def test_reindex_full_runs_end_to_end(
        self,
        vault_dir: Path,
        cli_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        note = _note()
        _write(vault_dir, "personal/fact/a.md", note)
        monkeypatch.setenv("DATABASE_URL", cli_database_url)
        monkeypatch.setenv("VAULT_DIR", str(vault_dir))

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

    def test_help_does_not_crash(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            cli.main(["--help"])
        assert exc_info.value.code == 0

    def test_without_subcommand_returns_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        exit_code = cli.main([])
        assert exit_code == 1
