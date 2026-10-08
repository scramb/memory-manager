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
type modifier cannot be a query parameter in Postgres. This is the Git
backend's path, unchanged by ADR-0016 below (Git mode never applies
`migrations/postgres/0012_vector_layout.sql`).

**ADR-0016's per-kind vector search (#221).** On a database migrated with
`backend="postgres"` (detected via `schema_migrations`,
`POSTGRES_VECTOR_LAYOUT_VERSION` - the same check `index/indexer.py`'s
`_uses_vector_layout` makes), `chunks` is list-partitioned by
`namespace_kind` and `vector_search` issues one query per kind
(`_VECTOR_KINDS`) instead of one query over the whole table, each scoped to
that kind's own access path: `user`/personal - no HNSW index exists on
that partition at all (`0012_vector_layout.sql`), so the planner's own
exact sort over the (B-tree-narrowed) partition is what runs; `group`/
`project` - HNSW with `hnsw.iterative_scan = relaxed_order`, `ef_search =
200`, `max_scan_tuples = 20000`, the combination `docs/research/
vector-index.md` §1/§3 measured at full recall within the project's search
budget; `org` - HNSW, `ef_search = 400`, `iterative_scan = off` (ADR-0016:
"unfiltered, generously tuned ef_search", not yet validated against real
content, WP-32). Every kind's three `hnsw.*` settings are set explicitly,
including the ones a kind does not need (`user`'s has no effect - that
partition carries no HNSW index to apply them to): Postgres's `SET LOCAL`
persists to the end of the *top-level* transaction once a savepoint that
set it is released, not just to that savepoint's own scope, so leaving a
kind's settings implicit would let an earlier kind's `SET LOCAL` in the
same call leak into a later one's query rather than being reset by it.
`_fetch_vector_kind_rows` opens its own transaction (a nested savepoint
when `conn_or_pool` is already inside one, e.g. `_hybrid_search_impl`'s
custom-plan transaction - asyncpg's own nesting, the same pattern
`db/migrate.py` relies on for its per-migration savepoints) so each kind's
`SET LOCAL` only ever reaches that kind's own query.

