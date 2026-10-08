-- SPDX-License-Identifier: AGPL-3.0-only
-- Browser sessions for `/account` (ADR-0008 addendum 2026-10-08, #228): an opaque
-- session id in a cookie, only its hash stored here, next to the principal it was
-- issued to and the two expiries `account/sessions.py` checks.
--
-- `oid` is a real FK into `users`, same shape as `0010_oauth_token_principal.sql`'s
-- `oauth_tokens.user_oid` - null for a `password`/`oidc` session, which establishes
-- no Entra identity at all. `roles` carries the same three Entra app role values as
-- every other principal-bearing table (`static_tokens`, `oauth_tokens`); no
-- "roles requires oid" pairing constraint, mirroring `oauth_tokens`/`oauth_auth_codes`
-- (not `static_tokens`'s stricter one) since a session's roles, like those two, come
-- from a completed login, not from an operator-chosen pairing.
--
-- `login_mode` is validated against the exact three values `config.py`'s
-- `LOGIN_MODE`/`http.py`'s `build_authenticator` accept.
--
-- `created_at`/`last_seen_at` are read and written by `account/sessions.py`;
-- `expires_at` is the absolute (not idle) expiry - the only one worth an index, since
-- it never moves after creation and is what `auth/store.cleanup`'s sweep deletes by.

create table account_sessions (
    session_hash text primary key,
    subject text not null,
    oid text null references users (oid),
    roles text[] not null default '{}',
    login_mode text not null,
    created_at timestamptz not null,
    last_seen_at timestamptz not null,
    expires_at timestamptz not null,

    constraint account_sessions_roles_known
        check (roles <@ array['Memory.User', 'Memory.Curator', 'Memory.Admin']::text[]),
    constraint account_sessions_login_mode_known
        check (login_mode in ('password', 'oidc', 'entra'))
);

create index account_sessions_expires_at_idx on account_sessions (expires_at);
