-- SPDX-License-Identifier: AGPL-3.0-only
-- Static bearer tokens (ADR-0004): operational data, never derivable from the vault.
--
-- `token_hash` is the SHA-256 hex digest of the plaintext (CLAUDE.md "token hashes
-- only"); the plaintext itself is never stored. `namespaces = '{*}'` means "every
-- namespace" - `memory_manager.auth.verifier` reads that literal, not an empty array,
-- as "no restriction".

create table static_tokens (
    id bigserial primary key,
    name text not null unique,
    token_hash text not null unique,
    scopes text[] not null,
    namespaces text[] not null,
    created_at timestamptz not null default now(),
    last_used_at timestamptz,
    expires_at timestamptz,
    revoked_at timestamptz
);