Each kind's leg is its own best-first ranking (one query, one result set,
no cross-kind merge inside it); `_vector_search_legs` is what both
`vector_search` and `_hybrid_search_impl` build these from. `vector_search`
itself still returns one list for a direct caller (Git mode: that one
query's own list, unchanged; Postgres mode: the per-kind legs fused with
the existing `rrf_fuse` - reused, not a new algorithm - so a direct caller
still gets one best-first ranking across every kind). `_hybrid_search_impl`
instead feeds every leg straight into the *same* `rrf_fuse` call it already
uses for the full-text side (ADR-0016 Decision: "fuses them with RRF -
already the plan for full-text/vector fusion, so this adds no new fusion
step, only more vector legs") - one fusion step, now taking a full-text
list plus one list per visible kind instead of one combined vector list.

`hybrid_search` is the one entry point meant for callers: it runs both
searches (vector only with a `provider`), fuses their per-chunk rankings
with Reciprocal Rank Fusion (`rrf_fuse`), collapses chunks to one `NoteHit`
per note - score is the best chunk's fused score, ties broken by the sum of
its chunks' scores then `note_id` - and renders a snippet for the winning
chunk: `ts_headline` when it came from the full-text side, else a plain
excerpt. Without a `provider`, or when embedding the query raises
`EmbeddingError`, it degrades to full-text-only ranking - the PLAN's
"hybrid search, full-text fallback without an embedding provider".

**Bounding very frequent terms (#117, ADR-0007 addendum).** A chunk
matching any one OR'd lexeme still ranks (above), which means a lexeme
present in nearly every chunk - a common English/German filler word that
slipped past stemming, or just a word the vault happens to use everywhere -
makes `_FULLTEXT_SQL` compute `ts_rank_cd` for, and sort, nearly every row
of `chunks`. That cost grows with vault size, not with `limit`: the load
test measured 1.3s at 60k chunks and 8.8s at 1M. The fix is two-staged:
a `candidates` CTE first picks up to `_CANDIDATE_CAP` chunk ids - the exact
filters and match condition the ranked `select` used to apply directly,
plus (when available) a *selective* OR-query built only from the lexemes
that are not frequent - and only then does the outer `select` compute
`score`/sort, over at most `_CANDIDATE_CAP` rows rather than every match.

Frequency is looked up via `mm_frequent_lexemes` (`0008_frequent_lexemes
.sql`), a `security definer` function reading `pg_stats`/`pg_class`: a
lexeme is frequent once its estimated row count reaches
`_FREQUENT_LEXEME_MIN_ROWS`. Both constants are module-level and chosen
against `tests/search/test_fulltext_bounded.py`'s fixture (50k chunks, the
word "note" in every one of them, a unique marker per chunk): low enough
that "note" (estimated count ~50k) clears the threshold and a chunk's own
marker (estimated count ~0, below Postgres's most-common-element list
entirely) does not, high enough, and the cap a small enough multiple of
the threshold, that both stay far under the fixture's own 50k rows.

Detection depends on `ANALYZE` having run against `chunks` - a bulk load or
`reindex --full` needs to `analyze` it afterwards, or `mm_frequent_lexemes`
simply has no stats to read and returns no frequent lexemes, leaving only
the cap to bound the work. The same happens, deliberately, whenever *every*
lexeme of a branch turns out to be frequent (the query itself is just a
very common word): there is nothing selective left to filter the candidate
match on for that branch, so it falls back to its own unfiltered match
condition, capped the same way - an arbitrary (not best-ranked) capped
sample of the matching rows, rather than ranking all of them.
`fulltext_search` retries once, unfiltered but still capped, whenever the
selective-filtered attempt comes back with zero rows, so "a frequent word
or'd with a word nothing matches" still returns the frequent word's hits
instead of an empty list.

Under RLS (WP-19, #116) the app role has no `select` on `chunks`, and
Postgres hides a table's `pg_stats` rows from a role that cannot read the
table - `mm_frequent_lexemes` being `security definer` is what lets the app
role ask "is this lexeme frequent" at all; see the migration's own comment
for why its `execute` grant is left at the Postgres default (`public`)
rather than narrowed together with that table grant.

**Custom plans (#120).** asyncpg caches a prepared statement per
connection; by the sixth execution of the same statement text on one
connection, Postgres (`plan_cache_mode=auto`) stops planning it fresh and
switches to a generic plan - one that no longer sees `$1`'s actual value
and has to estimate `_FULLTEXT_SQL`'s `candidates` CTE from the column's
overall statistics instead. Measured against the load-test vault, that
estimate came out at 2 rows against an actual ~10,278, which the planner
then "fixes" with a nested loop that re-checks the tsquery match over
every one of `chunks`' rows rather than the few thousand a custom plan
would narrow down to first: roughly 600 ms a search, against ~22 ms
planned fresh each time - and under load, enough of the pool's
connections stall on that nested loop at once that unrelated reads and
writes queue up behind `pool.acquire()` too, not just search. Forcing a
connection-wide `plan_cache_mode` at pool startup would fix the symptom
but reaches every statement, not just `_FULLTEXT_SQL`'s, and is silently
dropped by a transaction-mode pooler (PgBouncer, a plausible front for the
`postgres` backend's write path) before it ever reaches Postgres, since
that mode never runs a session's startup parameters against the physical
connection it hands out per transaction. `_hybrid_search_impl` instead
opens one pool connection and a read-only transaction around every
statement a call makes, and issues `SET LOCAL plan_cache_mode =
force_custom_plan` as the transaction's first statement: pooler-safe
(scoped to the transaction, not the connection), and scoped to this one
code path rather than every statement on the connection. `hybrid_search`'s
thin wrapper and `track_search`'s timing are unaffected - both still see
the same call, just now backed by one connection instead of the pool for
its duration.

Under RLS (#116), `hybrid_search` is instead called with a connection
`mcp/server.py`'s `memory_search` already holds inside `rls.
request_identity`'s own transaction (via `rls.request_connection`) - not a
pool. `_hybrid_search_impl` tells the two apart (`isinstance(...,
asyncpg.Pool)`) and, for a connection, reuses that transaction rather than
opening a nested one of its own: asyncpg has no notion of a second,
independent transaction on a connection that already has one open, and
there is no need for one - `SET LOCAL plan_cache_mode = force_custom_plan`
is exactly as valid inside the caller's transaction as inside one this
function opens itself, and reverts with it the same way either way.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date

import asyncpg
import asyncpg.pool

from memory_manager.db.migrate import POSTGRES_VECTOR_LAYOUT_VERSION
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

# `fulltext_search`/`vector_search`/`_fetch_notes`/`_headline` are called
# with a pool (indexing/CLI use, and tests that exercise one of them in
# isolation) or a single connection from one (`_hybrid_search_impl` runs
# all four over the one connection it holds for the custom-plan
# transaction, #120 - see the module docstring); both expose the same
# `fetch`/`fetchval` convenience methods, so no branching is needed here.
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
),
-- Very frequent lexemes (#117, ADR-0007 addendum): any lexeme `query`'s
-- raw text contains, per config - straight from `to_tsvector`, not from
-- `q.*` above, so a phrase/exclusion rewrite there does not hide a lexeme
-- from this check. Removing a lexeme below only shrinks the *candidate*
-- match (`candidates`, further down); `q.*`/`q.safe` still rank every
-- candidate exactly as before.
-- `materialized`: without it, Postgres is free to inline `lexemes` (and,
-- downstream, `frequent`/`selective` below) into `candidates`' join tree
-- rather than computing it once - verified against Postgres 16 to turn
-- into a per-row re-evaluation of everything downstream of it, including
-- the `mm_frequent_lexemes` call `frequent` makes (#117 follow-up: at 50k
-- chunks, that call ran 25,000 times instead of once, dominating the
-- query's cost). None of the three CTEs references `c`/`n`, so forcing
-- materialization changes no result, only when the (cheap, one-row) work
-- happens.
lexemes as materialized (
    select
        (select coalesce(array_agg(distinct word), '{}')
            from unnest(tsvector_to_array(to_tsvector('simple', $1))) as word) as simple,
        (select coalesce(array_agg(distinct word), '{}')
            from unnest(tsvector_to_array(to_tsvector('german', $1))) as word) as german,
        (select coalesce(array_agg(distinct word), '{}')
            from unnest(tsvector_to_array(to_tsvector('english', $1))) as word) as english
),
-- `mm_frequent_lexemes` (`0008_frequent_lexemes.sql`) answers "which of
-- these lexemes does the planner's own statistics say cover at least $8
-- rows of `chunks`". `tsv_simple` and `tsv_lang` each need their own call
-- - the same word can be frequent in one column's stored forms and not
-- the other, since stemming differs per chunk; `german`/`english` share
-- one call, since both feed `tsv_lang`, the one physical column whose
-- statistics the function reads.
-- `materialized` for the same reason as `lexemes` above - this is the CTE
-- that actually calls `mm_frequent_lexemes`, so it is the one that most
-- needs to run exactly once rather than once per candidate row.
frequent as materialized (
    select
        mm_frequent_lexemes('tsv_simple', lexemes.simple, $8) as simple,
        mm_frequent_lexemes('tsv_lang', lexemes.german || lexemes.english, $8) as lang
    from lexemes
),
-- Each branch's lexemes, minus the frequent ones, OR'd together. Empty
-- text (`''`) means "every lexeme of this branch is frequent" (or the
-- branch had none at all) - `candidates` below then has nothing selective
-- to filter that branch's match on and falls back to its own unfiltered
-- condition, the same fallback `$10 = false` forces for every branch at
-- once (`fulltext_search`'s retry - see the module docstring).
-- `materialized` for the same reason as `lexemes`/`frequent` above - left
-- inlined, `candidates`' own join tree re-evaluates this CTE (and thus
-- `frequent`/`mm_frequent_lexemes`) once per row it filters, not once.
selective as materialized (
    select
        (select coalesce(string_agg(quote_literal(word), ' | '), '')
            from unnest(lexemes.simple) as word
            where word <> all (frequent.simple))::tsquery as simple,
        (select coalesce(string_agg(quote_literal(word), ' | '), '')
            from unnest(lexemes.german) as word
            where word <> all (frequent.lang))::tsquery as german,
        (select coalesce(string_agg(quote_literal(word), ' | '), '')
            from unnest(lexemes.english) as word
            where word <> all (frequent.lang))::tsquery as english
    from lexemes cross join frequent
),
-- Candidate stage: the note filters and match condition below are exactly
-- what used to sit directly on the ranked `select` (#28/#29) - only
-- `and (not $10 or selective.* ...)` is new. `limit $9` with no `order by`
-- lets the planner stop once it has $9 matches rather than needing every
-- one of them; `materialized` forces this CTE to run to completion before
-- the outer `select` computes a single `score`, so a query whose only
-- lexeme is frequent still only ever scores/sorts $9 rows, never every
-- match in `chunks` (the actual bug, #117: ranking every chunk took 1.3s
-- at 60k chunks, 8.8s at 1M).
candidates as materialized (
    select c.id
    from chunks c
    join notes n on n.id = c.note_id
    cross join q
    cross join selective
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
                and (not $10 or selective.simple::text = '' or c.tsv_simple @@ selective.simple)
            )
            or (
                c.lang = 'de' and c.tsv_lang @@ q.german
                and (not $10 or selective.german::text = '' or c.tsv_lang @@ selective.german)
            )
            or (
                c.lang = 'en' and c.tsv_lang @@ q.english
                and (not $10 or selective.english::text = '' or c.tsv_lang @@ selective.english)
            )
        )
    limit $9
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
from candidates
join chunks c on c.id = candidates.id
join notes n on n.id = c.note_id
cross join q
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

# ADR-0016's per-kind counterpart to `_VECTOR_SQL_TEMPLATE` above, used once
# `_uses_vector_layout` finds the postgres-mode `chunks` layout
# (`migrations/postgres/0012_vector_layout.sql`). `c.namespace_kind = $2`
# (bound, not baked in - unlike `{dim}`, a plain `text` value can be a
# parameter) is what lets the planner prune straight to one partition;
# `{dim}` is still baked into the SQL text for the same reason as the flat
# template - a `halfvec` type modifier cannot be a bound parameter either.
_VECTOR_SQL_KIND_TEMPLATE = """
select
    n.id as note_id,
    n.path as path,
    c.id as chunk_id,
    c.ord as ord,
    c.heading_path as heading_path,
    c.text as text,
    1 - ((c.embedding::halfvec({dim})) <=> $1::halfvec({dim})) as score
from chunks c
join notes n on n.id = c.note_id
where c.namespace_kind = $2
    and c.model = $3
    and c.dimension = $4
    and ($5 or not n.archived)
    and ($6::text[] is null or n.type = any($6))
    and (n.tags @> $7)
    -- `c.namespace` (denormalised from `n.namespace`, `migrations/postgres/
    -- 0012_vector_layout.sql`), not the joined `n.namespace` the flat
    -- `_VECTOR_SQL_TEMPLATE` above filters on: this is the predicate the
    -- migration's own B-tree (`chunks_namespace_idx`) exists to serve -
    -- filtering the joined column instead would never let the planner use
    -- it, no matter how selective `$8` is.
    and ($8::text[] is null or c.namespace = any($8))
    and ($9::date is null or (
        (n.valid_from is null or n.valid_from <= $9)
        and (n.valid_to is null or n.valid_to >= $9)
    ))
order by (c.embedding::halfvec({dim})) <=> $1::halfvec({dim}) asc
limit $10
"""

# The four `namespace_kind` values `migrations/postgres/0012_vector_layout.
# sql` partitions `chunks` into (`agent`, ADR-0013, attaches additively
# later - #220 "Not included" - and is not queried here yet).
_VECTOR_KINDS: tuple[str, ...] = ("user", "group", "project", "org")

# ADR-0016 §3 / `docs/research/vector-index.md` §1/§3's recommended
# starting parameters, one explicit, complete set per kind (see the module
# docstring: "Every kind's three `hnsw.*` settings are set explicitly" -
# this is what stops an earlier kind's `SET LOCAL` from leaking into a
# later one's query within the same call).
_HNSW_KIND_SETTINGS: dict[str, tuple[str, ...]] = {
    "user": (
        # No HNSW index exists on `chunks_user` at all (personal/agent:
        # B-tree on `namespace` plus exact sort, ADR-0016) - these settings
        # have nothing to apply to; listed anyway so this kind's query
        # never silently inherits another kind's still-open values.
        "SET LOCAL hnsw.iterative_scan = off",
        "SET LOCAL hnsw.ef_search = 40",
        "SET LOCAL hnsw.max_scan_tuples = 20000",
    ),
    "group": (
        "SET LOCAL hnsw.iterative_scan = relaxed_order",
        "SET LOCAL hnsw.ef_search = 200",
        "SET LOCAL hnsw.max_scan_tuples = 20000",
    ),
    "project": (
        "SET LOCAL hnsw.iterative_scan = relaxed_order",
        "SET LOCAL hnsw.ef_search = 200",
        "SET LOCAL hnsw.max_scan_tuples = 20000",
    ),
    "org": (
        # Unfiltered, generously tuned `ef_search` (ADR-0016 Decision,
        # option B); `iterative_scan` left `off` - the ADR does not
        # prescribe `relaxed_order` for `org` the way it does for
        # `group`/`project` (not fully settled, pending WP-32).
        "SET LOCAL hnsw.iterative_scan = off",
        "SET LOCAL hnsw.ef_search = 400",
        "SET LOCAL hnsw.max_scan_tuples = 20000",
    ),
}

# `vector_search`'s own fusion of per-kind legs for a direct caller
# (`_hybrid_search_impl` fuses the same legs itself, at the same `k` its
# full-text/vector fusion already uses by default - see `hybrid_search`).
_VECTOR_KIND_FUSE_K = 60

_SCHEMA_MIGRATION_EXISTS_SQL = "select exists(select 1 from schema_migrations where version = $1)"

_HEADLINE_SQL = "select ts_headline('simple', $1, websearch_to_tsquery('simple', $2), $3)"
_HEADLINE_OPTIONS = "StartSel=**, StopSel=**, MaxWords=35, MinWords=15, MaxFragments=2"
_FALLBACK_SNIPPET_CHARS = 240

_NOTES_BY_ID_SQL = "select id, path, title, description, type, tags from notes where id = any($1)"

# `_hybrid_search_impl`'s custom-plan transaction (#120, see the module
# docstring) - `SET LOCAL` so the setting only ever reaches the one
# transaction it is issued in, never leaking onto the connection's next
# user once it is released back to the pool.
_FORCE_CUSTOM_PLAN_SQL = "SET LOCAL plan_cache_mode = force_custom_plan"

# Bounding very frequent terms (#117, ADR-0007 addendum; see the module
# docstring) - a lexeme is "frequent" once `mm_frequent_lexemes` estimates
# it covers at least this many rows of `chunks`...
_FREQUENT_LEXEME_MIN_ROWS = 2_000.0
# ...and the candidate stage never considers more than this many chunks for
# ranking, frequent lexemes or not. Chosen against `tests/search/
# test_fulltext_bounded.py`'s 50k-chunk fixture: both constants comfortably
# clear a lone chunk's own unique marker (estimated count ~0) and stay well
# under the fixture size, while `_CANDIDATE_CAP` is a small enough multiple
# of the threshold that an "all lexemes frequent" query still only ranks a
# small sample, not a sixth of the vault.
_CANDIDATE_CAP = 10_000


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

    sql, args = _build_fulltext_query(
        query,
        include_archived=include_archived,
        limit=limit,
        types=types,
        tags=tags,
        namespaces=namespaces,
        valid_at=valid_at,
        use_selective=True,
    )
    rows = await conn_or_pool.fetch(sql, *args)
    if not rows:
        # Selective filtering can legitimately starve the candidate stage
        # when every lexeme of every branch turned out to be frequent (#117)
        # - retry once, unfiltered but still capped, so that case still
        # returns the frequent term's own hits instead of an empty list.
        # Harmless when the query simply matches nothing at all: that
        # attempt is bounded by the same cap and was already fast.
        sql, args = _build_fulltext_query(
            query,
            include_archived=include_archived,
            limit=limit,
            types=types,
            tags=tags,
            namespaces=namespaces,
            valid_at=valid_at,
            use_selective=False,
        )
        rows = await conn_or_pool.fetch(sql, *args)
    return [_row_to_chunk_hit(row) for row in rows]


def _build_fulltext_query(
    query: str,
    *,
    include_archived: bool,
    limit: int,
    types: Sequence[str] | None,
    tags: Sequence[str] | None,
    namespaces: Sequence[str] | None,
    valid_at: date | None,
    use_selective: bool,
) -> tuple[str, list[object]]:
    """Build `_FULLTEXT_SQL`'s text and bound parameters for one attempt.

    A private seam so a test can `EXPLAIN` exactly what `fulltext_search`
    runs, without duplicating the parameter order in two places.
    `use_selective=False` is `fulltext_search`'s retry: it keeps every
    other parameter identical and only flips `$10`, so the candidate stage
    falls back to each branch's unfiltered match condition (still capped
    by `_CANDIDATE_CAP`) instead of a selective one.
    """
    return _FULLTEXT_SQL, [
        query,
        include_archived,
        limit,
        list(types) if types else None,
        list(tags) if tags else [],
        list(namespaces) if namespaces else None,
        valid_at,
        _FREQUENT_LEXEME_MIN_ROWS,
        _CANDIDATE_CAP,
        use_selective,
    ]


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

    Git mode (today's flat `chunks`): one query, returned as-is. Postgres
    mode with the ADR-0016 layout (module docstring, "ADR-0016's per-kind
    vector search"): one query per namespace kind, fused into a single
    best-first ranking with `rrf_fuse` - the same fusion `_hybrid_search_impl`
    uses, reused here so a direct caller still gets one list back.
    """
    legs = await _vector_search_legs(
        conn_or_pool,
        embedding,
        model,
        dimension,
        limit=limit,
        include_archived=include_archived,
        types=types,
        tags=tags,
        namespaces=namespaces,
        valid_at=valid_at,
    )
    if len(legs) <= 1:
        return legs[0][:limit] if legs else []

    rank_lists = [[hit.chunk_id for hit in leg] for leg in legs]
    hits_by_chunk = {hit.chunk_id: hit for leg in legs for hit in leg}
    fused = rrf_fuse(rank_lists, k=_VECTOR_KIND_FUSE_K)
    ordered_ids = sorted(fused, key=lambda chunk_id: (-fused[chunk_id], chunk_id))
    return [hits_by_chunk[chunk_id] for chunk_id in ordered_ids[:limit]]


async def _vector_search_legs(
    conn_or_pool: _Queryable,
    embedding: Sequence[float],
    model: str,
    dimension: int,
    *,
    limit: int,
    include_archived: bool,
    types: Sequence[str] | None,
    tags: Sequence[str] | None,
    namespaces: Sequence[str] | None,
    valid_at: date | None,
) -> list[list[ChunkHit]]:
    """One best-first `ChunkHit` list per vector query `vector_search`/
    `_hybrid_search_impl` issues (module docstring).

    Git mode: a single-element list wrapping `_VECTOR_SQL_TEMPLATE`'s one
    query, unchanged by ADR-0016. Postgres mode with the ADR-0016 layout:
    one list per namespace kind in `_VECTOR_KINDS`, each from its own
    query under that kind's `_HNSW_KIND_SETTINGS` - a kind with no match at
    all (RLS, or the caller's own `namespaces` filter, hides every row of
    it) contributes no list rather than an empty one, so it never counts as
    a "leg" for the RRF fusion above/in `_hybrid_search_impl`.
    """
    if not await _uses_vector_layout(conn_or_pool):
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
        return [[_row_to_chunk_hit(row) for row in rows]]

    legs: list[list[ChunkHit]] = []
    for kind in _VECTOR_KINDS:
        rows = await _fetch_vector_kind_rows(
            conn_or_pool,
            kind,
            embedding,
            model,
            dimension,
            limit=limit,
            include_archived=include_archived,
            types=types,
            tags=tags,
            namespaces=namespaces,
            valid_at=valid_at,
        )
        if rows:
            legs.append([_row_to_chunk_hit(row) for row in rows])
    return legs


async def _fetch_vector_kind_rows(
    conn_or_pool: _Queryable,
    kind: str,
    embedding: Sequence[float],
    model: str,
    dimension: int,
    *,
    limit: int,
    include_archived: bool,
    types: Sequence[str] | None,
    tags: Sequence[str] | None,
    namespaces: Sequence[str] | None,
    valid_at: date | None,
) -> list[asyncpg.Record]:
    """Run `_VECTOR_SQL_KIND_TEMPLATE` for one namespace `kind`, under that
    kind's own `_HNSW_KIND_SETTINGS` (module docstring).

    Opens its own transaction around the `SET LOCAL`s and the query - a
    nested one (asyncpg's own savepoint nesting) when `conn_or_pool` is
    already inside a transaction, so this kind's settings never reach a
    statement outside this function, in either direction. A plain
    `asyncpg.Pool` has no `transaction()` of its own, so it acquires one
    connection first (`_search_connection`'s own distinction, reused here).
    """
    sql = _VECTOR_SQL_KIND_TEMPLATE.format(dim=dimension)
    args = (
        _vector_literal(embedding),
        kind,
        model,
        dimension,
        include_archived,
        list(types) if types else None,
        list(tags) if tags else [],
        list(namespaces) if namespaces else None,
        valid_at,
        limit,
    )
    settings = _HNSW_KIND_SETTINGS[kind]

    if isinstance(conn_or_pool, asyncpg.Pool):
        async with conn_or_pool.acquire() as conn:
            return await _fetch_vector_kind_rows_on_connection(conn, sql, args, settings)
    return await _fetch_vector_kind_rows_on_connection(conn_or_pool, sql, args, settings)


async def _fetch_vector_kind_rows_on_connection(
    conn: asyncpg.pool.PoolConnectionProxy | asyncpg.Connection,
    sql: str,
    args: tuple[object, ...],
    settings: Sequence[str],
) -> list[asyncpg.Record]:
    async with conn.transaction():
        for statement in settings:
            await conn.execute(statement)
        return await conn.fetch(sql, *args)


async def _uses_vector_layout(conn_or_pool: _Queryable) -> bool:
    """Whether `conn_or_pool` is talking to a database with the ADR-0016
    `chunks` layout (`migrations/postgres/0012_vector_layout.sql`).

    Detected from `schema_migrations`, the same check `index/indexer.py`'s
    `Indexer._uses_vector_layout` makes - kept as a plain module function
    here rather than cached on an instance, since `search.py`'s functions
    carry no state between calls the way `Indexer` does.
    """
    return bool(
        await conn_or_pool.fetchval(_SCHEMA_MIGRATION_EXISTS_SQL, POSTGRES_VECTOR_LAYOUT_VERSION)
    )


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


@asynccontextmanager
async def _search_connection(pool: _Queryable) -> AsyncIterator[_Queryable]:
    """Yield one connection with its `plan_cache_mode` forced to custom (#120).

    `pool` a real `asyncpg.Pool`: acquires one connection and opens a
    read-only transaction around every statement `_hybrid_search_impl`
    makes on it - nothing else holds a transaction on it yet, so this is the
    one that forces the custom plan (module docstring, "Custom plans").

    `pool` already a connection: the RLS request path (#116) - `mcp/
    server.py`'s `memory_search` calls `hybrid_search` with the connection
    `rls.request_connection` yields, already inside the transaction `rls.
    request_identity` opened under the app role. Opening a second,
    independent transaction on it here is neither possible (asyncpg has no
    such thing on a connection that already has one open) nor needed:
    `SET LOCAL plan_cache_mode = force_custom_plan` is exactly as valid
    inside that transaction as inside one this function opens itself, and
    reverts with it the same way either way - so it is issued directly on
    the connection, with no transaction of this function's own around it.
    """
    if isinstance(pool, asyncpg.Pool):
        async with pool.acquire() as conn, conn.transaction(readonly=True):
            await conn.execute(_FORCE_CUSTOM_PLAN_SQL)
            yield conn
    else:
        await pool.execute(_FORCE_CUSTOM_PLAN_SQL)
        yield pool


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

    # Embed before touching the database (#120): the one connection this
    # function holds below is scarce (pool-sized, shared with every other
    # request), so a slow/failing embedding call should not hold it idle.
    embedding: list[float] | None = None
    embedding_model = ""
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
                embedding = embeddings[0]
                embedding_model = provider.model

    # One connection, forced onto a custom plan for every statement this
    # call makes (#120, see the module docstring: "Custom plans") - a bound
    # `websearch_to_tsquery` parameter otherwise gets a misestimating
    # generic plan from its sixth execution on a connection onward.
    # `_search_connection` tells a plain pool (opens its own connection and
    # read-only transaction) apart from a connection already inside the
    # RLS request path's own transaction (#116, reused as-is).
    async with _search_connection(pool) as conn:
        fulltext_hits = await fulltext_search(
            conn,
            query,
            limit=candidates,
            include_archived=filters.include_archived,
            types=filters.types,
            tags=filters.tags,
            namespaces=filters.namespaces,
            valid_at=filters.valid_at,
        )

        # One list per vector query (module docstring, "ADR-0016's per-kind
        # vector search"): Git mode gives a single-element list, Postgres
        # mode with the ADR-0016 layout gives one list per visible
        # namespace kind - either way, every list below becomes its own
        # RRF leg, fed into the same `rrf_fuse` call as the full-text side
        # (ADR-0016 Decision: "no new fusion step, only more vector legs").
        vector_legs: list[list[ChunkHit]] = []
        if embedding is not None:
            vector_legs = await _vector_search_legs(
                conn,
                embedding,
                embedding_model,
                len(embedding),
                limit=candidates,
                include_archived=filters.include_archived,
                types=filters.types,
                tags=filters.tags,
                namespaces=filters.namespaces,
                valid_at=filters.valid_at,
            )

        if not fulltext_hits and not vector_legs:
            return []

        chunks_by_id: dict[int, ChunkHit] = {}
        fulltext_chunk_ids: set[int] = set()
        rank_lists: list[list[int]] = []

        if fulltext_hits:
            rank_lists.append([hit.chunk_id for hit in fulltext_hits])
            for hit in fulltext_hits:
                chunks_by_id.setdefault(hit.chunk_id, hit)
                fulltext_chunk_ids.add(hit.chunk_id)
        for leg in vector_legs:
            rank_lists.append([hit.chunk_id for hit in leg])
            for hit in leg:
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

        note_rows = await _fetch_notes(conn, ordered_note_ids)

        results: list[NoteHit] = []
        for note_id in ordered_note_ids:
            row = note_rows.get(note_id)
            if row is None:
                # The note existed when the chunk was matched but is gone
                # now (e.g. deleted between the two queries); skip rather
                # than fail.
                continue

            score, chunk_id = note_best_chunk[note_id]
            best_chunk = chunks_by_id[chunk_id]
            if chunk_id in fulltext_chunk_ids:
                snippet = await _headline(conn, best_chunk.text, query)
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


async def _fetch_notes(
    conn_or_pool: _Queryable, note_ids: Sequence[str]
) -> dict[str, asyncpg.Record]:
    if not note_ids:
        return {}
    rows = await conn_or_pool.fetch(_NOTES_BY_ID_SQL, list(note_ids))
    return {row["id"]: row for row in rows}


async def _headline(conn_or_pool: _Queryable, text: str, query: str) -> str:
    result = await conn_or_pool.fetchval(_HEADLINE_SQL, text, query, _HEADLINE_OPTIONS)
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
