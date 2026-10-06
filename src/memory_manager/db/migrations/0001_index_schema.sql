-- SPDX-License-Identifier: AGPL-3.0-only
-- Index schema: notes, chunks, links and the write/audit log.
--
-- Everything here except `audit_log` is derivable from the vault (Git) and
-- gets rebuilt by `reindex --full`. `audit_log` is operational data that
-- only ever lives in Postgres.

create extension if not exists vector;

create table notes (
    id text primary key, -- ULID
    path text not null unique,
    namespace text not null,
    type text not null,
    slug text not null,
    title text not null,
    description text not null,
    tags text[] not null default '{}',
    aliases text[] not null default '{}',
    created timestamptz not null,
    updated timestamptz not null,
    valid_from date,
    valid_to date,
    supersedes text[] not null default '{}',
    source text,
    archived boolean not null default false,
    file_hash text not null,
    indexed_at timestamptz not null default now()
);

create index notes_namespace_type_idx on notes (namespace, type);
create index notes_tags_gin_idx on notes using gin (tags);
create index notes_valid_range_idx on notes (valid_from, valid_to);

create table chunks (
    id bigserial primary key,
    note_id text not null references notes (id) on delete cascade,
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
    embedding vector, -- dimension fixed per model; HNSW index created at runtime (#27)
    model text,
    dimension int,
    unique (note_id, ord)
);

create index chunks_tsv_simple_gin_idx on chunks using gin (tsv_simple);
create index chunks_tsv_lang_gin_idx on chunks using gin (tsv_lang);

create table links (
    source_id text not null references notes (id) on delete cascade,
    target_path text,
    target_raw text not null,
    primary key (source_id, target_raw)
);

create index links_target_path_idx on links (target_path);

create table audit_log (
    id bigserial primary key,
    at timestamptz not null default now(),
    actor text not null,
    client text not null,
    op text not null,
    path text,
    commit_sha text,
    outcome text not null,
    detail jsonb not null default '{}'
);

create index audit_log_at_idx on audit_log (at);
