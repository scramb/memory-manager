-- SPDX-License-Identifier: AGPL-3.0-only
-- Which of a given set of lexemes are so frequent in `chunks` that reading
-- their GIN posting list is itself the cost `search.py`'s candidate stage
-- needs to avoid (ADR-0007 addendum, #117): a term present in nearly every
-- chunk turns `ts_rank_cd` ranking into sorting the whole table, no matter
-- what the caller's own `limit` is.
--
-- Frequency is read from the planner's own statistics rather than counted
-- live: for a tsvector column, `pg_stats.most_common_elems`/
-- `most_common_elem_freqs` estimate, per distinct lexeme, the fraction of
-- rows containing it; multiplied by `pg_class.reltuples` (the planner's own
-- row-count estimate) that becomes an estimated row count per lexeme -
-- cheap, because both are already there once `ANALYZE` has run, with no
-- scan of `chunks` itself.
--
-- `most_common_elem_freqs` is *longer* than `most_common_elems`: Postgres
-- appends one or two extra entries after the per-element frequencies (the
-- average number of distinct elements per row, and the fraction of rows
-- with no elements at all) that are not paired with any element. Only the
-- first `array_length(most_common_elems, 1)` entries of
-- `most_common_elem_freqs` correspond to an element; this function stops
-- there instead of reading the trailing entries as if they were
-- frequencies too.
--
-- Under RLS (WP-19, #116) the app role will have no `select` on `chunks`,
-- and Postgres only exposes a table's `pg_stats` rows to a role that can
-- read the table - so the app role could not query `pg_stats` directly.
-- This function is `security definer`, owned by the schema owner, to read
-- `pg_stats` on the app role's behalf regardless. It discloses only
-- whether a given lexeme is very common across the whole `chunks` table -
-- nothing about rare terms or about any row's content - so its `execute`
-- privilege is deliberately left at the default Postgres grants to
-- `public` on function creation, rather than restricted to the roles #116
-- will grant table access to; that issue narrows it once that grant
-- exists.
--
-- `search_path` is pinned (`pg_catalog` first, so no earlier schema can
-- shadow a built-in; `public` for `chunks`' own schema; `pg_temp` last) -
-- standard hardening for a `security definer` function, so a caller
-- controlling `search_path` cannot make it resolve a different function
-- or operator than the one intended. Pinning it this way makes
-- `current_schema()` inside the function body report `pg_catalog`, not
-- the caller's actual schema - so the schema to look `chunks` up in is
-- captured by a trailing parameter whose default expression
-- (`current_schema()`) is evaluated at the call site, in the *caller's*
-- search_path, before this function's own `set search_path` ever takes
-- effect; every real call (`search.py`) passes only the first three
-- arguments and gets that default.
create function mm_frequent_lexemes(
    attname text,
    lexemes text[],
    min_rows real,
    _schema name default current_schema()
)
returns text[]
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    elems text[];
    freqs real[];
    estimated_rows real;
    elem_count int;
    result text[];
begin
    -- `attname` shapes the `pg_stats` lookup below, so it is validated
    -- against a fixed allowlist - the two tsvector columns `chunks`
    -- actually has (`0001_index_schema.sql`) - rather than trusted.
    -- `search.py` is the only caller.
    if attname not in ('tsv_simple', 'tsv_lang') then
        raise exception 'mm_frequent_lexemes: unknown attname %', attname;
    end if;

    select s.most_common_elems, s.most_common_elem_freqs
    into elems, freqs
    from pg_stats s
    where s.schemaname = _schema
        and s.tablename = 'chunks'
        and s.attname = $1;

    -- No stats yet (no `analyze chunks` has run) - nothing is known to be
    -- frequent; the candidate-stage cap in `search.py` is then the only
    -- thing bounding the work.
    if elems is null then
        return '{}';
    end if;

    select c.reltuples into estimated_rows
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = _schema and c.relname = 'chunks';

    -- A missing or negative `reltuples` (never analyzed, or a
    -- just-truncated table) makes "rows containing this lexeme"
    -- unknowable; treat that as "nothing is frequent" rather than guess.
    if estimated_rows is null or estimated_rows < 0 then
        return '{}';
    end if;

    elem_count := array_length(elems, 1);

    select coalesce(array_agg(elems[i]), '{}')
    into result
    from generate_series(1, elem_count) as i
    where elems[i] = any (lexemes)
        and freqs[i] * estimated_rows >= min_rows;

    return result;
end;
$$;
