-- SPDX-License-Identifier: AGPL-3.0-only
-- Break-glass workflow (ADR-0008 "Break-glass", addendum 2026-10-08; #237):
-- request/approve/deny/revoke on the `break_glass_grants` table
-- `0005_rls.sql` already declares (that migration's own comment: "workflow
-- - request, approval, audit, notification - is WP-26"). `mm_readable_ns()`
-- already honours an approved, unexpired, non-revoked grant whose
-- requester is the calling identity (`0005_rls.sql`'s own `break_glass`
-- CTE) - this migration is the only way such a row is ever written, read
-- in aggregate, or ended early.
--
-- Same shape as `0020_admin_namespaces.sql`: every function re-checks
-- `'Memory.Admin' = any(app.roles)` itself (`errcode = '42501'`, CLAUDE.md
-- "enforced twice, in Python and in SQL") before touching a row -
-- `account/break_glass.py`'s own Python-side role check is the first,
-- independent layer. Every parameter is `p_`-prefixed, same reasoning
-- `0020_admin_namespaces.sql`'s own module comment gives.
--
-- The four-eyes rule (`BREAK_GLASS_APPROVERS`,
-- `config.break_glass_approvers_from_env`) is checked twice too:
-- `account/break_glass.py` refuses a same-admin approval in Python before
-- ever calling `mm_break_glass_approve`, and that function takes the
-- configured count as `p_approver_count` and refuses it again,
-- independently - the same "two independent computations" shape every
-- other ADR-0008 permission check in this codebase already follows. `1`
-- means self-approval is allowed (ADR-0008: "operators may lower it to
-- 1"); any value other than `1`/`2` raises - `account/break_glass.py`
-- never passes one outside `{1, 2}` (`break_glass_approvers_from_env`
-- already refuses it at startup), so this is defense in depth, not a path
-- any caller is expected to hit.
--
-- `expires_at` is `not null` with no default (`0005_rls.sql`): a pending
-- (not yet approved) request is simply never readable regardless of its
-- own `expires_at` value, since `mm_readable_ns()`'s own `break_glass` CTE
-- additionally requires `bg.approved` - so `mm_break_glass_request` below
-- sets it to `now()` (already "expired", a harmless placeholder) and
-- `mm_break_glass_approve` is the only place that ever sets the real
-- value, exactly `now() + 1 hour` (ADR-0008: "the grant expires after 1
-- h", counted from approval, never from the request).

-- `mm_break_glass_request(target_oid, reason)`: the only way a
-- `break_glass_grants` row is ever created. Resolves the target's
-- *personal* namespace by Entra `oid` (`namespaces.kind = 'user'`) - an
-- admin names the person, never a namespace alias directly, the same
-- "target user" field `account/admin.py`'s own "revoke user access" form
-- already uses. A target with no personal namespace at all (never used
-- memory-manager, or deprovisioned and already erased) refuses with
-- `P0002`, before a row is ever inserted.
create function mm_break_glass_request(p_target_oid text, p_reason text)
    returns bigint
    language plpgsql
    volatile
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_oid text := coalesce(nullif(current_setting('app.oid', true), ''), '');
    v_namespace_id bigint;
    v_grant_id bigint;
begin
    if not (
        'Memory.Admin' = any(
            string_to_array(coalesce(nullif(current_setting('app.roles', true), ''), ''), ',')
        )
    ) then
        raise exception 'mm_break_glass_request: caller lacks Memory.Admin'
            using errcode = '42501';
    end if;

    if p_reason is null or p_reason = '' then
        raise exception 'mm_break_glass_request: reason must not be empty'
            using errcode = '22023';
    end if;

    select id into v_namespace_id
    from public.namespaces
    where kind = 'user' and external_key = p_target_oid;
    if v_namespace_id is null then
        raise exception 'mm_break_glass_request: no personal namespace for user %', p_target_oid
            using errcode = 'P0002';
    end if;

    insert into public.break_glass_grants (namespace_id, requester, reason, expires_at)
    values (v_namespace_id, v_oid, p_reason, now())
    returning id into v_grant_id;

    return v_grant_id;
end;
$$;

revoke execute on function mm_break_glass_request(text, text) from public;

-- `mm_break_glass_approve(grant_id, approver_count)`: the only way
-- `approved`/`approved_by`/`approved_at`/`expires_at` are ever set. Refuses
-- a grant that does not exist, is already approved, or was already
-- denied/revoked (`P0002`/`22023`) - and, when `approver_count > 1`, a
-- caller equal to the grant's own `requester` (`42501`, the module
-- comment's "four-eyes rule").
create function mm_break_glass_approve(p_grant_id bigint, p_approver_count integer)
    returns void
    language plpgsql
    volatile
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_oid text := coalesce(nullif(current_setting('app.oid', true), ''), '');
    v_requester text;
    v_approved boolean;
    v_revoked_at timestamptz;
begin
    if not (
        'Memory.Admin' = any(
            string_to_array(coalesce(nullif(current_setting('app.roles', true), ''), ''), ',')
        )
    ) then
        raise exception 'mm_break_glass_approve: caller lacks Memory.Admin'
            using errcode = '42501';
    end if;

    if p_approver_count not in (1, 2) then
        raise exception
            'mm_break_glass_approve: approver_count must be 1 or 2, got %', p_approver_count
            using errcode = '22023';
    end if;

    select requester, approved, revoked_at into v_requester, v_approved, v_revoked_at
    from public.break_glass_grants
    where id = p_grant_id;
    if v_requester is null then
        raise exception 'mm_break_glass_approve: no grant with id %', p_grant_id
            using errcode = 'P0002';
    end if;
    if v_approved then
        raise exception 'mm_break_glass_approve: grant % is already approved', p_grant_id
            using errcode = '22023';
    end if;
    if v_revoked_at is not null then
        raise exception 'mm_break_glass_approve: grant % was already denied or revoked',
            p_grant_id
            using errcode = '22023';
    end if;
    if p_approver_count > 1 and v_oid = v_requester then
        raise exception
            'mm_break_glass_approve: a second admin (not the requester) must approve grant %',
            p_grant_id
            using errcode = '42501';
    end if;

    update public.break_glass_grants
    set approved = true,
        approved_by = v_oid,
        approved_at = now(),
        expires_at = now() + interval '1 hour'
    where id = p_grant_id;
end;
$$;

revoke execute on function mm_break_glass_approve(bigint, integer) from public;

-- `mm_break_glass_deny(grant_id)`: ends a request that was never approved
-- (`approved = false`) without ever granting read access - distinct from
-- `mm_break_glass_revoke` below, which also ends an *already-approved*
-- grant. Reuses `revoked_at` rather than a separate "denied" column:
-- `mm_readable_ns()`'s own `break_glass` CTE already requires both
-- `bg.approved` and `bg.revoked_at is null`, so a denied-but-never-approved
-- row is excluded exactly like a revoked one, with no new case for that
-- function to learn.
create function mm_break_glass_deny(p_grant_id bigint)
    returns void
    language plpgsql
    volatile
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
begin
    if not (
        'Memory.Admin' = any(
            string_to_array(coalesce(nullif(current_setting('app.roles', true), ''), ''), ',')
        )
    ) then
        raise exception 'mm_break_glass_deny: caller lacks Memory.Admin'
            using errcode = '42501';
    end if;

    update public.break_glass_grants
    set revoked_at = now()
    where id = p_grant_id and approved = false and revoked_at is null;

    if not found then
        raise exception 'mm_break_glass_deny: no pending grant with id %', p_grant_id
            using errcode = 'P0002';
    end if;
end;
$$;

revoke execute on function mm_break_glass_deny(bigint) from public;

-- `mm_break_glass_revoke(grant_id)`: ends a grant - pending or already
-- approved - at once. `mm_readable_ns()` re-evaluates `break_glass` on
-- every statement (it is an `InitPlan`, not cached across the
-- transaction), so a revoked grant stops being readable from the very next
-- statement onward, with nothing left to expire on its own.
create function mm_break_glass_revoke(p_grant_id bigint)
    returns void
    language plpgsql
    volatile
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
begin
    if not (
        'Memory.Admin' = any(
            string_to_array(coalesce(nullif(current_setting('app.roles', true), ''), ''), ',')
        )
    ) then
        raise exception 'mm_break_glass_revoke: caller lacks Memory.Admin'
            using errcode = '42501';
    end if;

    update public.break_glass_grants
    set revoked_at = now()
    where id = p_grant_id and revoked_at is null;

    if not found then
        raise exception 'mm_break_glass_revoke: no active grant with id %', p_grant_id
            using errcode = 'P0002';
    end if;
end;
$$;

revoke execute on function mm_break_glass_revoke(bigint) from public;

-- `mm_break_glass_list()`: every grant, newest first, plus the target
-- user's own `oid` and personal alias - what `account/break_glass.py`'s
-- own pending-list/history table renders. Counts/ids/metadata only, never
-- note content (CLAUDE.md "admins never see content, only ids/paths/
-- counts") - a grant lets an admin *read* a namespace through the
-- separate viewer #238 builds, never through this listing.
create function mm_break_glass_list()
    returns table (
        id bigint,
        alias text,
        target_oid text,
        requester text,
        reason text,
        requested_at timestamptz,
        approved boolean,
        approved_by text,
        approved_at timestamptz,
        expires_at timestamptz,
        revoked_at timestamptz
    )
    language sql
    stable
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
    select
        bg.id,
        n.alias,
        n.external_key,
        bg.requester,
        bg.reason,
        bg.requested_at,
        bg.approved,
        bg.approved_by,
        bg.approved_at,
        bg.expires_at,
        bg.revoked_at
    from public.break_glass_grants bg
    join public.namespaces n on n.id = bg.namespace_id
    where (
        'Memory.Admin' = any(
            string_to_array(coalesce(nullif(current_setting('app.roles', true), ''), ''), ',')
        )
    )
    order by bg.requested_at desc
$$;

revoke execute on function mm_break_glass_list() from public;
