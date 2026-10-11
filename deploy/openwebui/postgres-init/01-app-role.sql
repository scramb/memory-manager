-- SPDX-License-Identifier: AGPL-3.0-only
--
-- Run by postgres-entra-overlay.yml's own `db-role-init` one-shot (#313,
-- WP-42, via `psql -f`, once postgres itself is healthy): creates the
-- non-owner `DATABASE_APP_ROLE` every request transaction switches to
-- under FORCE ROW LEVEL SECURITY (ADR-0008 addendum), and makes the
-- connecting owner (the base docker-compose.yml's own `POSTGRES_USER=mm`)
-- a member of it - the `check_app_role` startup check in
-- memory-manager's own `db` package requires exactly that membership
-- (`pg_has_role(current_user, role, 'MEMBER')`) before `grant_app_role`
-- ever runs. The `do` block makes role creation idempotent (plain
-- `create role` is not) - `scripts/openwebui-up.sh` always brings this
-- overlay up from a clean `down -v`, but a developer re-running `psql -f`
-- by hand against an already-initialized stack must not fail either.
do $$
begin
  if not exists (select from pg_roles where rolname = 'mm_app') then
    create role mm_app nologin nosuperuser nobypassrls;
  end if;
end
$$;

grant mm_app to mm;
