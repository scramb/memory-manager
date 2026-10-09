-- SPDX-License-Identifier: AGPL-3.0-only
-- Admin namespace/ACL management (ADR-0008 A2 + R2, #234): the five
-- `SECURITY DEFINER` functions `account/admin.py` calls to create group and
-- project namespaces, rename a namespace's alias, add/remove a project
-- member and update a namespace's write settings, without ever granting the
-- app role direct `INSERT`/`UPDATE`/`DELETE` on `namespaces`/`project_members`/
-- `namespace_settings` (same reasoning `0005_rls.sql`'s own module docstring
-- gives: those tables are read/written only by `SECURITY DEFINER` functions
-- that run with the owner's privileges regardless of caller).
--
-- Lives in the common migration chain, not `migrations/postgres/` (unlike
-- `0012_vector_layout.sql`/`0018_erasure_log.sql`): namespace/ACL
-- administration is ADR-0008 machinery that exists for both storage
-- backends (`0004_vault.sql`/`0005_rls.sql`/`0009_namespace_resolution.sql`,
-- the functions this migration is the direct continuation of, all live here
-- too) - only content erasure and the vector layout are
-- Postgres-mode-only.
--
-- Every function below re-checks `'Memory.Admin' = any(app.roles)` itself
-- (`errcode = '42501'`, `insufficient_privilege`, the same class RLS itself
-- raises for a denied write - CLAUDE.md "enforced twice, in Python and in
-- SQL") before touching any row - `account/admin.py`'s own Python-side role
-- check on the session is the first, independent layer, exactly like the
-- rest of ADR-0008's permission matrix (`mcp/namespaces.py` + `0005_rls.
-- sql`'s "two independent computations").
--
-- Every parameter is named with a `p_` prefix, never a bare column name
-- (`p_alias`, not `alias`): PL/pgSQL's default `#variable_conflict error`
-- would otherwise make `where alias = alias` raise "column reference
-- ambiguous" the moment a parameter's name matches the very column it is
-- compared against - the same reason `0009_namespace_resolution.sql`'s own
-- locals are all `v_`-prefixed, applied here to parameters too.
--
-- `namespaces.alias` gets a format CHECK here (defense in depth, CLAUDE.md:
-- validated in Python before insert *and* by DB CHECK, the same shape
-- `account_sessions.login_mode` already uses) - the charset
-- `vault/paths.py`'s own `_NAMESPACE_RE` requires once a note is ever
-- written under it. Every alias already stored by this point (the `u-*`
-- personal aliases `mm_ensure_personal_ns()` mints, the fixed `'org'`) is
-- already shaped that way, so this never needs a `NOT VALID` escape hatch.
alter table namespaces add constraint namespaces_alias_format
    check (alias is null or alias ~ '^[a-z0-9][a-z0-9-]{0,39}$');

-- `mm_admin_create_namespace(kind, external_key, alias)`: the only way a
-- `group`/`project` namespace is ever created (ADR-0008 A2: "an admin step
-- to create group and project namespaces"). Never `user`/`org` - the
-- personal namespace is always lazy (`mm_ensure_personal_ns()`), and `org`
-- is a fixed, reserved alias no admin ever (re-)creates (ADR-0008: "`org` is
-- reserved").
--
-- `VOLATILE SECURITY DEFINER` with the pinned `search_path`, same shape
-- `mm_ensure_personal_ns()` already uses. A duplicate alias (the table's own
-- `unique` constraint) or a duplicate `(kind, external_key)` surfaces as a
-- plain `UniqueViolationError` to the caller - `account/admin.py` turns that
-- into a 400, never a 500.
create function mm_admin_create_namespace(p_kind text, p_external_key text, p_alias text)
    returns bigint
    language plpgsql
    volatile
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_id bigint;
begin
    if not (
        'Memory.Admin' = any(
            string_to_array(coalesce(nullif(current_setting('app.roles', true), ''), ''), ',')
        )
    ) then
        raise exception 'mm_admin_create_namespace: caller lacks Memory.Admin'
            using errcode = '42501';
    end if;

    if p_kind not in ('group', 'project') then
        raise exception 'mm_admin_create_namespace: kind must be group or project, got %', p_kind
            using errcode = '22023';
    end if;

    insert into public.namespaces (kind, external_key, alias)
    values (p_kind, p_external_key, p_alias)
    returning id into v_id;

    return v_id;
end;
$$;

revoke execute on function mm_admin_create_namespace(text, text, text) from public;

-- `mm_admin_rename_namespace_alias(old_alias, new_alias)`: re-point an
-- existing `group`/`project`/`org` namespace's alias (the migration's own
-- Implementation checklist, "namespace create/rename alias"). Looked up by
-- its *current* alias rather than a numeric id - the only handle
-- `account/admin.py`'s forms ever need to carry, and the one every other
-- admin field below is addressed by too.
create function mm_admin_rename_namespace_alias(p_old_alias text, p_new_alias text)
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
        raise exception 'mm_admin_rename_namespace_alias: caller lacks Memory.Admin'
            using errcode = '42501';
    end if;

    update public.namespaces set alias = p_new_alias where alias = p_old_alias;

    if not found then
        raise exception 'mm_admin_rename_namespace_alias: namespace with alias % not found',
            p_old_alias
            using errcode = 'P0002';
    end if;
end;
$$;

revoke execute on function mm_admin_rename_namespace_alias(text, text) from public;

-- `mm_admin_add_project_member(alias, principal_kind, principal_id, role)`:
-- insert or, for a principal already on the project, update its role
-- (`on conflict ... do update`, idempotent the same way `mm_ensure_personal_
-- ns()`'s own alias backfill is). Refuses an alias that does not resolve to
-- a `project` namespace - `group`/`org` membership is not a thing
-- `project_members` models (ADR-0008 A2).
create function mm_admin_add_project_member(
    p_alias text, p_principal_kind text, p_principal_id text, p_role text
)
    returns void
    language plpgsql
    volatile
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_namespace_id bigint;
begin
    if not (
        'Memory.Admin' = any(
            string_to_array(coalesce(nullif(current_setting('app.roles', true), ''), ''), ',')
        )
    ) then
        raise exception 'mm_admin_add_project_member: caller lacks Memory.Admin'
            using errcode = '42501';
    end if;

    if p_principal_kind not in ('user', 'group') then
        raise exception
            'mm_admin_add_project_member: principal_kind must be user or group, got %',
            p_principal_kind
            using errcode = '22023';
    end if;
    if p_role not in ('reader', 'writer', 'owner') then
        raise exception 'mm_admin_add_project_member: role must be reader, writer or owner, got %',
            p_role
            using errcode = '22023';
    end if;

    select id into v_namespace_id
    from public.namespaces
    where alias = p_alias and kind = 'project';
    if v_namespace_id is null then
        raise exception 'mm_admin_add_project_member: no project namespace with alias %', p_alias
            using errcode = 'P0002';
    end if;

    insert into public.project_members (namespace_id, principal_kind, principal_id, role)
    values (v_namespace_id, p_principal_kind, p_principal_id, p_role)
    on conflict (namespace_id, principal_kind, principal_id) do update set role = excluded.role;
end;
$$;

revoke execute on function mm_admin_add_project_member(text, text, text, text) from public;

-- `mm_admin_remove_project_member(alias, principal_kind, principal_id)`: the
-- inverse of the function above. A no-op (not an error) when the principal
-- was never a member - same "idempotent, nothing to report back" shape
-- `account.sessions.revoke` already uses for a session that is already gone.
create function mm_admin_remove_project_member(
    p_alias text, p_principal_kind text, p_principal_id text
)
    returns void
    language plpgsql
    volatile
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_namespace_id bigint;
begin
    if not (
        'Memory.Admin' = any(
            string_to_array(coalesce(nullif(current_setting('app.roles', true), ''), ''), ',')
        )
    ) then
        raise exception 'mm_admin_remove_project_member: caller lacks Memory.Admin'
            using errcode = '42501';
    end if;

    select id into v_namespace_id
    from public.namespaces
    where alias = p_alias and kind = 'project';
    if v_namespace_id is null then
        raise exception 'mm_admin_remove_project_member: no project namespace with alias %',
            p_alias
            using errcode = 'P0002';
    end if;

    delete from public.project_members
    where namespace_id = v_namespace_id
      and principal_kind = p_principal_kind
      and principal_id = p_principal_id;
end;
$$;

revoke execute on function mm_admin_remove_project_member(text, text, text) from public;

-- `mm_admin_update_namespace_settings(alias, group_write, project_write)`:
-- upsert `namespace_settings` for an existing `group`/`project` namespace.
-- Either column may be `NULL` ("leave this one alone") - `coalesce` against
-- the row's own current value (or the column's database default, for a
-- namespace with no settings row yet) on conflict, the same "only touch
-- what was actually asked for" shape `0009_namespace_resolution.sql`'s own
-- alias backfill uses.
create function mm_admin_update_namespace_settings(
    p_alias text, p_group_write text, p_project_write text
)
    returns void
    language plpgsql
    volatile
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_namespace_id bigint;
begin
    if not (
        'Memory.Admin' = any(
            string_to_array(coalesce(nullif(current_setting('app.roles', true), ''), ''), ',')
        )
    ) then
        raise exception 'mm_admin_update_namespace_settings: caller lacks Memory.Admin'
            using errcode = '42501';
    end if;

    if p_group_write is not null and p_group_write not in ('members', 'curators') then
        raise exception
            'mm_admin_update_namespace_settings: group_write must be members or curators, got %',
            p_group_write
            using errcode = '22023';
    end if;
    if p_project_write is not null and p_project_write not in ('readers', 'writers') then
        raise exception
            'mm_admin_update_namespace_settings: project_write must be readers or writers, got %',
            p_project_write
            using errcode = '22023';
    end if;

    select id into v_namespace_id from public.namespaces where alias = p_alias;
    if v_namespace_id is null then
        raise exception 'mm_admin_update_namespace_settings: no namespace with alias %', p_alias
            using errcode = 'P0002';
    end if;

    insert into public.namespace_settings (namespace_id, group_write, project_write)
    values (
        v_namespace_id,
        coalesce(p_group_write, 'members'),
        coalesce(p_project_write, 'writers')
    )
    on conflict (namespace_id) do update set
        group_write = coalesce(excluded.group_write, public.namespace_settings.group_write),
        project_write = coalesce(excluded.project_write, public.namespace_settings.project_write);
end;
$$;

revoke execute on function mm_admin_update_namespace_settings(text, text, text) from public;

-- `mm_admin_list_namespaces()`: every namespace plus its note count (never
-- content - CLAUDE.md "counts only, never content") - the one read
-- `account/admin.py`'s namespace list renders. Admin-gated the same way the
-- five write functions above are, even though `account/admin.py` itself
-- only ever calls this from the already role-gated admin section
-- (CLAUDE.md "enforced twice").
create function mm_admin_list_namespaces()
    returns table (
        id bigint,
        kind text,
        external_key text,
        alias text,
        note_count bigint
    )
    language sql
    stable
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
    select
        n.id,
        n.kind,
        n.external_key,
        n.alias,
        (
            select count(*) from public.vault_notes vn where vn.namespace = n.alias
        ) as note_count
    from public.namespaces n
    where (
        'Memory.Admin' = any(
            string_to_array(coalesce(nullif(current_setting('app.roles', true), ''), ''), ',')
        )
    )
    order by n.kind, n.alias
$$;

revoke execute on function mm_admin_list_namespaces() from public;
