-- SPDX-License-Identifier: AGPL-3.0-only
-- Fix `mm_frequent_lexemes` (`migrations/0008_frequent_lexemes.sql`) for the
-- ADR-0016 partitioned `chunks` (`migrations/postgres/0012_vector_layout.
-- sql`, #220) - #293: a search whose vector legs find the right chunk still
-- lost it in the final ranking, because the full-text side's "is this
-- lexeme frequent" check read `pg_stats` for `tablename = 'chunks'` - the
-- partitioned *parent* - whose own stats autovacuum never maintains
-- (a partitioned table carries no rows of its own; nothing ever runs
-- `ANALYZE` against it unless something explicitly does, and this
-- project's own `loadtest.load.analyze_chunks`/`index.indexer`'s
-- post-reindex analyze both already only ever call plain `analyze chunks`,
-- which Postgres fans out to every partition but does not reliably turn
-- into a *matching* inherited `most_common_elems`/`most_common_elem_freqs`
-- for an array-typed column like a tsvector - confirmed directly against a
-- real load-test vault: the inherited row for `tsv_simple` carried a
-- handful of elements, not the vault's actual, much more frequent, shared
-- vocabulary). A lexeme the candidate stage should have bounded instead
-- read as "not frequent", so `_FULLTEXT_SQL`'s selective stage ran it as an
-- ordinary, unbounded match term and returned real but semantically
-- irrelevant hits - which then outscored the correct vector match in RRF
-- fusion, since those hits picked up a second, independent list membership
-- (full-text *and* their own namespace kind's vector leg) the correct
-- match's single vector-leg membership could not match.
--
-- The fix: never trust the partitioned parent's own `pg_stats`/`reltuples`
-- at all - sum each lexeme's estimated row count straight from every leaf
-- partition's *own*, independently and reliably `ANALYZE`d statistics
-- (`pg_inherits` is what finds them, by name, regardless of how many
-- `chunks_<kind>` partitions exist today or after a future `ALTER TABLE
-- ... ATTACH PARTITION`, e.g. ADR-0013's `agent` kind). A partition with no
-- stats yet (no row in `pg_stats` for it) or a negative `reltuples` (never
-- analyzed) contributes nothing, exactly like the original function's own
-- "nothing known -> not frequent" rule for the single flat table.
--
-- Postgres-mode-only (this subdirectory's own convention): by the time
-- this migration ever runs, `migrations/postgres/0012_vector_layout.sql`
-- has already run first (lower version number, same merged-by-filename
-- order `db/migrate.py` applies) and unconditionally made `chunks`
-- partitioned - there is no "postgres backend, not yet partitioned" state
-- for this file to handle. `backend="git"`'s own flat `chunks`
-- (`migrations/0001_index_schema.sql`) never loads this file at all
-- (`db/migrate.py`'s module docstring: a Postgres-mode-only migration
-- "simply never runs against a Git-backend database") and keeps using
-- `0008_frequent_lexemes.sql`'s original, still-correct-for-a-flat-table
-- definition, completely untouched by this one.
--
-- Same signature, `security definer` and pinned `search_path` as the
-- function this replaces (`0008_frequent_lexemes.sql`'s own comment
-- explains both: `pg_stats` hides a table's rows from a role that cannot
-- read it, and the pinned `search_path` is standard `security definer`
-- hardening) - `create or replace` only changes the body, not any grant
-- already made on this function name/signature, so the default `public`
-- `execute` grant `0008` relies on still applies and no new grant is
-- needed here.
create or replace function mm_frequent_lexemes(
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
    result text[] := '{}';
    lex text;
    total double precision;
    part record;
    part_elems text[];
    part_freqs real[];
    pos int;
begin
    -- `attname` shapes the `pg_stats` lookup below, so it is validated
    -- against a fixed allowlist - the two tsvector columns `chunks`
    -- actually has (`0001_index_schema.sql`) - rather than trusted.
    -- `search.py` is the only caller.
    if attname not in ('tsv_simple', 'tsv_lang') then
        raise exception 'mm_frequent_lexemes: unknown attname %', attname;
    end if;

    foreach lex in array lexemes loop
        total := 0;

        -- Every leaf partition `chunks` currently has, found by name via
        -- `pg_inherits` rather than hard-coded - additive future partitions
        -- (ADR-0013's `agent` kind) need no change here.
        for part in
            select c.relname as tablename, c.reltuples as reltuples
            from pg_inherits i
            join pg_class parent on parent.oid = i.inhparent
            join pg_namespace pn on pn.oid = parent.relnamespace
            join pg_class c on c.oid = i.inhrelid
            where pn.nspname = _schema and parent.relname = 'chunks'
        loop
            -- Never analyzed (or just truncated): this partition's own row
            -- count is unknowable, so it contributes nothing rather than a
            -- guess - same rule the original function applied to the flat
            -- table's own `reltuples`.
            if part.reltuples is null or part.reltuples < 0 then
                continue;
            end if;

            -- `$1`, not the parameter name `attname`: `pg_stats` has its
            -- own `attname` column, and a bare reference inside this SQL
            -- statement would be ambiguous between the two (the same
            -- reason the function this replaces used `$1` here).
            select s.most_common_elems, s.most_common_elem_freqs
            into part_elems, part_freqs
            from pg_stats s
            where s.schemaname = _schema
                and s.tablename = part.tablename
                and s.attname = $1;

            -- No stats yet for this partition/attname - nothing known,
            -- contributes nothing (not every partition need be analyzed
            -- for the ones that are to still count).
            if part_elems is null then
                continue;
            end if;

            -- `most_common_elem_freqs` carries one or two trailing entries
            -- past `most_common_elems`' own last element (the average
            -- distinct-element count per row, the null-fraction) - looking
            -- `lex`'s position up in `part_elems` and indexing `part_freqs`
            -- at that same position, rather than ever reading past
            -- `array_length(part_elems, 1)`, is what keeps those trailing
            -- entries from ever being misread as a frequency.
            pos := array_position(part_elems, lex);
            if pos is not null then
                total := total + part_freqs[pos] * part.reltuples;
            end if;
        end loop;

        if total >= min_rows then
            result := array_append(result, lex);
        end if;
    end loop;

    return result;
end;
$$;
