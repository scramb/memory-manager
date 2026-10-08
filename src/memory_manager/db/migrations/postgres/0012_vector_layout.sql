-- SPDX-License-Identifier: AGPL-3.0-only
-- Postgres-mode-only: the ADR-0016 `chunks` layout (#220) - `halfvec(1024)`,
-- a B-tree on `namespace`, list-partitioned by `namespace_kind`. Applied
-- only when `db/migrate.py`'s `migrate(conn, backend="postgres")` is used
-- (`memory_manager.db.migrations.postgres/`, loaded and recorded in
-- `schema_migrations` exactly like every other migration, but skipped
-- entirely for `backend="git"`): the Git backend's own derived index keeps
-- today's flat `chunks` (migration 0001), `vector` column and
-- `index/indexer.py`'s runtime `ensure_vector_index`, completely untouched
-- by this file (ADR-0016 Consequences: "Git backend unchanged").
--
-- `chunks` cannot be partitioned in place (Postgres has no `ALTER TABLE
-- ... SET PARTITION BY`), so this drops and recreates it. Safe
-- unconditionally, including for a database that already has Git-mode-
-- shaped `chunks` rows (e.g. `memory-manager migrate git-to-postgres`
-- pointed at a `DATABASE_URL` that previously ran `STORAGE_BACKEND=git`
-- with a derived Postgres index, #220's own "handle a database that
-- already has Git-mode chunks rows"): nothing in `chunks` is itself a
-- source of truth (`vault_notes` is, ADR-0007 §2), and both
-- `Indexer.reindex_full` and the worker's own startup catch-up
-- (`enqueue_stale_embeddings`, #219) rebuild every row and re-embed it
-- from scratch. Carrying old rows over into the new, differently-typed
-- `embedding` column would need a reindex anyway (ADR-0016
-- Reversibility: "cheap while chunks is still empty ... a full reindex
-- once it is not") - dropping and relying on the reindex/catch-up path
-- already in place is the simpler, equally-correct choice.
drop table if exists chunks cascade;

create table chunks (
    id bigserial not null,
    note_id text not null references notes (id) on delete cascade,
    -- Denormalised from `notes.namespace` (ADR-0016 Consequences): lets the
    -- B-tree below resolve a namespace-filtered query without joining back
    -- to `notes`.
    namespace text not null,
    -- Denormalised from `namespaces.kind` (ADR-0016 Consequences): the
    -- partition key and the per-kind query router. The ADR's own prose
    -- calls this "personal" for one-owner namespaces; the stored value is
    -- `namespaces.kind`'s own `'user'` (0004_vault.sql's check
    -- constraint) - there is no separate "personal" kind value anywhere
    -- in this schema. `'agent'` (ADR-0013) attaches additively later
    -- (`alter table ... add constraint ...` plus one more `partition of`
    -- statement) - out of scope here (#220 "Not included").
    namespace_kind text not null check (namespace_kind in ('user', 'group', 'project', 'org')),
    ord int not null,
    heading_path text not null default '',
    text text not null,
    lang text, -- 'de' | 'en' | null
    tsv_simple tsvector generated always as (to_tsvector('simple', text)) stored,
    tsv_lang tsvector generated always as (
        case lang
            when 'de' then to_tsvector('german', text)
            when 'en' then to_tsvector('english', text)
            else to_tsvector('simple', text)
        end
    ) stored,
    -- `halfvec(1024)` (ADR-0016 decision, option B): the dimension is
    -- pinned once in `embedding_dimension` below (`db/migrate.py`,
    -- `backend="postgres"`'s own dimension-pin step) and immutable
    -- afterwards (PLAN O23) - `index/indexer.py` refuses a provider
    -- response of another dimension before it ever reaches this column.
    embedding halfvec(1024),
    model text,
    dimension int,
    -- A partitioned table's primary key/unique constraints must include
    -- the partition key (`namespace_kind`) - `id` (the `bigserial`,
    -- shared by one sequence across every partition) stays the value
    -- every other caller treats as this row's identity, just no longer
    -- unique all on its own at the constraint level.
    primary key (id, namespace_kind),
    unique (note_id, ord, namespace_kind)
) partition by list (namespace_kind);

create table chunks_user partition of chunks for values in ('user');
create table chunks_group partition of chunks for values in ('group');
create table chunks_project partition of chunks for values in ('project');
create table chunks_org partition of chunks for values in ('org');

-- A plain B-tree on `namespace` (ADR-0016 §1's own measured fix:
-- 4-15 ms, exact, vs. 71-372 ms for a sequential scan or a forced HNSW
-- path under `iterative_scan=relaxed_order`) - created on the partitioned
-- parent, which propagates a matching index to every partition above,
-- `chunks_user`'s own one-owner selectivity included.
create index chunks_namespace_idx on chunks (namespace);

-- Full-text: the same two indexes migration 0001 gave the flat table,
-- propagated to every partition the same way.
create index chunks_tsv_simple_gin_idx on chunks using gin (tsv_simple);
create index chunks_tsv_lang_gin_idx on chunks using gin (tsv_lang);

-- HNSW only for the multi-owner kinds (ADR-0016 §3): `chunks_user` stays
-- B-tree + exact sort, no HNSW at all - a one-owner namespace is never
-- worth approximating. `m=16`/`ef_construction=64` are ADR-0007 §5's
-- starting point, confirmed by the spike
-- (docs/research/vector-index.md §1/§3); `hnsw.ef_search`/
-- `hnsw.iterative_scan` are query-time settings, not stored in the index -
-- `search.py`'s own concern (#221, out of scope here).
create index chunks_group_embedding_hnsw_idx on chunks_group
    using hnsw (embedding halfvec_cosine_ops) with (m = 16, ef_construction = 64);
create index chunks_project_embedding_hnsw_idx on chunks_project
    using hnsw (embedding halfvec_cosine_ops) with (m = 16, ef_construction = 64);
create index chunks_org_embedding_hnsw_idx on chunks_org
    using hnsw (embedding halfvec_cosine_ops) with (m = 16, ef_construction = 64);

-- Row-level security, recreated for the new `chunks` (dropped along with
-- the old table above) and propagated to every partition - the same four
-- policies `0005_rls.sql` gave the flat table, unchanged.
alter table chunks enable row level security;
alter table chunks force row level security;

create policy chunks_owner_access on chunks
    to current_user
    using (true)
    with check (true);
comment on policy chunks_owner_access on chunks is
    'System identity (the role that ran this migration) per ADR-0008 addendum 2026-10-07 (#100): Git-mode indexing, reindex --full and other system jobs connect as the owner and bypass the namespace checks below entirely.';

create policy chunks_select on chunks
    for select to public
    using (exists (
        select 1 from notes n
        where n.id = chunks.note_id
          and n.namespace = any ((select mm_readable_ns())::text[])
    ));

create policy chunks_insert on chunks
    for insert to public
    with check (exists (
        select 1 from notes n
        where n.id = chunks.note_id
          and n.namespace = any ((select mm_writable_ns())::text[])
    ));

create policy chunks_update on chunks
    for update to public
    using (exists (
        select 1 from notes n
        where n.id = chunks.note_id
          and n.namespace = any ((select mm_writable_ns())::text[])
    ))
    with check (exists (
        select 1 from notes n
        where n.id = chunks.note_id
          and n.namespace = any ((select mm_writable_ns())::text[])
    ));

create policy chunks_delete on chunks
    for delete to public
    using (exists (
        select 1 from notes n
        where n.id = chunks.note_id
          and n.namespace = any ((select mm_writable_ns())::text[])
    ));

-- `mm_namespace_kind`: resolves a namespace alias' `namespaces.kind` for
-- `index/indexer.py`'s connection-path chunk insert (`index_on_connection`)
-- - the one `chunks`-writing path that can run under the app role
-- (request-serving writes, ADR-0008 addendum) rather than the owner, which
-- holds no direct `select` on `namespaces` (0005_rls.sql's own grant
-- boundary - the registry is only ever read through a `security definer`
-- function). Same shape as `mm_readable_ns`/`mm_writable_ns`
-- (0005_rls.sql): `stable security definer` with a pinned `search_path`
-- and a schema-qualified table, `execute` revoked from `public` and
-- granted to the app role alone (`db/rls.py`'s `grant_app_role`). Returns
-- `null` for an alias with no registry row - `index/indexer.py` falls
-- back to `'org'` rather than failing a write over it.
create function mm_namespace_kind(p_alias text) returns text
    language sql
    stable
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
    select kind from public.namespaces where alias = p_alias
$$;

revoke execute on function mm_namespace_kind(text) from public;

-- Dimension pin (#220, PLAN O23): a singleton row `db/migrate.py`'s
-- `migrate(conn, backend="postgres", embedding_dimensions=...)` writes
-- once, inside the same migration-lock transaction every other migration
-- runs in - immutable afterwards. `index/indexer.py` checks every
-- provider response against it before writing `chunks.embedding`, and
-- refuses a mismatch with a message naming the reindex-into-a-new-
-- column-or-table path (ADR-0016; this table has no second row or column
-- for an alternate dimension itself - that is the reindex's job, not
-- this one's).
create table embedding_dimension (
    id boolean primary key default true check (id),
    dimension int not null check (dimension > 0)
);
