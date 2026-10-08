-- SPDX-License-Identifier: AGPL-3.0-only
-- Erasure (ADR-0007 §3 + addendum 2026-10-08, #231): `erasure_log` records
-- every hard-delete `storage/erasure.py` performs - IDs, actor, reason and
-- row counts only, never content (CLAUDE.md: "audited with metadata only").
--
-- Postgres-mode-only (this subdirectory's own convention, `migrations/
-- postgres/0012_vector_layout.sql`, ADR-0016/#220; `db/migrate.py`'s module
-- docstring): erasure itself only ever exists for `backend="postgres"`
-- (CLAUDE.md "erasure exists only with the postgres backend") -
-- `storage.git.GitBackend.erase` raises `ErasureUnsupported` before any SQL
-- runs, so a `backend="git"` database never needs this table at all.
--
-- No RLS, no grant to the app role - same reasoning as `0011_jobs.sql`'s
-- own "no RLS" for `jobs`: `storage/erasure.py` runs exclusively as the
-- owner (ADR-0008 addendum #100, "system identity"), and `db/rls.py`'s
-- `grant_app_role` never mentions this table at all, so the app role has no
-- privilege on it whatsoever, not even `SELECT`.
create table erasure_log (
    id bigint generated always as identity primary key,
    at timestamptz not null default now(),
    actor text not null,
    reason text not null,
    target_kind text not null check (target_kind in ('note', 'namespace', 'user')),
    target_ids text[] not null,
    row_counts jsonb not null default '{}'::jsonb
);
