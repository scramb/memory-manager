# SPDX-License-Identifier: AGPL-3.0-only
"""Full-text and hybrid search over the derived Postgres index (#28, #29).

`fulltext_search` ranks `chunks` rows with `ts_rank_cd` over two tsvector
columns the schema already maintains (`0001_index_schema.sql`):
`tsv_simple`, generated with the `simple` config (exact tokens, language
independent - proper names like `pgvector` or `bge-m3`), and `tsv_lang`,
generated with `german`/`english` per chunk (`chunks.lang`, falling back to
`simple` when unset). A chunk matches if either tsvector matches the
corresponding `websearch_to_tsquery` query for `query`; its score is the
better of the two ranks. `websearch_to_tsquery` also gives us phrase
("..."), exclusion (-word) and `or` syntax for free.

A query that is only stopwords in a chunk's language config parses to an
empty tsquery; `@@`/`ts_rank_cd` treat that as "no match"/`0` rather than
erroring (verified against Postgres 16), so such a query still matches via
`tsv_simple` - `simple` has no stopword list - without special-casing here.

`vector_search` ranks chunks by cosine distance to a query embedding,
restricted to the embedding's `(model, dimension)` pair - chunks embedded
by a different model/provider are invisible to it, exactly like
`Indexer.ensure_vector_index`'s partial HNSW index. It reuses that index's
expression, `(embedding::vector(<dim>)) <=> $1::vector(<dim>)` under
`where model = ... and dimension = ...`, so the query is plannable against
it; `<dim>` is baked into the SQL text rather than bound, since a `vector`
type modifier cannot be a query parameter in Postgres.

`hybrid_search` is the one entry point meant for callers: it runs both
searches (vector only with a `provider`), fuses their per-chunk rankings
with Reciprocal Rank Fusion (`rrf_fuse`), collapses chunks to one `NoteHit`
per note - score is the best chunk's fused score, ties broken by the sum of
its chunks' scores then `note_id` - and renders a snippet for the winning
chunk: `ts_headline` when it came from the full-text side, else a plain
excerpt. Without a `provider`, or when embedding the query raises
`EmbeddingError`, it degrades to full-text-only ranking - the PLAN's
"hybrid search, full-text fallback without an embedding provider".
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import asyncpg
import asyncpg.pool

from memory_manager.index.embeddings import EmbeddingError, EmbeddingProvider

__all__ = [
    "ChunkHit",
    "NoteHit",
    "SearchFilters",
    "fulltext_search",
    "hybrid_search",
    "rrf_fuse",
    "vector_search",
]

_logger = logging.getLogger(__name__)

# `fulltext_search`/`vector_search` are called with a pool (indexing/CLI
# use) or a single connection from one (tests acquire their own); both
# expose the same `fetch`/`fetchval` convenience methods, so no branching
# is needed here.
_Queryable = asyncpg.Pool | asyncpg.pool.PoolConnectionProxy | asyncpg.Connection

_FULLTEXT_SQL = """
with q as (
    select
        websearch_to_tsquery('simple', $1) as simple,
        websearch_to_tsquery('german', $1) as german,
        websearch_to_tsquery('english', $1) as english
)
select
    n.id as note_id,
    n.path as path,
    c.id as chunk_id,
    c.ord as ord,
    c.heading_path as heading_path,
    c.text as text,
    greatest(
        ts_rank_cd(c.tsv_simple, q.simple),
        case c.lang
            when 'de' then ts_rank_cd(c.tsv_lang, q.german)
            when 'en' then ts_rank_cd(c.tsv_lang, q.english)
            else 0
        end
    ) as score
