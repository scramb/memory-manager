# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `fulltext_search` (#28)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from memory_manager.db.migrate import migrate
from memory_manager.index.indexer import Indexer
from memory_manager.search import ChunkHit, fulltext_search
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import new_ulid

_CREATED = datetime(2024, 1, 1, tzinfo=UTC)


def _note(
    *,
    title: str = "A note",
    description: str = "A description",
    body: str = "Some body text.",
) -> Note:
    return Note(
        id=new_ulid(),
        title=title,
        description=description,
        type="fact",
        created=_CREATED,
        updated=_CREATED,
        body=body,
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


@pytest_asyncio.fixture
async def seeded(indexer: Indexer, vault_dir: Path, pool: asyncpg.Pool) -> asyncpg.Pool:
    """Index a small set of fictional notes covering both languages.

    Goes through the real `Indexer` pipeline, as required for this task
    (#28), not hand-crafted rows.
    """
    notes = {
        "personal/fact/houses.md": _note(
            title="Houses in Hamburg",
            body=(
                "Die Häuser in Hamburg sind alt und die Straßen sind schmal. "
                "Das ist eine Stadt mit vielen Kanälen."
            ),
        ),
        "personal/fact/running.md": _note(
            title="Running shoes",
            body=(
                "Running shoes are great for a morning run. "
                "The shoes fit well and the laces are long."
            ),
        ),
        "personal/fact/stack.md": _note(
            title="Search stack",
            body="pgvector powers memory search. bge-m3 generates embeddings.",
        ),
        "personal/fact/queue.md": _note(
            title="Write queue",
            body="The write queue serializes concurrent writes to the vault.",
        ),
        "personal/fact/two-words.md": _note(
            title="Two words",
            body="A write happened. Later, someone joined the queue.",
        ),
        "personal/fact/shoes-winter.md": _note(
            title="Winter shoes",
            body="Winter shoes keep your feet warm in the snow.",
        ),
        "personal/fact/coffee.md": _note(
            title="Coffee break",
            body="A short coffee break in the afternoon.",
        ),
        "personal/fact/tea.md": _note(
            title="Tea time",
            body="A short tea time in the afternoon.",
        ),
        "personal/fact/memory-prominent.md": _note(
            title="Memory memory memory",
            body="Memory, memory and more memory - this note is all about memory.",
        ),
        "personal/fact/memory-passing.md": _note(
            title="Unrelated topic",
            body="This note briefly mentions memory once, among other things.",
        ),
    }
    for rel, note in notes.items():
        _write(vault_dir, rel, note)
    await indexer.index_paths(list(notes))
    return pool


async def _archive_note(pool: asyncpg.Pool, vault_dir: Path, indexer: Indexer) -> str:
    """Move `houses.md` to `_archive/` through the indexer, return its id."""
    async with pool.acquire() as conn:
        note_id = await conn.fetchval("select id from notes where path = 'personal/fact/houses.md'")
    source = vault_dir / "personal/fact/houses.md"
    target = vault_dir / "_archive/personal/fact/houses.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    source.unlink()
    await indexer.index_paths(["_archive/personal/fact/houses.md", "personal/fact/houses.md"])
    assert isinstance(note_id, str)
    return note_id


class TestMatching:
    async def test_german_inflection_matches_via_stemming(self, seeded: asyncpg.Pool) -> None:
        hits = await fulltext_search(seeded, "Haus")
        paths = {hit.path for hit in hits}
        assert "personal/fact/houses.md" in paths

    async def test_english_stemming_matches(self, seeded: asyncpg.Pool) -> None:
        hits = await fulltext_search(seeded, "run")
        paths = {hit.path for hit in hits}
        assert "personal/fact/running.md" in paths

    async def test_simple_config_matches_exact_proper_names(self, seeded: asyncpg.Pool) -> None:
        hits = await fulltext_search(seeded, "pgvector")
        paths = {hit.path for hit in hits}
        assert "personal/fact/stack.md" in paths

        hits = await fulltext_search(seeded, "bge-m3")
        paths = {hit.path for hit in hits}
        assert "personal/fact/stack.md" in paths

    async def test_websearch_exact_phrase(self, seeded: asyncpg.Pool) -> None:
        hits = await fulltext_search(seeded, '"write queue"')
        paths = {hit.path for hit in hits}
        assert "personal/fact/queue.md" in paths
        assert "personal/fact/two-words.md" not in paths

    async def test_websearch_exclusion(self, seeded: asyncpg.Pool) -> None:
        hits = await fulltext_search(seeded, "shoes -winter")
        paths = {hit.path for hit in hits}
        assert "personal/fact/running.md" in paths
        assert "personal/fact/shoes-winter.md" not in paths

    async def test_websearch_or(self, seeded: asyncpg.Pool) -> None:
        hits = await fulltext_search(seeded, "coffee or tea")
        paths = {hit.path for hit in hits}
        assert "personal/fact/coffee.md" in paths
        assert "personal/fact/tea.md" in paths


class TestRanking:
    async def test_best_chunk_ranks_first(self, seeded: asyncpg.Pool) -> None:
        hits = await fulltext_search(seeded, "memory")
        assert len(hits) >= 2
        assert hits[0].path == "personal/fact/memory-prominent.md"
        assert hits[0].score > hits[-1].score

    async def test_results_are_chunk_hits_with_expected_fields(self, seeded: asyncpg.Pool) -> None:
        hits = await fulltext_search(seeded, "pgvector")
        assert len(hits) == 1
        hit = hits[0]
        assert isinstance(hit, ChunkHit)
        assert hit.path == "personal/fact/stack.md"
        assert hit.ord == 0
        assert hit.heading_path == ""
        assert "pgvector" in hit.text
        assert hit.score > 0


class TestArchiving:
    async def test_archived_notes_excluded_by_default(
        self, seeded: asyncpg.Pool, vault_dir: Path, indexer: Indexer
    ) -> None:
        await _archive_note(seeded, vault_dir, indexer)

        hits = await fulltext_search(seeded, "Haus")
        assert "personal/fact/houses.md" not in {hit.path for hit in hits}

    async def test_archived_notes_included_on_request(
        self, seeded: asyncpg.Pool, vault_dir: Path, indexer: Indexer
    ) -> None:
        await _archive_note(seeded, vault_dir, indexer)

        hits = await fulltext_search(seeded, "Haus", include_archived=True)
        assert "_archive/personal/fact/houses.md" in {hit.path for hit in hits}


class TestSimpleConfigFallback:
    """A stopword-only query parses to an empty tsquery in its language
    config (verified against Postgres 16: `websearch_to_tsquery('german',
    'die')` is empty). `tsv_simple` has no stopword list, so matching must
    fall back to it - but only when the language-specific query is empty;
    a real content word still has to go through stemming.
    """

    async def test_single_german_stopword_falls_back_to_simple(self, seeded: asyncpg.Pool) -> None:
        hits = await fulltext_search(seeded, "die")
        assert "personal/fact/houses.md" in {hit.path for hit in hits}

    async def test_english_stopwords_fall_back_to_simple(self, seeded: asyncpg.Pool) -> None:
        hits = await fulltext_search(seeded, "the and")
        assert "personal/fact/running.md" in {hit.path for hit in hits}

    async def test_german_content_word_still_uses_stemming_not_fallback(
        self, seeded: asyncpg.Pool
    ) -> None:
        # The vault only ever spells it "Häuser"; `tsv_simple` keeps that
        # literal token and would not match "Haus" on its own (verified
        # against Postgres 16). A match here can only come from the german
        # branch's stemming, proving the fallback does not mask it.
        hits = await fulltext_search(seeded, "Haus")
        assert "personal/fact/houses.md" in {hit.path for hit in hits}


class TestEdgeCases:
    async def test_empty_query_returns_no_hits(self, seeded: asyncpg.Pool) -> None:
        assert await fulltext_search(seeded, "") == []

    async def test_whitespace_only_query_returns_no_hits(self, seeded: asyncpg.Pool) -> None:
        assert await fulltext_search(seeded, "   ") == []

    async def test_stopword_only_query_does_not_crash(self, seeded: asyncpg.Pool) -> None:
        hits = await fulltext_search(seeded, "der die das")
        assert isinstance(hits, list)

    async def test_sql_special_characters_are_harmless(self, seeded: asyncpg.Pool) -> None:
        hits = await fulltext_search(seeded, "O'Brien; DROP TABLE notes; \\x00")
        assert isinstance(hits, list)

        async with seeded.acquire() as conn:
            count = await conn.fetchval("select count(*) from notes")
        assert count == 10

    async def test_limit_is_respected(self, seeded: asyncpg.Pool) -> None:
        hits = await fulltext_search(seeded, "a or the or memory or write", limit=2)
        assert len(hits) <= 2

    async def test_no_match_returns_empty_list(self, seeded: asyncpg.Pool) -> None:
        hits = await fulltext_search(seeded, "zzznonexistentzzz")
        assert hits == []
