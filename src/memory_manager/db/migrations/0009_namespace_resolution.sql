-- SPDX-License-Identifier: AGPL-3.0-only
-- Namespace resolution and revision authorship (ADR-0008 addendum
-- "identity sources and curate", #101/#115/#116; owner decision 2026-10-07,
-- #119): the personal-namespace lazy-creation function and the principal
-- resolver the application side (#101) needs, plus the column that makes
-- author-based curate possible.
--
-- Same shape as `0005_rls.sql`'s functions: `SECURITY DEFINER` with a
-- pinned `search_path` and schema-qualified tables, so the caller needs no
-- grants on `namespaces`/`users`/`project_members`/`namespace_settings` -
-- the app role only ever gets `EXECUTE` on the functions themselves
-- (`db/rls.py`'s `grant_app_role`), never table access on the registry.
-- `EXECUTE` is revoked from `PUBLIC` for both, same reasoning as 0005's.

-- `vault_revisions.author_oid`: the ADR-0008 addendum's "a note's author is
-- the `author_oid` of its revision 1" needs a column that cannot be forged
-- by the app role. The default resolves it from the transaction's own
-- identity, exactly like `mm_readable_ns`/`mm_writable_ns` already do; the
-- insert policy below additionally *enforces* that no other value can be
-- written under the app role, so a forged `author_oid` is rejected, not
-- merely defaulted away.
alter table vault_revisions add column author_oid text null default
    nullif(current_setting('app.oid', true), '');

drop policy vault_revisions_insert on vault_revisions;

create policy vault_revisions_insert on vault_revisions
    for insert to public
    with check (
        author_oid is not distinct from nullif(current_setting('app.oid', true), '')
        and exists (
            select 1 from vault_notes vn
            where vn.id = vault_revisions.note_id
              and vn.namespace = any ((select mm_writable_ns())::text[])
        )
    );
comment on policy vault_revisions_insert on vault_revisions is
    'Recreated by 0009_namespace_resolution.sql to also pin author_oid to the '
    'transaction''s own app.oid - the owner-only policy (unchanged, to current_user) '
    'is unaffected, so system jobs may still write an explicit or NULL author_oid.';

-- `mm_ensure_personal_ns()`: lazy personal-namespace creation (ADR-0008
-- addendum: "the app role gets no general INSERT on namespaces"). Only
-- ever acts for the calling transaction's own `app.oid` - never creates or
-- returns a namespace for anyone else, and never touches `users`.
--
-- `VOLATILE` (it writes) `SECURITY DEFINER`: the app role holds no insert
-- or update privilege on `namespaces` at all, this function runs with the
-- owner's privileges instead. Race-safe by construction: the `unique
-- (kind, external_key)` constraint from `0004_vault.sql` serializes two
-- concurrent inserts for the same `oid` (the second blocks on the
-- conflicting key until the first commits, then sees the row and inserts
-- nothing), and the alias backfill below only ever updates a row that
-- still has `alias is null`, so a second caller that loses that race
-- re-reads the alias the first one just set instead of overwriting it.
create function mm_ensure_personal_ns() returns text
    language plpgsql
    volatile
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_oid text := coalesce(nullif(current_setting('app.oid', true), ''), '');
    v_id bigint;
    v_alias text;
begin
    if v_oid = '' then
        return null;
    end if;

    select id, alias into v_id, v_alias
    from public.namespaces
    where kind = 'user' and external_key = v_oid;

    if v_id is null then
        insert into public.namespaces (kind, external_key)
        values ('user', v_oid)
        on conflict (kind, external_key) do nothing
        returning id into v_id;

        if v_id is null then
            select id, alias into v_id, v_alias
            from public.namespaces
            where kind = 'user' and external_key = v_oid;
        end if;
    end if;

    if v_alias is null then
        update public.namespaces
        set alias = 'u-' || v_id
        where id = v_id and alias is null
        returning alias into v_alias;

        if v_alias is null then
            select alias into v_alias from public.namespaces where id = v_id;
        end if;
    end if;

    return v_alias;
end;
$$;

revoke execute on function mm_ensure_personal_ns() from public;

-- `mm_principal_namespaces(groups)`: the per-namespace rows #101's
-- application-side resolver needs to build `me`/alias resolution and the
-- permission matrix from - the DB-side half of the "two independent
-- computations" ADR-0008's R2 decision asks for. `groups` is the caller's
-- own group membership claim (ADR-0008 addendum: "the app computes access
-- from token claims"; this function reads none of `user_groups` itself,
-- unlike `mm_readable_ns`/`mm_writable_ns`, which derive membership from
-- that table for the *independent* database-side computation).
--
-- Returns one row per namespace the calling identity has any standing in:
-- its own personal namespace (never another user's - the join is always
-- against the caller's own `app.oid`), one row per group in `groups` that
-- has a namespace, one row per project the caller or one of `groups` is a
-- member of (with the strongest of its own membership rows: 'owner' over
-- 'writer' over 'reader'), and the org namespace. `namespace_settings` is
-- left-joined in for the two columns the app side needs to resolve write
-- access the same way `mm_writable_ns()` does. A disabled user
-- (`users.disabled_at`) or an empty/missing `app.oid` resolves to zero
-- rows - `active` stays empty, and every other CTE below is cross-joined
-- against it.
--
-- `STABLE SECURITY DEFINER` with the pinned `search_path` (0005_rls.sql's
-- own comment explains why); `EXECUTE` is revoked from `PUBLIC` below.
create function mm_principal_namespaces(groups text[])
    returns table (
        kind text,
        alias text,
        role text,
        group_write text,
        project_write text
    )
    language sql
    stable
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
    with ctx as (
        select coalesce(nullif(current_setting('app.oid', true), ''), '') as oid
    ),
    identity as (
        select ctx.oid, u.disabled_at
        from ctx
        left join public.users u on u.oid = ctx.oid and ctx.oid <> ''
    ),
    active as (
        select oid from identity where oid <> '' and disabled_at is null
    ),
    personal as (
        select
            n.kind,
            n.alias,
            null::text as role,
            null::text as group_write,
            null::text as project_write
        from public.namespaces n
        join active a on n.kind = 'user' and n.external_key = a.oid
        where n.alias is not null
    ),
    group_ns as (
        select
            n.kind,
            n.alias,
            null::text as role,
            s.group_write,
            null::text as project_write
        from public.namespaces n
        join active a on true
        left join public.namespace_settings s on s.namespace_id = n.id
        where n.kind = 'group'
          and n.alias is not null
          and n.external_key = any (coalesce($1, '{}'::text[]))
    ),
    project_roles as (
        select
            pm.namespace_id,
            case
                when bool_or(pm.role = 'owner') then 'owner'
                when bool_or(pm.role = 'writer') then 'writer'
                else 'reader'
            end as role
        from public.project_members pm
        join active a on
            (pm.principal_kind = 'user' and pm.principal_id = a.oid)
            or (
                pm.principal_kind = 'group'
                and pm.principal_id = any (coalesce($1, '{}'::text[]))
            )
        group by pm.namespace_id
    ),
    project_ns as (
        select
            n.kind,
            n.alias,
            pr.role,
            null::text as group_write,
            s.project_write
        from public.namespaces n
        join project_roles pr on pr.namespace_id = n.id
        left join public.namespace_settings s on s.namespace_id = n.id
        where n.kind = 'project' and n.alias is not null
    ),
    org_ns as (
        select
            n.kind,
            n.alias,
            null::text as role,
            null::text as group_write,
            null::text as project_write
        from public.namespaces n
        join active a on true
        where n.kind = 'org' and n.alias is not null
    )
    select * from personal
    union all
    select * from group_ns
    union all
    select * from project_ns
    union all
    select * from org_ns
$$;

revoke execute on function mm_principal_namespaces(text[]) from public;