from chunks c
join notes n on n.id = c.note_id
cross join q
where ($2 or not n.archived)
    and ($4::text[] is null or n.type = any($4))
    and (n.tags @> $5)
    and ($6::text[] is null or n.namespace = any($6))
    and ($7::date is null or (
        (n.valid_from is null or n.valid_from <= $7)
        and (n.valid_to is null or n.valid_to >= $7)
    ))
    and (
        c.tsv_simple @@ q.simple
        or (c.lang = 'de' and c.tsv_lang @@ q.german)
        or (c.lang = 'en' and c.tsv_lang @@ q.english)
    )
order by score desc, c.id asc
limit $3
"""

# `{dim}` is filled in by `vector_search` itself, not by asyncpg - see the
# module docstring on why the vector type modifier can't be a bound param.
_VECTOR_SQL_TEMPLATE = """
select
    n.id as note_id,
    n.path as path,
    c.id as chunk_id,
    c.ord as ord,
    c.heading_path as heading_path,
    c.text as text,
    1 - ((c.embedding::vector({dim})) <=> $1::vector({dim})) as score
from chunks c
join notes n on n.id = c.note_id
where c.model = $2
    and c.dimension = $3
    and ($4 or not n.archived)
    and ($5::text[] is null or n.type = any($5))
    and (n.tags @> $6)
    and ($7::text[] is null or n.namespace = any($7))
    and ($8::date is null or (
        (n.valid_from is null or n.valid_from <= $8)
        and (n.valid_to is null or n.valid_to >= $8)
    ))
order by (c.embedding::vector({dim})) <=> $1::vector({dim}) asc
limit $9
"""

_HEADLINE_SQL = "select ts_headline('simple', $1, websearch_to_tsquery('simple', $2), $3)"
_HEADLINE_OPTIONS = "StartSel=**, StopSel=**, MaxWords=35, MinWords=15, MaxFragments=2"
_FALLBACK_SNIPPET_CHARS = 240

_NOTES_BY_ID_SQL = "select id, path, title, description, type, tags from notes where id = any($1)"


@dataclass(frozen=True)
class ChunkHit:
    """One chunk matched by `fulltext_search` or `vector_search`, with its score."""

    note_id: str
    path: str
    chunk_id: int
    ord: int
    heading_path: str
    text: str
    score: float


@dataclass(frozen=True)
class NoteHit:
    """One note surfaced by `hybrid_search`, with its best matching chunk's snippet."""

    note_id: str
    path: str
    title: str
    description: str
    type: str
    tags: tuple[str, ...]
    snippet: str
    score: float
    matched_chunks: int


@dataclass(frozen=True)
class SearchFilters:
    """Filters applied identically to the full-text and vector sides.

    `tags` is all-of (a note must carry every tag listed); the other
    sequence filters are any-of. Empty sequences and `valid_at=None` mean
    "no filter", `include_archived=False` excludes archived notes.
    """

    types: Sequence[str] = ()
    tags: Sequence[str] = ()
    namespaces: Sequence[str] = ()
    valid_at: date | None = None
    include_archived: bool = False


async def fulltext_search(
    conn_or_pool: _Queryable,
    query: str,
    *,
    limit: int = 50,
    include_archived: bool = False,
    types: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    namespaces: Sequence[str] | None = None,
    valid_at: date | None = None,
) -> list[ChunkHit]:
    """Rank chunks against `query` with Postgres full-text search.

    Matches on `chunks.tsv_simple` (exact tokens, any language) or, per
    chunk, `chunks.tsv_lang` in its detected language config; ranked with
    `ts_rank_cd`, best score first. An empty or whitespace-only `query`
    returns `[]` without touching the database. Notes with `archived` set
    are excluded unless `include_archived` is `True`. `types`/`namespaces`
    are any-of filters on the note; `tags` is all-of; `valid_at` excludes
    notes outside their `valid_from`/`valid_to` range (both unset means
    always valid). `None`/empty for any filter means "no filter".
    """
    if not query.strip():
        return []

    rows = await conn_or_pool.fetch(
        _FULLTEXT_SQL,
        query,
        include_archived,
        limit,
        list(types) if types else None,
        list(tags) if tags else [],
        list(namespaces) if namespaces else None,
        valid_at,
    )
    return [_row_to_chunk_hit(row) for row in rows]


