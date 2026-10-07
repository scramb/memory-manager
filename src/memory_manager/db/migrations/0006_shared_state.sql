-- SPDX-License-Identifier: AGPL-3.0-only
-- Shared state across replicas (ADR-0009 §2, #103): rate-limit windows and
-- pending OIDC login state, both loss-tolerant - losing either on crash or
-- restart only resets counters or aborts logins already in progress, never
-- data this project has to keep (CLAUDE.md: Postgres must stay rebuildable
-- from the vault; neither table holds anything derived from it).
--
-- `rate_limits` is one row per key, upserted on every hit by
-- `auth.shared_state.PostgresSharedState.window_hit` - UNLOGGED because a
-- fixed-window counter is exactly the kind of state this project never
-- needs WAL-durable.
--
-- `oauth_pending` gains `kind`: `'authorize'` for every row `auth.store.
-- save_pending`/`get_pending` already manage (a real `/authorize` call,
-- always carrying a `client_id`), anything else for `auth.shared_state`'s
-- own pending login state (`PostgresSharedState.put_pending`/`take_pending` -
-- no `client_id` of its own to park, the login flow it stands in for -
-- OIDC's `state` round trip - has not reached a client-bound authorization
-- yet). `get_pending` is updated in the same change (`auth/store.py`) to
-- filter on `kind = 'authorize'`, so a `SharedState` pending row can never be
-- mistaken for one of its own, even if both happened to share a table.

create unlogged table rate_limits (
    key text primary key,
    window_start timestamptz not null,
    count integer not null
);

alter table oauth_pending add column kind text not null default 'authorize';
alter table oauth_pending alter column client_id drop not null;
alter table oauth_pending add constraint oauth_pending_authorize_requires_client
    check (kind <> 'authorize' or client_id is not null);
