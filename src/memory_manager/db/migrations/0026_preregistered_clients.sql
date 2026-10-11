-- SPDX-License-Identifier: AGPL-3.0-only
-- Operator-registered OAuth clients (ADR-0015, ADR-0004 addendum, #197): a confidential
-- client an operator registers through `memory-manager oauth-client create`, for an
-- integration that supports neither DCR (`/register`) nor CIMD (#38) - Gemini Enterprise
-- is the first one.
--
-- `registered_by` tells a pre-registered row apart from a DCR/CIMD one: `NULL` for every
-- row either of those two ever create (unchanged - neither ever sets it), the fixed marker
-- `'cli'` for a row `auth.registered_clients.create` inserts (CLAUDE.md "audit log for
-- every write" extends to "no personal data in it either" - `created_by`/`registered_by`
-- name the tool, never an operator). `auth.store.save_client`'s own `on conflict` clause
-- additionally refuses to overwrite a row once `registered_by` is set, so a DCR/CIMD
-- `client_id` collision with a pre-registered one (practically impossible - DCR mints a
-- `uuid4()`, CIMD's is an https URL, `auth.registered_clients.create`'s own `client_id`
-- is neither shape) can never silently replace an operator's registration.
--
-- `revoked_at` is a pre-registered client's own soft-delete, mirroring `oauth_tokens.
-- revoked_at` (`0003_oauth.sql`): `auth.store.get_client` now treats a revoked row exactly
-- like an unknown `client_id` (`/authorize`, `/token` both fail closed, the same "not
-- found" outcome a never-registered client already gets), and `auth.store.cleanup`'s
-- client sweep never removes a row with `registered_by` set, revoked or not - revocation is
-- `auth.registered_clients.revoke`'s job, not the age-based sweep's.

alter table oauth_clients add column registered_by text;
alter table oauth_clients add column revoked_at timestamptz;
