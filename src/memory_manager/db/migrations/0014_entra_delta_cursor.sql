-- SPDX-License-Identifier: AGPL-3.0-only
-- Entra deprovisioning delta-sync cursor (ADR-0006 §6, #223): a singleton row
-- storing the Graph `users/delta` round's own `@odata.deltaLink` plus when the
-- worker's `entra_delta_sync` job last ran and last completed a round
-- successfully (`worker.py`'s own job, `auth.graph.GraphClient.users_delta`).
--
-- `id boolean primary key default true check (id)` is the same singleton-row
-- trick `postgres/0012_vector_layout.sql`'s own `embedding_dimension` already
-- uses - there is exactly one Entra tenant per deployment (ADR-0006 §1:
-- `ENTRA_ALLOWED_TENANTS` defaults to a single tenant), so one delta cursor is
-- enough; a second tenant would need a second deployment, not a second row
-- here.
--
-- No row at all before the first round ever ran: `delta_link is null` then
-- means "full sync" to `auth.graph.GraphClient.users_delta(None)`, exactly the
-- meaning that function's own `delta_link` parameter already gives a bare
-- `None` - no separate "never run" flag needed.
--
-- `last_run_at` is stamped at the start of every attempt, whether or not it
-- succeeds; `last_success_at` only once a round's every page was applied and
-- `delta_link` advanced (`worker.py`'s own "cursor advanced only after all
-- pages were applied") - the two can disagree, and that gap is the signal an
-- operator reads to notice a stuck sync.
--
-- Carries no RLS and no grants, like `0005_rls.sql`'s own `users`/
-- `user_groups`: only the worker, connected as the owner, ever touches this
-- table (`worker.py`'s own module docstring: "always connects to Postgres as
-- the owner").
create table entra_delta_cursor (
    id boolean primary key default true check (id),
    delta_link text,
    last_run_at timestamptz,
    last_success_at timestamptz
);
