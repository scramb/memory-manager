-- SPDX-License-Identifier: AGPL-3.0-only
-- Break-glass notification (#239, ADR-0008 addendum 2026-10-08 "/account
-- session and break-glass notification"): the column and the two
-- `mm_break_glass_*` functions behind the `/account` banner every
-- *affected* user sees until they acknowledge it - distinct from
-- `mm_break_glass_list()` (`0021_break_glass_workflow.sql`), which only a
-- `Memory.Admin` may call and which lists every grant, not just the
-- caller's own.
--
-- Same shape as every other `mm_break_glass_*`/`mm_admin_*` function:
-- `SECURITY DEFINER` with a pinned `search_path`, `EXECUTE` revoked from
-- `PUBLIC` below and granted to the one app role by `db.rls.
-- grant_app_role` (its own `_FUNCTIONS` tuple lists both). Neither checks
-- `Memory.Admin` at all - the caller does not need it here, only to be the
-- person the grant is *about*, resolved from `app.oid` the same way
-- `mm_ensure_personal_ns()` already resolves the caller's own namespace.

alter table break_glass_grants add column acknowledged_at timestamptz;

-- `mm_break_glass_notices()`: every *approved*, not-yet-acknowledged grant
-- on the caller's own personal namespace - what `account.break_glass_
-- notice.render_break_glass_notice` shows as a banner. A pending (not yet
-- approved) request gave nobody read access yet, so it is never a notice
-- here (ADR-0008 addendum: "requester, approver, reason, time, expiry" - a
-- pending request has no approver yet). Still reported once already
-- revoked or expired: the banner is about the grant having happened, not
-- about access still being live - `account.break_glass`'s own admin-facing
-- `mm_break_glass_list()` is where "is it still active" is answered.
create function mm_break_glass_notices()
    returns table (
        id bigint,
        requester text,
        approved_by text,
        reason text,
        approved_at timestamptz,
        expires_at timestamptz,
        revoked_at timestamptz
    )
    language sql
    stable
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
    select bg.id, bg.requester, bg.approved_by, bg.reason, bg.approved_at,
           bg.expires_at, bg.revoked_at
    from public.break_glass_grants bg
    join public.namespaces n on n.id = bg.namespace_id
    where n.kind = 'user'
      and n.external_key = coalesce(nullif(current_setting('app.oid', true), ''), '')
      and bg.approved
      and bg.acknowledged_at is null
    order by bg.approved_at desc
$$;

revoke execute on function mm_break_glass_notices() from public;

-- `mm_break_glass_acknowledge(grant_id)`: the only way `acknowledged_at` is
-- ever set - always to the caller's own identity's own grant, never
-- anyone else's (the same `n.external_key = app.oid` ownership check
-- `mm_break_glass_notices()` applies, repeated here rather than shared
-- since both are single statements). `P0002` ("no_data_found", the same
-- SQLSTATE `mm_break_glass_deny`/`mm_break_glass_revoke` already raise for
-- "no matching row") covers a grant that does not exist, is not the
-- caller's own, is not approved, or was already acknowledged alike -
-- `account/break_glass_notice.py`'s own route maps it to a plain 404,
-- without needing to tell which of the four it was.
create function mm_break_glass_acknowledge(p_grant_id bigint)
    returns void
    language plpgsql
    volatile
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_oid text := coalesce(nullif(current_setting('app.oid', true), ''), '');
begin
    update public.break_glass_grants bg
    set acknowledged_at = now()
    where bg.id = p_grant_id
      and bg.approved
      and bg.acknowledged_at is null
      and exists (
          select 1 from public.namespaces n
          where n.id = bg.namespace_id and n.kind = 'user' and n.external_key = v_oid
      );

    if not found then
        raise exception 'mm_break_glass_acknowledge: no unacknowledged grant % for this user',
            p_grant_id
            using errcode = 'P0002';
    end if;
end;
$$;

revoke execute on function mm_break_glass_acknowledge(bigint) from public;
