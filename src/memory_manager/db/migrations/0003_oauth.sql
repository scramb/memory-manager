-- SPDX-License-Identifier: AGPL-3.0-only
-- Embedded OAuth 2.1 authorization server (ADR-0004, #36): operational data,
-- never derivable from the vault.
--
-- Every secret is stored as its SHA-256 hex digest only (CLAUDE.md "token
-- hashes only"); plaintext tokens, codes and pending-authorization ids are
-- never written here. `oauth_tokens.revoked_at` is a soft delete, not a hard
-- one: a refresh token's row stays around after rotation so a later replay
-- of that same token can still be recognized (and its whole `family_id`
-- revoked) instead of looking exactly like "never existed" - the cleanup
-- job purges rows only well after they go stale.

create table oauth_clients (
    client_id text primary key,
    client_info jsonb not null,
    created_at timestamptz not null default now(),
    last_used_at timestamptz
);

create table oauth_pending (
    id_hash text primary key,
    client_id text not null references oauth_clients (client_id) on delete cascade,
    params jsonb not null,
    expires_at timestamptz not null
);

create table oauth_auth_codes (
    code_hash text primary key,
    client_id text not null references oauth_clients (client_id) on delete cascade,
    subject text not null,
    namespaces text[] not null,
    scopes text[] not null,
    code_challenge text not null,
    redirect_uri text not null,
    redirect_uri_provided_explicitly boolean not null,
    resource text,
    expires_at timestamptz not null
);

create table oauth_tokens (
    token_hash text primary key,
    kind text not null check (kind in ('access', 'refresh')),
    client_id text not null references oauth_clients (client_id) on delete cascade,
    subject text not null,
    namespaces text[] not null,
    scopes text[] not null,
    resource text,
    family_id text not null,
    client_label text not null,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    revoked_at timestamptz
);

create index oauth_tokens_family_idx on oauth_tokens (family_id);
