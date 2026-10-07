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
    primary key (note_id, revision)
);

create table namespaces (
    id bigint generated always as identity primary key,
    kind text not null check (kind in ('user', 'group', 'project', 'org')),
    external_key text not null,
    alias text unique,
    unique (kind, external_key)
);
