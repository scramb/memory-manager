-- SPDX-License-Identifier: AGPL-3.0-only
--
-- Measurement only, never a migration (#109, WP-21): not referenced by
-- `db.migrate`, applied manually - or by `scripts/loadtest-smoke.sh` under
-- `LOADTEST_RLS_VARIANT=r1` - against the throwaway `mm_loadtest` database
-- only, after `loadtest.load` has filled `users`/`namespaces`/`user_groups`
-- (`populate_registry`) and before the server starts.
--
-- Owner decision 2026-10-07 (#109): R1 (ADR-0008 "the app passes the
-- computed namespace set, the policy only checks membership in it") is
-- approximated at the DB level rather than reimplemented on the
-- application side. The RLS policies (`migrations/0005_rls.sql`) and the
-- request path (`db.rls.request_identity`) stay byte-for-byte identical
-- between variants - only `mm_readable_ns()`/`mm_writable_ns()`'s own
-- derivation cost changes, from "resolve four membership tables per call"
-- (R2, as deployed) to "look a precomputed row up by oid" (what receiving
-- an already-computed set from the app would cost once it reaches the
-- policy). `mm_ensure_personal_ns()` and `mm_principal_namespaces()` are
-- untouched: #109's own scope is `select`/`insert`/`update`/`delete`
-- latency on content tables, not namespace resolution on the write path.
--
-- Step 1: for every registered user, run R2's own functions once - the
-- only place that still resolves the matrix from membership tables - and
-- freeze the result into `mm_r1_lookup`. The table carries no RLS and no
-- grants, the same reasoning as `0005_rls.sql`'s own membership tables: it
-- is read only by the two `SECURITY DEFINER` functions below, which run
-- with the owner's privileges regardless of the caller.
create table mm_r1_lookup (
    oid text primary key,
    readable_ns text[] not null,
    writable_ns text[] not null
);

-- `set_config(..., true)` is transaction-local, the same semantics
-- `db.rls.request_identity` uses - this whole block runs inside the one
-- implicit transaction a `DO` block opens, so each iteration's setting is
-- visible to `mm_readable_ns()`/`mm_writable_ns()` immediately and is
-- overwritten, not accumulated, by the next iteration. Every synthetic
-- principal `loadtest.load` creates carries exactly one role,
-- `Memory.User` (`loadtest.load`'s own `_TOKEN_ROLES`) - matched here so
-- the frozen set is what that same principal would resolve to live.
do $$
declare
    v_oid text;
begin
    for v_oid in select oid from users order by oid loop
        perform set_config('app.oid', v_oid, true);
        perform set_config('app.roles', 'Memory.User', true);
        insert into mm_r1_lookup (oid, readable_ns, writable_ns)
        values (v_oid, mm_readable_ns(), mm_writable_ns());
    end loop;
    perform set_config('app.oid', '', true);
    perform set_config('app.roles', '', true);
end;
$$;

-- Step 2: replace the two functions with a lookup against the frozen
-- table. `create or replace` keeps the function's existing owner and
-- grants (`db.rls.grant_app_role`'s `EXECUTE` grant to the app role is
-- unaffected, whether it was already made or is still to come), but resets
-- every attribute this statement does not repeat - so `stable`, `security
-- definer` and the pinned `search_path` are spelled out again here,
-- identically to `migrations/0005_rls.sql`'s own originals, even though
-- nothing about them changes. An oid with no row in `mm_r1_lookup` (never
-- set, or `app.oid` unset) resolves to `'{}'::text[]`, the same empty-set
-- behaviour R2's own functions give an unknown or missing identity.
create or replace function mm_readable_ns() returns text[]
    language sql
    stable
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
    select coalesce(
        (select readable_ns from mm_r1_lookup
         where oid = coalesce(nullif(current_setting('app.oid', true), ''), '')),
        '{}'::text[]
    )
$$;

create or replace function mm_writable_ns() returns text[]
    language sql
    stable
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
    select coalesce(
        (select writable_ns from mm_r1_lookup
         where oid = coalesce(nullif(current_setting('app.oid', true), ''), '')),
        '{}'::text[]
    )
$$;
