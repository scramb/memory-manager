-- SPDX-License-Identifier: AGPL-3.0-only
-- Namespace permissions (ADR-0008 A2 + R2, addendum 2026-10-07, #100): the
-- membership tables the permission functions read, the two functions
-- themselves, and row-level security on every content table they protect.
--
-- Membership tables below carry no RLS and no grants: they are read only by
-- the `SECURITY DEFINER` functions, which run with the owner's privileges
-- regardless of the caller's role. Nothing here needs superuser or
-- CREATEROLE - this migration runs fine as a plain database owner, which is
-- what CloudNativePG gives it (`tests/db/test_migrations.py`'s non-superuser
-- test covers that).
--
-- Out of scope here (#101): wiring the role-switch helper into
-- `PostgresBackend`/search/the indexer, the app role's name/provisioning,
-- `Memory.Admin`'s namespace/ACL administration, and grants on `namespaces`
-- or these membership tables for alias resolution - the functions below
-- need none of that, since `SECURITY DEFINER` already gives them owner
-- privileges on every table they touch.

create table users (
    oid text primary key, -- Entra `oid` claim
    tid text not null, -- Entra tenant id
    display_name text not null,
    disabled_at timestamptz
);

create table user_groups (
    oid text not null references users (oid) on delete cascade,
    group_id text not null, -- Entra group object id
    fetched_at timestamptz not null default now(),
    primary key (oid, group_id)
);

create table project_members (
    namespace_id bigint not null references namespaces (id) on delete cascade,
    principal_kind text not null check (principal_kind in ('user', 'group')),
    principal_id text not null, -- a `users.oid` or a `user_groups.group_id`
    role text not null check (role in ('reader', 'writer', 'owner')),
    primary key (namespace_id, principal_kind, principal_id)
);

-- Reverse lookup: "which projects is this user/group a principal of",
-- the direction `mm_readable_ns`/`mm_writable_ns` query in.
create index project_members_principal_idx on project_members (principal_kind, principal_id);

create table namespace_settings (
    namespace_id bigint primary key references namespaces (id) on delete cascade,
    group_write text not null default 'members' check (group_write in ('members', 'curators')),
    project_write text not null default 'writers' check (project_write in ('readers', 'writers'))
);

-- Break-glass read grants (ADR-0008 "Break-glass"; workflow - request,
-- approval, audit, notification - is WP-26. Only the columns the read
-- function needs exist here.)
create table break_glass_grants (
    id bigint generated always as identity primary key,
    namespace_id bigint not null references namespaces (id) on delete cascade,
    requester text not null, -- the admin's `users.oid`; must equal `app.oid` to be usable
    reason text not null,
    requested_at timestamptz not null default now(),
    approved boolean not null default false,
    approved_by text,
    approved_at timestamptz,
    expires_at timestamptz not null,
    revoked_at timestamptz
);

-- `mm_readable_ns()` / `mm_writable_ns()`: the per-transaction identity
-- (`app.oid`, `app.roles`, `app.break_glass`, set by `db/rls.py`'s
-- `request_identity`) resolved against the membership tables above into the
-- set of namespace aliases (`namespaces.alias`, the stored first path
-- segment, = `vault_notes.namespace` = `notes.namespace`) the identity may
-- read or write, per the ADR-0008 permission matrix. A namespace row with
-- `alias is null` grants nothing (A2: group/project namespaces get an alias
-- only once an admin creates them).
--
-- `STABLE SECURITY DEFINER` with a pinned `search_path` and schema-qualified
-- tables (docs/research/enterprise.md §3.2): the function runs with the
-- owner's privileges regardless of caller, so the caller needs no grants on
-- `users`/`user_groups`/`project_members`/`namespace_settings`/
-- `break_glass_grants`/`namespaces`. `EXECUTE` is revoked from `PUBLIC`
-- below; `db/rls.py`'s grant helper grants it to the one app role.
--
-- Missing or empty `app.oid`/`app.roles`/`app.break_glass` (never set, or a
-- previous transaction on a pooled connection set them and this one did
-- not) resolve to the empty string via `coalesce(nullif(current_setting(...,
-- true), ''), '')`, which never matches a membership row - zero rows, no
-- error. A disabled user (`users.disabled_at is not null`) is treated as no
-- identity at all, for both functions.
create function mm_readable_ns() returns text[]
    language sql
    stable
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
    with ctx as (
        select
            coalesce(nullif(current_setting('app.oid', true), ''), '') as oid,
            coalesce(nullif(current_setting('app.roles', true), ''), '') as roles_raw,
            nullif(current_setting('app.break_glass', true), '') as break_glass_raw
    ),
    identity as (
        select
            ctx.oid,
            case when ctx.roles_raw = '' then '{}'::text[] else string_to_array(ctx.roles_raw, ',') end
                as roles,
            ctx.break_glass_raw,
            u.disabled_at
        from ctx
        left join public.users u on u.oid = ctx.oid and ctx.oid <> ''
    ),
    active as (
        select oid, roles, break_glass_raw
        from identity
        where oid <> '' and disabled_at is null
    ),
    memberships as (
        select a.oid, g.group_id
        from active a
        join public.user_groups g on g.oid = a.oid
    ),
    personal as (
        select n.alias
        from public.namespaces n
        join active a on n.kind = 'user' and n.external_key = a.oid
        where n.alias is not null
    ),
    group_read as (
        select n.alias
        from public.namespaces n
        join memberships m on n.kind = 'group' and n.external_key = m.group_id
        where n.alias is not null
    ),
    project_read as (
        select distinct n.alias
        from public.namespaces n
        join public.project_members pm on pm.namespace_id = n.id
        join active a on
            (pm.principal_kind = 'user' and pm.principal_id = a.oid)
            or (
                pm.principal_kind = 'group'
                and pm.principal_id in (select group_id from memberships where oid = a.oid)
            )
        where n.kind = 'project' and n.alias is not null
    ),
    org_read as (
        -- "every user with a memory role" (ADR-0008 matrix): any non-empty
        -- `app.roles`, which only ever carries `Memory.User`/`Curator`/`Admin`.
        select n.alias
        from public.namespaces n
        join active a on cardinality(a.roles) > 0
        where n.kind = 'org' and n.alias is not null
    ),
    break_glass as (
        select n.alias
        from public.break_glass_grants bg
        join public.namespaces n on n.id = bg.namespace_id
        join active a on true
        where bg.id = a.break_glass_raw::bigint
          and bg.requester = a.oid
          and bg.approved
          and bg.revoked_at is null
          and bg.expires_at > now()
    )
    select coalesce(array_agg(alias), '{}'::text[])
    from (
        select alias from personal
        union select alias from group_read
        union select alias from project_read
        union select alias from org_read
        union select alias from break_glass
    ) resolved
$$;

revoke execute on function mm_readable_ns() from public;

create function mm_writable_ns() returns text[]
    language sql
    stable
    security definer
    set search_path = pg_catalog, public, pg_temp
as $$
    with ctx as (
        select
            coalesce(nullif(current_setting('app.oid', true), ''), '') as oid,
            coalesce(nullif(current_setting('app.roles', true), ''), '') as roles_raw
    ),
    identity as (
        select
            ctx.oid,
            case when ctx.roles_raw = '' then '{}'::text[] else string_to_array(ctx.roles_raw, ',') end
                as roles,
            u.disabled_at
        from ctx
        left join public.users u on u.oid = ctx.oid and ctx.oid <> ''
    ),
    active as (
        select oid, roles
        from identity
        where oid <> '' and disabled_at is null
    ),
    memberships as (
        select a.oid, g.group_id
        from active a
        join public.user_groups g on g.oid = a.oid
    ),
    personal as (
        -- the user always writes their own namespace; no per-namespace setting
        select n.alias
        from public.namespaces n
        join active a on n.kind = 'user' and n.external_key = a.oid
        where n.alias is not null
    ),
    group_write as (
        select n.alias
        from public.namespaces n
        join memberships m on n.kind = 'group' and n.external_key = m.group_id
        join active a on m.oid = a.oid
        left join public.namespace_settings s on s.namespace_id = n.id
        where n.alias is not null
          and (
              coalesce(s.group_write, 'members') = 'members'
              or 'Memory.Curator' = any (a.roles)
          )
    ),
    project_write as (
        select distinct n.alias
        from public.namespaces n
        join public.project_members pm on pm.namespace_id = n.id
        join active a on
            (pm.principal_kind = 'user' and pm.principal_id = a.oid)
            or (
                pm.principal_kind = 'group'
                and pm.principal_id in (select group_id from memberships where oid = a.oid)
            )
        left join public.namespace_settings s on s.namespace_id = n.id
        where n.kind = 'project' and n.alias is not null
          and (
              coalesce(s.project_write, 'writers') = 'readers'
              or pm.role in ('writer', 'owner')
          )
    ),
    org_write as (
        select n.alias
        from public.namespaces n
        join active a on ('Memory.Curator' = any (a.roles) or 'Memory.Admin' = any (a.roles))
        where n.kind = 'org' and n.alias is not null
    )
    -- Break-glass never grants write (ADR-0008 addendum: "never write access").
    select coalesce(array_agg(alias), '{}'::text[])
    from (
        select alias from personal
        union select alias from group_write
        union select alias from project_write
        union select alias from org_write
    ) resolved
$$;

revoke execute on function mm_writable_ns() from public;

-- Row-level security on every content table. Each gets:
--  - one owner-only policy `to current_user` (resolved, at migration time, to
--    whatever role runs this migration - the owner, per the ADR-0008
--    addendum): Git-mode indexing, `reindex --full` and other system jobs
--    keep connecting as the owner and are unaffected by any of this.
--  - per-command policies `to public` (every other role, since `current_user`
--    above already claims the owner): `select` against `mm_readable_ns()`,
--    `insert`/`update`/`delete` against `mm_writable_ns()`. `vault_revisions`
--    only ever gets appended to or read, never updated or deleted.
-- `force row level security` matters only because of the owner-only policy:
-- without `force`, the owner would bypass RLS anyway as the table owner;
-- with it, the owner is subject to RLS like everyone else and relies on
-- that explicit policy - the same mechanism a non-owner app role relies on.

alter table vault_notes enable row level security;
alter table vault_notes force row level security;

create policy vault_notes_owner_access on vault_notes
    to current_user
    using (true)
    with check (true);
comment on policy vault_notes_owner_access on vault_notes is
    'System identity (the role that ran this migration) per ADR-0008 addendum 2026-10-07 (#100): Git-mode indexing, reindex --full and other system jobs connect as the owner and bypass the namespace checks below entirely.';

create policy vault_notes_select on vault_notes
    for select to public
    using (namespace = any ((select mm_readable_ns())::text[]));

create policy vault_notes_insert on vault_notes
    for insert to public
    with check (namespace = any ((select mm_writable_ns())::text[]));

create policy vault_notes_update on vault_notes
    for update to public
    using (namespace = any ((select mm_writable_ns())::text[]))
    with check (namespace = any ((select mm_writable_ns())::text[]));

create policy vault_notes_delete on vault_notes
    for delete to public
    using (namespace = any ((select mm_writable_ns())::text[]));

alter table vault_revisions enable row level security;
alter table vault_revisions force row level security;

create policy vault_revisions_owner_access on vault_revisions
    to current_user
    using (true)
    with check (true);
comment on policy vault_revisions_owner_access on vault_revisions is
    'System identity (the role that ran this migration) per ADR-0008 addendum 2026-10-07 (#100): Git-mode indexing, reindex --full and other system jobs connect as the owner and bypass the namespace checks below entirely.';

create policy vault_revisions_select on vault_revisions
    for select to public
    using (exists (
        select 1 from vault_notes vn
        where vn.id = vault_revisions.note_id
          and vn.namespace = any ((select mm_readable_ns())::text[])
    ));

create policy vault_revisions_insert on vault_revisions
    for insert to public
    with check (exists (
        select 1 from vault_notes vn
        where vn.id = vault_revisions.note_id
          and vn.namespace = any ((select mm_writable_ns())::text[])
    ));

alter table notes enable row level security;
alter table notes force row level security;

create policy notes_owner_access on notes
    to current_user
    using (true)
    with check (true);
comment on policy notes_owner_access on notes is
    'System identity (the role that ran this migration) per ADR-0008 addendum 2026-10-07 (#100): Git-mode indexing, reindex --full and other system jobs connect as the owner and bypass the namespace checks below entirely.';

create policy notes_select on notes
    for select to public
    using (namespace = any ((select mm_readable_ns())::text[]));

create policy notes_insert on notes
    for insert to public
    with check (namespace = any ((select mm_writable_ns())::text[]));

create policy notes_update on notes
    for update to public
    using (namespace = any ((select mm_writable_ns())::text[]))
    with check (namespace = any ((select mm_writable_ns())::text[]));

create policy notes_delete on notes
    for delete to public
    using (namespace = any ((select mm_writable_ns())::text[]));

alter table chunks enable row level security;
alter table chunks force row level security;

create policy chunks_owner_access on chunks
    to current_user
    using (true)
    with check (true);
comment on policy chunks_owner_access on chunks is
    'System identity (the role that ran this migration) per ADR-0008 addendum 2026-10-07 (#100): Git-mode indexing, reindex --full and other system jobs connect as the owner and bypass the namespace checks below entirely.';

create policy chunks_select on chunks
    for select to public
    using (exists (
        select 1 from notes n
        where n.id = chunks.note_id
          and n.namespace = any ((select mm_readable_ns())::text[])
    ));

create policy chunks_insert on chunks
    for insert to public
    with check (exists (
        select 1 from notes n
        where n.id = chunks.note_id
          and n.namespace = any ((select mm_writable_ns())::text[])
    ));

create policy chunks_update on chunks
    for update to public
    using (exists (
        select 1 from notes n
        where n.id = chunks.note_id
          and n.namespace = any ((select mm_writable_ns())::text[])
    ))
    with check (exists (
        select 1 from notes n
        where n.id = chunks.note_id
          and n.namespace = any ((select mm_writable_ns())::text[])
    ));

create policy chunks_delete on chunks
    for delete to public
    using (exists (
        select 1 from notes n
        where n.id = chunks.note_id
          and n.namespace = any ((select mm_writable_ns())::text[])
    ));

alter table links enable row level security;
alter table links force row level security;

create policy links_owner_access on links
    to current_user
    using (true)
    with check (true);
comment on policy links_owner_access on links is
    'System identity (the role that ran this migration) per ADR-0008 addendum 2026-10-07 (#100): Git-mode indexing, reindex --full and other system jobs connect as the owner and bypass the namespace checks below entirely.';

create policy links_select on links
    for select to public
    using (exists (
        select 1 from notes n
        where n.id = links.source_id
          and n.namespace = any ((select mm_readable_ns())::text[])
    ));

create policy links_insert on links
    for insert to public
    with check (exists (
        select 1 from notes n
        where n.id = links.source_id
          and n.namespace = any ((select mm_writable_ns())::text[])
    ));

create policy links_update on links
    for update to public
    using (exists (
        select 1 from notes n
        where n.id = links.source_id
          and n.namespace = any ((select mm_writable_ns())::text[])
    ))
    with check (exists (
        select 1 from notes n
        where n.id = links.source_id
          and n.namespace = any ((select mm_writable_ns())::text[])
    ));

create policy links_delete on links
    for delete to public
    using (exists (
        select 1 from notes n
        where n.id = links.source_id
          and n.namespace = any ((select mm_writable_ns())::text[])
    ));