async def vector_search(
    conn_or_pool: _Queryable,
    embedding: Sequence[float],
    model: str,
    dimension: int,
    *,
    limit: int = 50,
    include_archived: bool = False,
    types: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    namespaces: Sequence[str] | None = None,
    valid_at: date | None = None,
) -> list[ChunkHit]:
    """Rank chunks by cosine similarity of `embedding` to `chunks.embedding`.

    Only considers chunks stamped with `model` and `dimension` - a chunk
    embedded by a different provider or a stale model never matches. Score
    is `1 - cosine distance`, best first. Filters have the same meaning as
    on `fulltext_search`.
    """
    sql = _VECTOR_SQL_TEMPLATE.format(dim=dimension)
    rows = await conn_or_pool.fetch(
        sql,
        _vector_literal(embedding),
        model,
        dimension,
        include_archived,
        list(types) if types else None,
        list(tags) if tags else [],
        list(namespaces) if namespaces else None,
        valid_at,
        limit,
    )
    return [_row_to_chunk_hit(row) for row in rows]


async def hybrid_search(
    pool: asyncpg.Pool,
    query: str,
    *,
    provider: EmbeddingProvider | None = None,
    filters: SearchFilters | None = None,
    limit: int = 8,
    k: int = 60,
    candidates: int = 50,
) -> list[NoteHit]:
    """Rank notes by fusing full-text and (optional) vector chunk rankings.

    Runs `fulltext_search` for up to `candidates` chunks, and - if
    `provider` is given - embeds `query` and runs `vector_search` for up to
    `candidates` more. A failed query embedding (`EmbeddingError`) is
    logged and treated the same as no `provider`: full-text only, no
    exception raised. The two chunk rankings are fused per chunk with
    `rrf_fuse(..., k=k)`; a note's score is its best chunk's fused score
    (ties: the sum of its chunks' scores, then `note_id`), and
    `matched_chunks` counts how many distinct chunks of it were ranked by
    either side. The top `limit` notes are returned, each with a snippet
    for its best chunk: a highlighted `ts_headline` excerpt if that chunk
    came from the full-text side, else a plain excerpt of its text.
    """
    if not query.strip():
        return []
    filters = filters if filters is not None else SearchFilters()

    fulltext_hits = await fulltext_search(
        pool,
        query,
        limit=candidates,
        include_archived=filters.include_archived,
        types=filters.types,
        tags=filters.tags,
        namespaces=filters.namespaces,
        valid_at=filters.valid_at,
    )

    vector_hits: list[ChunkHit] = []
    if provider is not None:
        try:
            embeddings = await provider.embed([query])
        except EmbeddingError as exc:
            _logger.info(
                "hybrid_search: embedding the query failed, falling back to full-text only: %s",
                exc,
            )
        else:
            if embeddings:
                vector_hits = await vector_search(
                    pool,
                    embeddings[0],
                    provider.model,
                    len(embeddings[0]),
                    limit=candidates,
                    include_archived=filters.include_archived,
                    types=filters.types,
                    tags=filters.tags,
                    namespaces=filters.namespaces,
                    valid_at=filters.valid_at,
                )

    if not fulltext_hits and not vector_hits:
        return []

    chunks_by_id: dict[int, ChunkHit] = {}
    fulltext_chunk_ids: set[int] = set()
    rank_lists: list[list[int]] = []

    if fulltext_hits:
        rank_lists.append([hit.chunk_id for hit in fulltext_hits])
        for hit in fulltext_hits:
            chunks_by_id.setdefault(hit.chunk_id, hit)
            fulltext_chunk_ids.add(hit.chunk_id)
    if vector_hits:
        rank_lists.append([hit.chunk_id for hit in vector_hits])
        for hit in vector_hits:
            chunks_by_id.setdefault(hit.chunk_id, hit)

    chunk_scores = rrf_fuse(rank_lists, k=k)

    note_chunk_scores: dict[str, list[float]] = {}
    note_best_chunk: dict[str, tuple[float, int]] = {}
    for chunk_id, score in chunk_scores.items():
        hit = chunks_by_id[chunk_id]
        note_chunk_scores.setdefault(hit.note_id, []).append(score)
        best = note_best_chunk.get(hit.note_id)
        if best is None or score > best[0] or (score == best[0] and chunk_id < best[1]):
            note_best_chunk[hit.note_id] = (score, chunk_id)

    ordered_note_ids = sorted(
        note_chunk_scores,
        key=lambda note_id: (
            -max(note_chunk_scores[note_id]),
            -sum(note_chunk_scores[note_id]),
            note_id,
        ),
    )[:limit]

    note_rows = await _fetch_notes(pool, ordered_note_ids)

    results: list[NoteHit] = []
    for note_id in ordered_note_ids:
        row = note_rows.get(note_id)
        if row is None:
            # The note existed when the chunk was matched but is gone now
            # (e.g. deleted between the two queries); skip rather than fail.
            continue

        score, chunk_id = note_best_chunk[note_id]
        best_chunk = chunks_by_id[chunk_id]
        if chunk_id in fulltext_chunk_ids:
            snippet = await _headline(pool, best_chunk.text, query)
        else:
            snippet = _fallback_snippet(best_chunk.text)

        results.append(
            NoteHit(
                note_id=note_id,
                path=row["path"],
                title=row["title"],
                description=row["description"],
                type=row["type"],
                tags=tuple(row["tags"]),
                snippet=snippet,
                score=score,
                matched_chunks=len(note_chunk_scores[note_id]),
            )
        )
    return results


