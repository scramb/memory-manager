# SPDX-License-Identifier: AGPL-3.0-only
"""Full-text search over the derived Postgres index (#28).

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
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg
import asyncpg.pool

__all__ = ["ChunkHit", "fulltext_search"]

# `fulltext_search` is called with a pool (indexing/CLI use) or a single
# connection from one (tests acquire their own); both expose the same
# `fetch` convenience method, so no branching is needed here.
_Queryable = asyncpg.Pool | asyncpg.pool.PoolConnectionProxy | asyncpg.Connection

_SQL = """
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
    and (
        c.tsv_simple @@ q.simple
        or (c.lang = 'de' and c.tsv_lang @@ q.german)
        or (c.lang = 'en' and c.tsv_lang @@ q.english)
    )
order by score desc, c.id asc
limit $3
"""


@dataclass(frozen=True)
class ChunkHit:
    """One chunk matched by `fulltext_search`, with its full-text rank."""

    note_id: str
    path: str
    chunk_id: int
    ord: int
    heading_path: str
    text: str
    score: float


async def fulltext_search(
    conn_or_pool: _Queryable,
    query: str,
    *,
    limit: int = 50,
    include_archived: bool = False,
) -> list[ChunkHit]:
    """Rank chunks against `query` with Postgres full-text search.

    Matches on `chunks.tsv_simple` (exact tokens, any language) or, per
    chunk, `chunks.tsv_lang` in its detected language config; ranked with
    `ts_rank_cd`, best score first. An empty or whitespace-only `query`
    returns `[]` without touching the database. Notes with `archived` set
    are excluded unless `include_archived` is `True`.
    """
    if not query.strip():
        return []

    rows = await conn_or_pool.fetch(_SQL, query, include_archived, limit)
    return [
        ChunkHit(
            note_id=row["note_id"],
            path=row["path"],
            chunk_id=row["chunk_id"],
            ord=row["ord"],
            heading_path=row["heading_path"],
            text=row["text"],
            score=row["score"],
        )
        for row in rows
    ]
