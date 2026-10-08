-- SPDX-License-Identifier: AGPL-3.0-only
-- Jobs outbox (ADR-0007 §2/§4, #218): a write transaction enqueues work here
-- in the same transaction as its content writes (`jobs.py`'s `enqueue`),
-- and exactly one worker claims each row with `FOR UPDATE SKIP LOCKED`
-- (`jobs.py`'s `claim`). Migration number 0011, not 0010 - reserved for
-- WP-22's login work landing on `main` first (WP-23 owns 0011-0012).
--
-- `state`: 'pending' (claimable once `run_after` has passed), 'running'
-- (claimed by a worker, `locked_at` set, `attempts` already incremented),
-- 'done', 'failed' (`attempts` exhausted, `jobs.fail`/`fail_or_retry`).
--
-- `payload` carries IDs only, never note content (CLAUDE.md: Postgres must
-- stay rebuildable from the vault; M9's erasure acceptance, WP-26: nothing
-- here needs scrubbing beyond the row itself) - enforced in `jobs.enqueue`
-- with a size check, not a column constraint here: a constraint would have
-- to understand every future job kind's own shape, a size check does not.
--
-- `traceparent`: nullable, filled by neither this migration nor `jobs.py`
-- today (WP-31, not #218) - carried on the row now so linking a worker
-- span back to the request that enqueued it needs no new migration later.
--
-- The app role gets `INSERT` only (`db/rls.py`'s `grant_app_role`): a
-- request transaction enqueues, nothing else - `claim`/`complete`/`fail`/
-- `fail_or_retry` all run as the owner, the same system identity the worker
-- already connects as for every other table it touches (`worker.py`'s own
-- module docstring). No RLS: `jobs` carries no namespace of its own to
-- scope by, and the app role cannot read a row back at all once inserted.
--
-- `id` is a client-generated ULID (`vault.ulid.new_ulid`, the same helper
-- `storage/postgres.py` uses for note ids), not `bigint generated always as
-- identity`: Postgres requires `SELECT` privilege to run `INSERT ...
-- RETURNING`, even just for the row a caller's own statement inserted - an
-- identity column would force either granting the app role `SELECT` after
-- all (the one privilege this table is meant to withhold) or `enqueue`
-- never learning the id it just created. A ULID needs neither.
create table jobs (
    id text primary key,
    kind text not null,
    payload jsonb not null default '{}'::jsonb,
    state text not null default 'pending'
        check (state in ('pending', 'running', 'done', 'failed')),
    attempts integer not null default 0,
    run_after timestamptz not null default now(),
    locked_at timestamptz null,
    last_error text null,
    created_at timestamptz not null default now(),
    traceparent text null
);

-- `jobs.claim`'s own query: claimable `pending` rows ordered by `run_after`.
-- Partial (only `pending` rows) so it never grows with `done`/`failed`
-- history, which is expected to dominate the table over time.
create index jobs_claimable_idx on jobs (run_after) where state = 'pending';

-- Reclaiming a stale `running` row (a crashed worker, `jobs.claim`'s own
-- `stale_after` branch) scans by `locked_at` instead - a second partial
-- index, since `running` rows are rare and short-lived in the healthy case.
create index jobs_stale_running_idx on jobs (locked_at) where state = 'running';
