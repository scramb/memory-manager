# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `hybrid_search` and its building blocks (#29)."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from memory_manager.db.migrate import migrate
from memory_manager.index.embeddings import EmbeddingError
from memory_manager.index.indexer import Indexer
from memory_manager.search import SearchFilters, fulltext_search, hybrid_search, rrf_fuse
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import new_ulid

_CREATED = datetime(2024, 1, 1, tzinfo=UTC)

# The fake provider hashes words into one of this many buckets; large
# enough relative to the handful of distinct words across the fixture
# notes below that an unrelated word colliding with a bucket that matters
# for a test (e.g. "vehicle") is very unlikely, while staying a plain,
# deterministic, dependency-free stand-in for a real embedding model.
_FAKE_DIMENSION = 256

# Maps a query word to the same token a semantically related word in a
# note's body is also mapped to, so a fake embedding can simulate semantic
# overlap a full-text match would never find on its own (differing exact
# words/stems).
_SYNONYMS = {
    "car": "vehicle",
    "cars": "vehicle",
    "automobile": "vehicle",
    "automobiles": "vehicle",
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def _bow_vector(text: str, dimension: int) -> list[float]:
    """A deterministic, hash-based bag-of-words vector for `text`.

    Not a real embedding - just good enough that cosine similarity tracks
    shared (post-synonym-mapping) vocabulary between two texts, which is
    all `vector_search`'s ranking needs from it in these tests.
    """
    vector = [0.0] * dimension
    for word in _WORD_RE.findall(text.lower()):
        token = _SYNONYMS.get(word, word)
        digest = hashlib.sha1(token.encode("utf-8"), usedforsecurity=False).hexdigest()
        bucket = int(digest, 16) % dimension
        vector[bucket] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm > 0:
        vector = [value / norm for value in vector]
    return vector


@dataclass
class FakeProvider:
    """A deterministic `EmbeddingProvider` stand-in; see `_bow_vector`."""

    model: str = "fake-bow"
    dimension: int = _FAKE_DIMENSION
    raise_error: bool = False
    calls: list[tuple[str, ...]] = field(default_factory=list)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(tuple(texts))
        if self.raise_error:
            raise EmbeddingError("fake provider configured to fail")
        return [_bow_vector(text, self.dimension) for text in texts]


def _note(
    *,
    title: str,
    body: str,
    type: str = "fact",
    description: str = "A description",
    tags: tuple[str, ...] = (),
    valid_from: date | None = None,
    valid_to: date | None = None,
) -> Note:
    return Note(
        id=new_ulid(),
        title=title,
        description=description,
        type=type,
        created=_CREATED,
        updated=_CREATED,
        body=body,
        tags=tags,
        valid_from=valid_from,
        valid_to=valid_to,
    )


def _write(vault_dir: Path, rel: str, note: Note) -> None:
    path = vault_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(serialize(note))


# Fictional notes exercising every angle `hybrid_search` has to handle:
# vector-only matches, agreement between both sides, note-level dedup
# across several matching chunks, and every `SearchFilters` field.
_NOTES: dict[str, Note] = {
    "work/fact/automobile-industry.md": _note(
        title="Automobile Industry",
        body=(
            "The automobile industry builds many automobiles every year. "
            "Automobiles need steel, rubber and glass to manufacture."
        ),
    ),
    "work/fact/library-strong.md": _note(
        title="City Library",
        body=(
            "The library is open every day. The library has thousands of books. "
            "People visit the library to read and study at the library."
        ),
    ),
    "work/fact/library-decoy.md": _note(
        title="Neighborhood Newsletter",
        body=(
            "This newsletter covers gardening, weather, recipes, local sports, "
            "traffic updates, volunteer schedules and a brief note about the library."
        ),
    ),
    "work/fact/database-guide.md": _note(
        title="Database Guide",
        body=(
            "# Setup\n\n"
            "The database setup requires careful planning. "
            "Database configuration matters a lot.\n\n"
            "# Maintenance\n\n"
            "Database maintenance includes regular backups. The database must stay healthy.\n\n"
            "# Scaling\n\n"
            "Scaling a database horizontally needs sharding. Database scaling is hard work."
        ),
    ),
    "work/fact/cobaltite-alpha-beta.md": _note(
        title="Cobaltite Alpha Beta",
        tags=("alpha", "beta", "gamma"),
        body="Cobaltite appears in this fact record for the work namespace.",
    ),
    "personal/reference/cobaltite-personal.md": _note(
        title="Cobaltite Personal Reference",
        type="reference",
        tags=("alpha",),
        body="Cobaltite also appears in this personal reference note.",
    ),
    "work/fact/cobaltite-expired.md": _note(
        title="Cobaltite Expired Project",
        body="Cobaltite was mentioned while this note was still valid.",
        valid_from=date(2000, 1, 1),
        valid_to=date(2001, 1, 1),
    ),
    "work/fact/cobaltite-archived.md": _note(
        title="Cobaltite Archived",
        body="Cobaltite is mentioned in a note that will be archived.",
    ),
}


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
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def indexer(pool: asyncpg.Pool, vault_dir: Path, provider: FakeProvider) -> Indexer:
    return Indexer(pool, vault_dir, provider=provider)


@pytest_asyncio.fixture
async def seeded(indexer: Indexer, vault_dir: Path, pool: asyncpg.Pool) -> asyncpg.Pool:
    """Index `_NOTES` through the real `Indexer`, embeddings included."""
    for rel, note in _NOTES.items():
        _write(vault_dir, rel, note)
    await indexer.index_paths(list(_NOTES))
    return pool


async def _archive(vault_dir: Path, indexer: Indexer, rel: str) -> None:
    source = vault_dir / rel
    target = vault_dir / "_archive" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    source.unlink()
    await indexer.index_paths([f"_archive/{rel}", rel])


class TestVectorOnlyMatch:
    async def test_synonym_match_surfaces_only_through_vector_side(
        self, seeded: asyncpg.Pool, provider: FakeProvider
    ) -> None:
        fulltext_only = await fulltext_search(seeded, "car")
        assert fulltext_only == []

        hits = await hybrid_search(seeded, "car", provider=provider)
        assert "work/fact/automobile-industry.md" in {hit.path for hit in hits}


class TestBothSidesAgree:
    async def test_note_both_sides_rank_highly_wins_over_a_weak_decoy(
        self, seeded: asyncpg.Pool, provider: FakeProvider
    ) -> None:
        hits = await hybrid_search(seeded, "library", provider=provider)
        paths = [hit.path for hit in hits]
        assert paths[0] == "work/fact/library-strong.md"
        assert "work/fact/library-decoy.md" in paths
        assert paths.index("work/fact/library-strong.md") < paths.index(
            "work/fact/library-decoy.md"
        )


class TestNoteLevelDedup:
    async def test_note_with_three_matching_chunks_appears_once(self, seeded: asyncpg.Pool) -> None:
        hits = await hybrid_search(seeded, "database")
        assert len(hits) == 1
        assert hits[0].path == "work/fact/database-guide.md"
        assert hits[0].matched_chunks == 3


class TestFallbackWithoutEmbeddings:
    async def test_without_provider_matches_fulltext_note_order(self, seeded: asyncpg.Pool) -> None:
        hits = await hybrid_search(seeded, "database")

        fulltext_hits = await fulltext_search(seeded, "database", limit=50)
        expected_order: list[str] = []
        for hit in fulltext_hits:
            if hit.note_id not in expected_order:
                expected_order.append(hit.note_id)

        assert [hit.note_id for hit in hits] == expected_order

    async def test_failed_query_embedding_falls_back_to_fulltext_only(
        self, seeded: asyncpg.Pool
    ) -> None:
        raising_provider = FakeProvider(raise_error=True)

        hits = await hybrid_search(seeded, "database", provider=raising_provider)

        assert len(hits) == 1
        assert hits[0].path == "work/fact/database-guide.md"


class TestFilters:
    async def test_type_filter(self, seeded: asyncpg.Pool) -> None:
        hits = await hybrid_search(seeded, "cobaltite", filters=SearchFilters(types=("fact",)))
        paths = {hit.path for hit in hits}
        assert "work/fact/cobaltite-alpha-beta.md" in paths
        assert "personal/reference/cobaltite-personal.md" not in paths

    async def test_tags_filter_is_all_of(self, seeded: asyncpg.Pool) -> None:
        hits = await hybrid_search(
            seeded, "cobaltite", filters=SearchFilters(tags=("alpha", "beta"))
        )
        paths = {hit.path for hit in hits}
        assert paths == {"work/fact/cobaltite-alpha-beta.md"}

    async def test_namespace_filter(self, seeded: asyncpg.Pool) -> None:
        hits = await hybrid_search(
            seeded, "cobaltite", filters=SearchFilters(namespaces=("personal",))
        )
        paths = {hit.path for hit in hits}
        assert paths == {"personal/reference/cobaltite-personal.md"}

    async def test_valid_at_excludes_expired_notes(self, seeded: asyncpg.Pool) -> None:
        hits = await hybrid_search(
            seeded, "cobaltite", filters=SearchFilters(valid_at=date(2020, 1, 1))
        )
        paths = {hit.path for hit in hits}
        assert "work/fact/cobaltite-expired.md" not in paths
        assert "work/fact/cobaltite-alpha-beta.md" in paths

    async def test_archived_excluded_by_default_included_on_request(
        self, seeded: asyncpg.Pool, vault_dir: Path, indexer: Indexer
    ) -> None:
        await _archive(vault_dir, indexer, "work/fact/cobaltite-archived.md")

        hits = await hybrid_search(seeded, "cobaltite")
        assert "work/fact/cobaltite-archived.md" not in {hit.path for hit in hits}
        assert "_archive/work/fact/cobaltite-archived.md" not in {hit.path for hit in hits}

        hits = await hybrid_search(
            seeded, "cobaltite", filters=SearchFilters(include_archived=True)
        )
        assert "_archive/work/fact/cobaltite-archived.md" in {hit.path for hit in hits}


class TestSnippet:
    async def test_fulltext_snippet_highlights_the_query_term(self, seeded: asyncpg.Pool) -> None:
        hits = await hybrid_search(seeded, "library")
        strong = next(hit for hit in hits if hit.path == "work/fact/library-strong.md")
        assert "**" in strong.snippet
        assert "library" in strong.snippet.lower()


class TestRrfFuse:
    def test_matches_hand_computed_scores(self) -> None:
        scores = rrf_fuse([["a", "b", "c"], ["b", "c", "a"]], k=1)
        assert scores["a"] == pytest.approx(1 / 2 + 1 / 4)
        assert scores["b"] == pytest.approx(1 / 3 + 1 / 2)
        assert scores["c"] == pytest.approx(1 / 4 + 1 / 3)
        assert scores["b"] > scores["a"] > scores["c"]

    def test_item_missing_from_one_list_only_scores_from_the_other(self) -> None:
        scores = rrf_fuse([["x"], ["y"]], k=10)
        assert scores == {"x": pytest.approx(1 / 11), "y": pytest.approx(1 / 11)}

    def test_empty_rank_lists_yield_no_scores(self) -> None:
        assert rrf_fuse([], k=60) == {}
        assert rrf_fuse([[], []], k=60) == {}