def rrf_fuse[RankItem](
    rank_lists: Sequence[Sequence[RankItem]], k: int = 60
) -> dict[RankItem, float]:
    """Fuse several best-first rankings with Reciprocal Rank Fusion.

    For each item, sums `1 / (k + rank)` over every list it appears in
    (1-based rank within that list); an item missing from a list simply
    does not contribute from it. Pure function, no I/O - the chunk/note
    aggregation around it lives in `hybrid_search`.
    """
    scores: dict[RankItem, float] = {}
    for ranked in rank_lists:
        for rank, item in enumerate(ranked, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return scores


async def _fetch_notes(pool: asyncpg.Pool, note_ids: Sequence[str]) -> dict[str, asyncpg.Record]:
    if not note_ids:
        return {}
    rows = await pool.fetch(_NOTES_BY_ID_SQL, list(note_ids))
    return {row["id"]: row for row in rows}


async def _headline(pool: asyncpg.Pool, text: str, query: str) -> str:
    result = await pool.fetchval(_HEADLINE_SQL, text, query, _HEADLINE_OPTIONS)
    return str(result)


def _fallback_snippet(text: str) -> str:
    """The chunk's content, without its `title`/`heading_path` prefix lines.

    `chunker.chunk_note` prefixes every chunk's text with `title` (and the
    heading path, if any), then a blank line, then the content - so the
    content always starts right after the first blank line.
    """
    separator = text.find("\n\n")
    content = text[separator + 2 :] if separator != -1 else text
    return content.strip()[:_FALLBACK_SNIPPET_CHARS]


def _row_to_chunk_hit(row: asyncpg.Record) -> ChunkHit:
    return ChunkHit(
        note_id=row["note_id"],
        path=row["path"],
        chunk_id=row["chunk_id"],
        ord=row["ord"],
        heading_path=row["heading_path"],
        text=row["text"],
        score=row["score"],
    )


def _vector_literal(vector: Sequence[float]) -> str:
    """Render `vector` as the `'[1.0,2.0,...]'` text pgvector parses via `::vector`."""
    return "[" + ",".join(str(float(value)) for value in vector) + "]"
