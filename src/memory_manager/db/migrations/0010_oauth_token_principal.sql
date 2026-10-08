-- SPDX-License-Identifier: AGPL-3.0-only
-- OAuth codes and tokens carry the Entra user they were issued to (ADR-0006
-- "New tables: ... Token rows reference users"; owner decision 2026-10-07,
-- addendum 2026-10-08; #213).
--
-- `users.last_seen`/`users.groups_fetched_at`: stamped by the Entra login
-- completer (#215, not this migration) and the Graph group sync (#214) -
-- `groups_fetched_at` is the per-user cache-TTL marker ADR-0006 §4 talks
-- about ("cached in Postgres with a configurable TTL"), distinct from
-- `user_groups.fetched_at` (0005_rls.sql, per-row) which stays untouched: a
-- user with zero current groups still needs a "when was this last checked"
-- timestamp to apply that TTL against, and a per-row column cannot carry
-- one once every row is gone.
--
-- `oauth_auth_codes`/`oauth_tokens` get `user_oid` (nullable: a `password`/
-- `oidc` login establishes no Entra identity at all, #216's own session
-- still has to carry namespaces but never an `oid`) and `roles`, the same
-- shape and the same CHECK as `static_tokens`' own `owner_oid`/`roles`
-- pairing (`0007_token_principal.sql`'s `static_tokens_roles_known`) - the
-- three Entra app role values `0005_rls.sql`'s `mm_readable_ns`/
-- `mm_writable_ns` read from `app.roles`. Unlike `static_tokens.owner_oid`,
-- `user_oid` here is a real FK into `users`: every row with one was issued
-- from a completed Entra login (#215), which always upserts the user first
-- (`auth/users.py`'s `upsert_user`), so the referenced row always exists.
--
-- `oauth_tokens.family_started_at`: the family's own session start, carried
-- forward unchanged by `auth/provider.py` across every refresh-token
-- rotation of that family (never reset) - the column `ENTRA_MAX_SESSION`
-- (#216, not this migration) will compare `now()` against. Nullable: a
-- `password`/`oidc` token family has no Entra session to bound this way and
-- simply never gets one.

alter table users add column last_seen timestamptz;
alter table users add column groups_fetched_at timestamptz;

alter table oauth_auth_codes add column user_oid text null references users (oid);
alter table oauth_auth_codes add column roles text[] not null default '{}';

alter table oauth_auth_codes add constraint oauth_auth_codes_roles_known
    check (roles <@ array['Memory.User', 'Memory.Curator', 'Memory.Admin']::text[]);

alter table oauth_tokens add column user_oid text null references users (oid);
alter table oauth_tokens add column roles text[] not null default '{}';
alter table oauth_tokens add column family_started_at timestamptz null;

alter table oauth_tokens add constraint oauth_tokens_roles_known
    check (roles <@ array['Memory.User', 'Memory.Curator', 'Memory.Admin']::text[]);
