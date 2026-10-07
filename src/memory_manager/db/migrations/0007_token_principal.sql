-- SPDX-License-Identifier: AGPL-3.0-only
-- Token principal (ADR-0008 addendum 2026-10-07 "identity sources and curate", #101/#115/#116;
-- ADR-0006 §7): `static_tokens` gets an owner principal, pulled forward from WP-24 so Postgres
-- mode is testable end-to-end over HTTP before the Entra login (WP-22) exists.
--
-- `roles` carries the same three Entra app role values `0005_rls.sql`'s `mm_readable_ns`/
-- `mm_writable_ns` read from `app.roles` (`Memory.User`, `Memory.Curator`, `Memory.Admin`) -
-- the first CHECK below is the DB-side half of the validation `auth/tokens.py`'s `create_token`
-- also does in Python before the insert (CLAUDE.md "validated in Python before insert AND by
-- DB CHECK"). A token with roles but no owner is meaningless (ADR-0006 §3: a user without a
-- memory role is denied - the inverse holds too, a role needs someone to belong to), so the
-- second CHECK forbids it.
--
-- `owner_oid` has no format requirement beyond non-empty, length-bounded and free of
-- whitespace/control characters: Postgres-mode RLS fixtures seed plain strings such as
-- `oid-alice`, not real Entra GUIDs, and this column is not joined against `users.oid` here -
-- that is the request-path wiring (#116). An empty string is rejected on purpose: RLS treats
-- `''` as "no identity" (0005_rls.sql's `coalesce(nullif(current_setting(...), ''), '')`).

alter table static_tokens add column owner_oid text null;
alter table static_tokens add column roles text[] not null default '{}';

alter table static_tokens add constraint static_tokens_roles_known
    check (roles <@ array['Memory.User', 'Memory.Curator', 'Memory.Admin']::text[]);

alter table static_tokens add constraint static_tokens_owner_oid_bounded
    check (
        owner_oid is null
        or (
            length(owner_oid) > 0
            and length(owner_oid) <= 128
            and owner_oid !~ '[[:space:][:cntrl:]]'
        )
    );

alter table static_tokens add constraint static_tokens_roles_require_owner
    check (cardinality(roles) = 0 or owner_oid is not null);
