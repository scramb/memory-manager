# SPDX-License-Identifier: AGPL-3.0-only
"""Full-text and hybrid search over the derived Postgres index (#28, #29).

`fulltext_search` ranks `chunks` rows with `ts_rank_cd` over two tsvector
columns the schema already maintains (`0001_index_schema.sql`):
`tsv_simple`, generated with the `simple` config (exact tokens, language
independent - proper names like `pgvector` or `bge-m3`), and `tsv_lang`,
generated with `german`/`english` per chunk (`chunks.lang`, falling back to
`simple` when unset). Its score is the better of the two ranks, normalized
by chunk length (`ts_rank_cd(..., 33)`) so a longer chunk does not win on
size alone. `websearch_to_tsquery` also gives us phrase ("..."), exclusion
(-word) and `or` syntax for free.

`websearch_to_tsquery` ANDs plain words together by default, which would
require every word of a natural-language question to appear in one chunk -
almost never true for a real sentence (#63). Per config, `_FULLTEXT_SQL`
rewrites a query that has no phrase/exclusion operator from an AND of its
words into an OR, so a chunk matching any one word still ranks, with
`ts_rank_cd` rewarding chunks that match more of them. A query using a
quoted phrase or `-exclusion` keeps its original (necessarily more
restrictive) semantics unchanged.

`tsv_simple` only ever contributes to a *detected-language* chunk's
score/match when its language's OR'd query has no lexemes at all (a
stopword-only query, verified against Postgres 16 to parse to an empty
tsquery that `@@`/`ts_rank_cd` treat as "no match"/`0` rather than
erroring). Letting `tsv_simple` compete on every chunk regardless, now that
it is OR'd too, backfires: `simple` has no stopword list, so common words
like "the" or "is" - filtered out of the `german`/`english` OR for every
other chunk - would literally match almost any chunk through it and drown
out real matches (found empirically against this vault's eval set, #63).

A chunk with `chunks.lang is null` needs the same protection but can't use
the per-chunk check above - its `tsv_lang` falls back to `tsv_simple`
itself when `lang` is unset (`0001_index_schema.sql`), so there is no
"this chunk's own language" to compare `query` against. `chunks.lang` is a
stopword-count heuristic (`chunker.detect_lang`) that gives up on plenty of
short, perfectly ordinary sentences, not just genuinely undetectable text,
so this is a common case, not an edge one - and simply trying both
`german`'s and `english`'s stemming on the chunk text does not help: a
word is dropped as a stopword by at most *one* of the two dictionaries
("the" is English-only, "die" is German-only), so whichever language's
pass is tried, the other one's filler words still slip through and
pollute the match. `q.safe` (the `safe` CTE) sidesteps this: it is `query`'s
own words, reduced to the ones neither dictionary drops, OR'd together and
matched against `tsv_simple` literally - no per-chunk language guess
needed. `tsv_simple`/`q.simple` is still the branch above's fallback
(`query` itself being stopword-only in a chunk's *detected* language is a
narrower, already-safe case: nothing in that single language's stopword
list survives into `q.simple`'s matching chunk anyway).

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
from memory_manager.observability.metrics import track_search

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
with q_and as (
    select
        websearch_to_tsquery('simple', $1) as simple,
        websearch_to_tsquery('german', $1) as german,
        websearch_to_tsquery('english', $1) as english
),
-- `websearch_to_tsquery` AND's plain words together, which makes a natural-
-- language question ("Where is Elbblick?") require every one of its words
-- - including ones the query's own language config never stems away, like
-- an English "is" inside a German chunk's "german" query - to appear in
-- the same chunk. That is almost never true for a real sentence, so plain
-- words are OR'd instead below: a chunk matching any one of them still
-- ranks, with `ts_rank_cd` rewarding chunks that match more of them.
--
-- `allow_or` keeps the user's own phrase/exclusion syntax (`"phrase"`,
-- `-word`) required/excluded, by looking at the *raw* query text rather
-- than the parsed tsquery: `websearch_to_tsquery` also renders a
-- hyphenated compound word like "auth-guide" as a `<->` phrase of its own
-- (verified against Postgres 16) with no exclusion or quoting on the
-- user's part, so testing the parsed query for `<->`/`!` would wrongly
-- keep plain, hyphen-containing questions AND-only.
--
-- The `&` -> `|` rewrite below casts the edited text straight to
-- `tsquery` (`::tsquery`), not back through `to_tsquery(config, ...)`:
-- the latter re-runs the config's parser/dictionary over every lexeme,
-- including already-quoted ones - which re-triggers the very hyphen
-- expansion above and corrupts it (verified against Postgres 16:
-- `to_tsquery('simple', $$'bge-m3' <-> 'bge' <-> 'm3'$$)` yields a
-- 5-lexeme chain, not the original 3). `::tsquery` parses quoted lexemes
-- as literal and sidesteps that.
flags as (
    select ($1 !~ '"') and ($1 !~ '(^|\\s)-\\S') as allow_or
),
-- `safe` is a second, stricter OR query for the fallback below: only the
-- query's words that survive (are not a stopword in) *both* `german` and
-- `english` - derived straight from those two dictionaries, not a
-- hand-maintained list. "the"/"me"/"is" fail that bar (at least one of
-- the two configs drops them); "show"/"auth"/"guide" or any proper name
-- pass it, same as before.
safe as (
    select coalesce(string_agg(quote_literal(word), ' | '), '')::tsquery as query
    from (select distinct unnest(tsvector_to_array(to_tsvector('simple', $1))) as word) words
    where to_tsvector('german', word) != ''::tsvector
        and to_tsvector('english', word) != ''::tsvector
),
q as (
    select
        case
            when flags.allow_or then (replace(q_and.simple::text, '&', '|'))::tsquery
            else q_and.simple
        end as simple,
        case
            when flags.allow_or then (replace(q_and.german::text, '&', '|'))::tsquery
            else q_and.german
        end as german,
        case
            when flags.allow_or then (replace(q_and.english::text, '&', '|'))::tsquery
            else q_and.english
        end as english,
        safe.query as safe
    from q_and cross join flags cross join safe
)
select
    n.id as note_id,
    n.path as path,
    c.id as chunk_id,
    c.ord as ord,
    c.heading_path as heading_path,
    c.text as text,
    greatest(
        case
            -- `chunks.lang` is a stopword-count heuristic (`chunker.detect_lang`)
            -- that gives up (`null`) on many short, legitimate sentences - too
            -- short to hit its confidence bar, not actually undetectable. Such
            -- a chunk's `tsv_lang` falls back to `tsv_simple` itself
            -- (`0001_index_schema.sql`), so it cannot tell "the chunk's own
            -- language has no stopwords left in this query" from "we don't
            -- know the chunk's language at all" the way the branch below
            -- does - ranking it via plain `q.simple` would reintroduce the
            -- same "the"/"is" pollution for every chunk this heuristic
            -- merely failed to confidently tag. `q.safe` (see above) is the
            -- cross-language-filtered stand-in for that unknown case.
            when c.lang is null then ts_rank_cd(c.tsv_simple, q.safe, 33)
            when (c.lang = 'de' and q.german::text = '')
                or (c.lang = 'en' and q.english::text = '')
                then ts_rank_cd(c.tsv_simple, q.simple, 33)
            else 0
        end,
        case c.lang
            when 'de' then ts_rank_cd(c.tsv_lang, q.german, 33)
            when 'en' then ts_rank_cd(c.tsv_lang, q.english, 33)
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
        (
            (
                c.lang is null
                or (c.lang = 'de' and q.german::text = '')
                or (c.lang = 'en' and q.english::text = '')
            )
            and c.tsv_simple @@ q.simple
        )
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
    pool: _Queryable,
    query: str,
    *,
    provider: EmbeddingProvider | None = None,
    filters: SearchFilters | None = None,
    limit: int = 8,
    k: int = 60,
    candidates: int = 50,
) -> list[NoteHit]:
    """Rank notes by fusing full-text and (optional) vector chunk rankings.

    A thin, timed wrapper (`mm_search_duration_seconds{mode}`, #43) around
    `_hybrid_search_impl`, which carries the actual docstring and logic;
    `mode` is `"hybrid"` when `provider` is given, `"fulltext"` otherwise -
    the same distinction `mcp/server.py`'s `_search_mode` reports to a
    caller.

    `pool` accepts a single connection too (`_Queryable`, #116): `mcp/
    server.py`'s `memory_search` passes a `db.rls.request_connection` in
    `postgres` mode, so the whole search runs under the caller's own
    identity rather than the owner pool - `"git"` with `DATABASE_URL`
    configured still passes the plain owner pool, unaffected.
    """
    mode = "hybrid" if provider is not None else "fulltext"
    async with track_search(mode):
        return await _hybrid_search_impl(
            pool,
            query,
            provider=provider,
            filters=filters,
            limit=limit,
            k=k,
            candidates=candidates,
        )


async def _hybrid_search_impl(
    pool: _Queryable,
    query: str,
    *,
    provider: EmbeddingProvider | None = None,
    filters: SearchFilters | None = None,
    limit: int = 8,
    k: int = 60,
    candidates: int = 50,
) -> list[NoteHit]:
    """The ranking logic behind `hybrid_search`, timed by its thin wrapper above.

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


async def _fetch_notes(pool: _Queryable, note_ids: Sequence[str]) -> dict[str, asyncpg.Record]:
    if not note_ids:
        return {}
    rows = await pool.fetch(_NOTES_BY_ID_SQL, list(note_ids))
    return {row["id"]: row for row in rows}


async def _headline(pool: _Queryable, text: str, query: str) -> str:
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
