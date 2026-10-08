# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `search.py`'s ADR-0016 per-kind vector search (#221).

Seeds `chunks`/`notes` directly (same raw-insert style `test_fulltext_bounded.py`
uses for `chunks`) against a `backend="postgres"`-migrated database - connecting
as the migration owner, which `chunks_owner_access` (`migrations/postgres/
0012_vector_layout.sql`) exempts from RLS entirely, the same "system identity"
path `Indexer`'s own batch writes use. This sidesteps the `namespaces` registry/
`mm_namespace_kind` resolution `tests/index/test_vector_schema.py` exercises -
not this file's concern - and lets a test set `namespace_kind` directly.

**Why no EXPLAIN-based planner-cost assertion.** ADR-0016/`docs/research/
vector-index.md` found the planner only prefers an HNSW index over a
sequential/B-tree scan at row counts and selectivity far past what a fast test
suite can seed (the spike itself needed 1,000,000 rows and a dedicated,
non-shared container to observe it, and even there needed `enable_seqscan`/
`enable_bitmapscan = off` to see a *forced* HNSW plan - this file's own probing
during development found that even that forcing still prefers a small table's
other indexes, e.g. the `(note_id, ord, namespace_kind)` unique constraint, over
HNSW at the row counts this suite can afford). A cost-based assertion here would
either flake across Postgres versions/hardware or need spike-scale data,
catching nothing reliably (`CLAUDE.md`: "no tests that only mirror structure").
`TestPartitionRouting` instead checks what *is* scale-independent - which
partition's relation a kind's query resolves to (`EXPLAIN`'s own `Relation
Name`, unaffected by cost heuristics) and whether an HNSW index exists on it at
all; `TestHnswSettingsReachTheSession` checks the exact `hnsw.*` values
`_HNSW_KIND_SETTINGS` sets actually reach the session (`current_setting`,
inside the same transaction - the real thing the planner would use if it did
pick that index, verified directly rather than inferred from a plan it might
not choose at this scale). `TestPersonalRanksFirstUnderOrgNoise` and
`TestGroupFilteredRecallMatchesExactScan` below are the recall/correctness
tests the issue's own checklist asks for - end-to-end, independent of which
access path Postgres happens to choose.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

import asyncpg
import asyncpg.pool
import pytest_asyncio

from memory_manager.db.migrate import migrate
from memory_manager.search import (
    _HNSW_KIND_SETTINGS,
    _VECTOR_KINDS,
    _VECTOR_SQL_KIND_TEMPLATE,
    _fetch_vector_kind_rows,
    _vector_search_legs,
    hybrid_search,
    vector_search,
)

__all__: list[str] = []

_DIMENSION = 1024
_MODEL = "test-model"

#: Seeding helpers take either a bare connection or one acquired from a pool
#: (`TestPersonalRanksFirstUnderOrgNoise` seeds through `postgres_pool.acquire()`).
_Conn = asyncpg.Connection | asyncpg.pool.PoolConnectionProxy


@pytest_asyncio.fixture
async def postgres_conn(test_database_url: str) -> AsyncIterator[asyncpg.Connection]:
    """A connection to a `backend="postgres"`-migrated database (ADR-0016 layout),
    as the migration owner - RLS-exempt (`chunks_owner_access`), so a test can
    seed `chunks` directly with whatever `namespace_kind` it needs.
    """
    connection = await asyncpg.connect(test_database_url)
    try:
        await migrate(connection, backend="postgres")
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def postgres_pool(test_database_url: str) -> AsyncIterator[asyncpg.Pool]:
    """`postgres_conn`'s own pool-shaped counterpart, for `hybrid_search` (which
    needs a `_search_connection`-compatible pool, not a single connection already
    inside a transaction of this fixture's own).
    """
    migration_conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(migration_conn, backend="postgres")
    finally:
        await migration_conn.close()

    pool = await asyncpg.create_pool(test_database_url)
    try:
        yield pool
    finally:
        await pool.close()


def _unit_vector(rng: random.Random, dimension: int = _DIMENSION) -> list[float]:
    """A deterministic, uniform-random unit vector - two different `rng` seeds
    give near-orthogonal vectors at this dimension (cosine similarity ~0), the
    same high-dimensional behaviour `docs/research/vector-index.md` §0 found for
    its own synthetic rows.
    """
    vector = [rng.gauss(0, 1) for _ in range(dimension)]
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector]


_WORD_RE = re.compile(r"[a-z0-9]+")


def _bow_vector(text: str, dimension: int = _DIMENSION) -> list[float]:
    """A deterministic, hash-based bag-of-words vector for `text` (same idea as
    `tests/search/test_hybrid.py`'s own `_bow_vector`, at `_DIMENSION` width) -
    not a real embedding, just good enough that cosine similarity tracks shared
    vocabulary between two texts.
    """
    vector = [0.0] * dimension
    for word in _WORD_RE.findall(text.lower()):
        digest = hashlib.sha1(word.encode("utf-8"), usedforsecurity=False).hexdigest()
        bucket = int(digest, 16) % dimension
        vector[bucket] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm > 0 else vector


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in vector) + "]"


async def _insert_note(
    conn: _Conn, note_id: str, namespace: str, *, archived: bool = False
) -> None:
    await conn.execute(
        """
        insert into notes
            (id, path, namespace, type, slug, title, description, created, updated,
             archived, file_hash)
        values ($1, $2, $3, 'fact', $1, $1, 'a description', now(), now(), $4, md5($1))
        """,
        note_id,
        f"{namespace}/fact/{note_id}.md",
        namespace,
        archived,
    )


async def _insert_chunk(
    conn: _Conn,
    *,
    note_id: str,
    namespace: str,
    namespace_kind: str,
    embedding: Sequence[float],
    text: str = "chunk text",
    ord: int = 0,
) -> None:
    await conn.execute(
        """
        insert into chunks
            (note_id, namespace, namespace_kind, ord, text, embedding, model, dimension)
        values ($1, $2, $3, $4, $5, $6::halfvec, $7, $8)
        """,
        note_id,
        namespace,
        namespace_kind,
        ord,
        text,
        _vector_literal(embedding),
        _MODEL,
        _DIMENSION,
    )


async def _seed_note_with_chunk(
    conn: _Conn,
    *,
    note_id: str,
    namespace: str,
    namespace_kind: str,
    embedding: Sequence[float],
    text: str = "chunk text",
) -> None:
    await _insert_note(conn, note_id, namespace)
    await _insert_chunk(
        conn,
        note_id=note_id,
        namespace=namespace,
        namespace_kind=namespace_kind,
        embedding=embedding,
        text=text,
    )


class TestPartitionRouting:
    """Each kind's query resolves to that kind's own partition - and only an
    HNSW-tuned kind's partition ever carries an HNSW index at all.
    """

    async def test_each_kind_scans_its_own_partition_only(
        self, postgres_conn: asyncpg.Connection
    ) -> None:
        rng = random.Random(1)  # noqa: S311 - deterministic test fixture, not crypto
        for kind in _VECTOR_KINDS:
            await _seed_note_with_chunk(
                postgres_conn,
                note_id=f"note-{kind}",
                namespace=f"ns-{kind}",
                namespace_kind=kind,
                embedding=_unit_vector(rng),
            )
        await postgres_conn.execute("analyze chunks")

        query_vector = _unit_vector(random.Random(2))  # noqa: S311 - deterministic test fixture, not crypto
        for kind in _VECTOR_KINDS:
            rows = await _fetch_vector_kind_rows(
                postgres_conn,
                kind,
                query_vector,
                _MODEL,
                _DIMENSION,
                limit=10,
                include_archived=False,
                types=None,
                tags=None,
                namespaces=None,
                valid_at=None,
            )
            assert len(rows) == 1, f"expected exactly the {kind} seed row, got {rows}"

            # The real production SQL (`_VECTOR_SQL_KIND_TEMPLATE`), not a
            # hand-rolled stand-in - a regression in that text should fail
            # this routing check too, not just `_fetch_vector_kind_rows`'s own
            # row-count assertion above.
            sql = "explain (format json) " + _VECTOR_SQL_KIND_TEMPLATE.format(dim=_DIMENSION)
            async with postgres_conn.transaction():
                for statement in _HNSW_KIND_SETTINGS[kind]:
                    await postgres_conn.execute(statement)
                plan = await postgres_conn.fetch(
                    sql,
                    _vector_literal(query_vector),
                    kind,
                    _MODEL,
                    _DIMENSION,
                    False,
                    None,
                    [],
                    None,
                    None,
                    10,
                )
            raw_plan = plan[0]["QUERY PLAN"]
            parsed_plan = json.loads(raw_plan) if isinstance(raw_plan, str) else raw_plan
            relation_names = _relation_names(parsed_plan[0]["Plan"])
            chunks_relations = relation_names & {f"chunks_{other}" for other in _VECTOR_KINDS}
            assert chunks_relations == {f"chunks_{kind}"}, (
                f"{kind}'s query touched {chunks_relations}, expected only chunks_{kind}"
            )

    async def test_only_group_project_org_partitions_carry_an_hnsw_index(
        self, postgres_conn: asyncpg.Connection
    ) -> None:
        index_names = {
            row["indexname"]
            for row in await postgres_conn.fetch(
                "select indexname from pg_indexes where tablename like 'chunks_%'"
            )
        }
        assert "chunks_user_embedding_hnsw_idx" not in index_names
        for kind in ("group", "project", "org"):
            assert f"chunks_{kind}_embedding_hnsw_idx" in index_names


def _relation_names(node: object) -> set[str]:
    """Every `Relation Name` anywhere in an `EXPLAIN (FORMAT JSON)` plan tree."""
    found: set[str] = set()
    if isinstance(node, dict):
        relation = node.get("Relation Name")
        if relation is not None:
            found.add(str(relation))
        for value in node.values():
            found |= _relation_names(value)
    elif isinstance(node, list):
        for item in node:
            found |= _relation_names(item)
    return found


class TestHnswSettingsReachTheSession:
    """`_fetch_vector_kind_rows` issues `_HNSW_KIND_SETTINGS[kind]` as real `SET
    LOCAL`s on the connection, not just text in a Python constant - checked via
    `current_setting` inside the same (enclosing) transaction, since `SET LOCAL`
    reverts at that transaction's end and cannot be observed from outside it.
    """

    async def test_each_kinds_settings_reach_current_setting(
        self, postgres_conn: asyncpg.Connection
    ) -> None:
        rng = random.Random(3)  # noqa: S311 - deterministic test fixture, not crypto
        await _seed_note_with_chunk(
            postgres_conn,
            note_id="settings-note",
            namespace="settings-ns",
            namespace_kind="group",
            embedding=_unit_vector(rng),
        )
        query_vector = _unit_vector(rng)

        for kind in _VECTOR_KINDS:
            async with postgres_conn.transaction():
                await _fetch_vector_kind_rows(
                    postgres_conn,
                    kind,
                    query_vector,
                    _MODEL,
                    _DIMENSION,
                    limit=10,
                    include_archived=False,
                    types=None,
                    tags=None,
                    namespaces=None,
                    valid_at=None,
                )
                observed = {
                    "hnsw.iterative_scan": await postgres_conn.fetchval(
                        "select current_setting('hnsw.iterative_scan')"
                    ),
                    "hnsw.ef_search": await postgres_conn.fetchval(
                        "select current_setting('hnsw.ef_search')"
                    ),
                    "hnsw.max_scan_tuples": await postgres_conn.fetchval(
                        "select current_setting('hnsw.max_scan_tuples')"
                    ),
                }

            expected = {
                statement.split("=")[0].removeprefix("SET LOCAL ").strip(): statement.split("=")[
                    1
                ].strip()
                for statement in _HNSW_KIND_SETTINGS[kind]
            }
            assert observed == expected, f"kind={kind}: {observed} != {expected}"


@dataclass
class _FakeProvider:
    """A deterministic `EmbeddingProvider` stand-in: `_bow_vector` of the text."""

    model: str = _MODEL
    dimension: int = _DIMENSION

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [_bow_vector(text, self.dimension) for text in texts]


class TestPersonalRanksFirstUnderOrgNoise:
    """A personal note that genuinely matches the query (both textually and by
    embedding) outranks a large `org` partition's worth of noise that only
    coincidentally resembles the query vector - the regression ADR-0016's
    per-kind querying guards against: a single, unpartitioned query with a
    fixed `limit` could have the `org` partition's sheer row count crowd the
    personal chunk out of the candidate set entirely before vector_search ever
    saw it, regardless of ranking.
    """

    async def test_personal_note_ranks_first(self, postgres_pool: asyncpg.Pool) -> None:
        query = "turtle sanctuary budget"
        provider = _FakeProvider()
        query_vector = _bow_vector(query)

        async with postgres_pool.acquire() as conn:
            await _seed_note_with_chunk(
                conn,
                note_id="personal-note",
                namespace="me",
                namespace_kind="user",
                embedding=query_vector,
                text="Notes on the turtle sanctuary budget for this year.",
            )

            rng = random.Random(7)  # noqa: S311 - deterministic test fixture, not crypto
            decoy_topics = [
                "quarterly roadmap meeting",
                "server migration checklist",
                "holiday schedule reminder",
                "office supply inventory",
                "parking garage renovation",
            ]
            rows = []
            for i in range(300):
                note_id = f"org-decoy-{i}"
                await _insert_note(conn, note_id, "everyone")
                rows.append(
                    (
                        note_id,
                        "everyone",
                        "org",
                        0,
                        decoy_topics[i % len(decoy_topics)],
                        _vector_literal(_unit_vector(rng)),
                        _MODEL,
                        _DIMENSION,
                    )
                )
            await conn.executemany(
                "insert into chunks "
                "(note_id, namespace, namespace_kind, ord, text, embedding, model, dimension) "
                "values ($1, $2, $3, $4, $5, $6::halfvec, $7, $8)",
                rows,
            )

        hits = await hybrid_search(postgres_pool, query, provider=provider, limit=5)

        assert hits, "expected at least one hit"
        assert hits[0].note_id == "personal-note"


class TestGroupFilteredRecallMatchesExactScan:
    """Filtering a `group`-kind search to one namespace (`SearchFilters.namespaces`)
    returns exactly the same ranking a brute-force exact scan of that namespace's
    own rows would - whatever access path Postgres actually used to get there.
    """

    async def test_filtered_group_search_matches_exact_scan(
        self, postgres_conn: asyncpg.Connection
    ) -> None:
        rng = random.Random(11)  # noqa: S311 - deterministic test fixture, not crypto
        query_vector = _unit_vector(rng)

        target_namespace = "team-a"
        other_namespaces = [f"team-{letter}" for letter in "bcdefghijklmno"]

        target_vectors: dict[str, list[float]] = {}
        for i in range(20):
            note_id = f"{target_namespace}-note-{i}"
            vector = _unit_vector(rng)
            target_vectors[note_id] = vector
            await _seed_note_with_chunk(
                postgres_conn,
                note_id=note_id,
                namespace=target_namespace,
                namespace_kind="group",
                embedding=vector,
            )

        for namespace in other_namespaces:
            for i in range(20):
                note_id = f"{namespace}-note-{i}"
                await _seed_note_with_chunk(
                    postgres_conn,
                    note_id=note_id,
                    namespace=namespace,
                    namespace_kind="group",
                    embedding=_unit_vector(rng),
                )
        await postgres_conn.execute("analyze chunks")

        expected_order = sorted(
            target_vectors,
            key=lambda note_id: -_dot(query_vector, target_vectors[note_id]),
        )[:5]

        hits = await vector_search(
            postgres_conn, query_vector, _MODEL, _DIMENSION, limit=5, namespaces=[target_namespace]
        )

        assert [hit.note_id for hit in hits] == expected_order


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class TestGitModeKeepsOneQuery:
    """Git mode (no `backend="postgres"` migration) issues exactly the one query
    it always has - `_vector_search_legs` returns a single-element list, unlike
    the per-kind legs the ADR-0016 layout gives above. The flat `chunks` table
    this builds (`migrations/0001_index_schema.sql`) has no `namespace`/
    `namespace_kind` columns at all, so this seeds it directly rather than
    through `_insert_chunk` (which assumes the ADR-0016 layout).
    """

    async def test_single_leg_without_the_adr_0016_layout(self, conn: asyncpg.Connection) -> None:
        await migrate(conn)  # backend="git" (the default).
        note_id = "git-note"
        await _insert_note(conn, note_id, "personal")
        await conn.execute(
            "insert into chunks (note_id, ord, text, embedding, model, dimension) "
            "values ($1, 0, 'chunk text', $2::vector, $3, $4)",
            note_id,
            _vector_literal(_unit_vector(random.Random(13))),  # noqa: S311 - deterministic test fixture, not crypto
            _MODEL,
            _DIMENSION,
        )

        legs = await _vector_search_legs(
            conn,
            _unit_vector(random.Random(14)),  # noqa: S311 - deterministic test fixture, not crypto
            _MODEL,
            _DIMENSION,
            limit=10,
            include_archived=False,
            types=None,
            tags=None,
            namespaces=None,
            valid_at=None,
        )

        assert len(legs) == 1
        assert [hit.note_id for hit in legs[0]] == [note_id]
