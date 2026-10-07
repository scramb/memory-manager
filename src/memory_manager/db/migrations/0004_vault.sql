-- SPDX-License-Identifier: AGPL-3.0-only
-- Source of truth for the Postgres `StorageBackend` (ADR-0007 §2, enterprise
-- mode): `vault_notes` holds the current row per note, `vault_revisions` an
-- append-only history of every change. With the Git backend these tables
-- stay empty; Git itself is the source of truth there, as it always was.
--
-- `namespaces` is declared here (ADR-0008 A2) but stays empty until WP-19
-- fills it from Entra groups/projects; no extension, RLS or trigger is
-- added yet - those land with the namespace/permissions work.

create table vault_notes (
    id text primary key, -- ULID from the note's frontmatter
    namespace text not null, -- first path segment
    path text not null unique,
    content bytea not null,
    version text not null, -- sha256 of the canonical bytes (vault.note.version)
    current_revision int not null check (current_revision > 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table vault_revisions (
    note_id text not null references vault_notes (id) on delete cascade,
    revision int not null,
    path text not null, -- the path this revision was written at
    content bytea not null,
    version text not null,
    author text not null,
    client text not null,
    message text,
    created_at timestamptz not null default now(),
    -- The committing transaction's id, used as `changes_since`'s cursor
    -- (ADR-0007 §2): a new cursor is `pg_snapshot_xmin(pg_current_snapshot())`
    -- taken in a `REPEATABLE READ` transaction, and the next call's window is
    -- every revision with `xid >= <previous cursor>`. Ordering by `xid`
    -- rather than `created_at`/`revision` is what makes a transaction that
    -- commits late, with a lower `xid` than one that raced ahead and
    -- committed first, still show up instead of being skipped by a cursor
    -- that already moved past it.
    xid xid8 not null default pg_current_xact_id(),
    primary key (note_id, revision)
);

create index vault_revisions_xid_idx on vault_revisions (xid);

create table namespaces (
    id bigint generated always as identity primary key,
    kind text not null check (kind in ('user', 'group', 'project', 'org')),
    external_key text not null,
    alias text unique,
    unique (kind, external_key)
);

-- Indexes for the Postgres-backend write path, see #98.
--
-- `index_on_connection` runs `indexer.py`'s `_upsert_note_rows`,
-- `_refresh_links` and `_heal_dangling_for_note` (via `_entries_for`) inside
-- the same transaction as a write, so every `notes`/`links` query on that
-- path must stay index-backed - never a sequential scan over either table -
-- for a write to stay cheap regardless of vault size. `notes.path` (unique),
-- `notes.id` (primary key) and `links`' own primary key `(source_id,
-- target_raw)` already cover the delete-by-path, upsert-by-id and
-- delete-by-source_id statements in those functions; only the link
-- resolution lookups below need new indexes.

-- `_entries_for`'s `lower(slug) = any($1)` branch.
create index notes_slug_lower_idx on notes (lower(slug));

-- `_entries_for`'s alias branch. `lower_array` folds every alias to lower
-- case once, so a GIN index on the result can be probed with `&&` instead
-- of the per-row `unnest`/`exists` the planner could never push through an
-- index. Semantically identical to "does any alias, lower-cased, appear in
-- the target list"; `strict` is safe because `notes.aliases` is `not null
-- default '{}'`, so it is never actually passed a `null`.
create or replace function lower_array(text[]) returns text[]
    language sql immutable strict as
$$
    select array_agg(lower(element)) from unnest($1) as element
$$;

create index notes_aliases_lower_gin_idx on notes using gin (lower_array(aliases));

-- `_heal_dangling_for_note`'s `target_path is null and
-- lower(trim(target_raw)) = any($1)`. Partial on `target_path is null` to
-- match the predicate exactly and stay small as links resolve over time.
create index links_dangling_target_raw_idx on links (lower(trim(target_raw)))
    where target_path is null;
