-- SPDX-License-Identifier: AGPL-3.0-only
-- Personal tokens (ADR-0012, #134): `static_tokens` gains `kind` (who/what the
-- token represents), `created_by` (an audit-trail marker - never personal data)
-- and `description` (free text an operator/owner uses to tell their tokens
-- apart). Every existing row defaults to `kind = 'service'` and keeps working
-- exactly as before (ADR-0012 Consequences: "Existing tokens become kind =
-- service and keep working unchanged").
--
-- `kind = 'agent'` is accepted by the first CHECK below but not yet reachable
-- from anywhere (agent tokens/policies are WP-52, a later work package) - the
-- column simply never holds it today.
--
-- A personal token identifies one person and is useless without both an owner
-- and an expiry (ADR-0012: "Expiry is mandatory for personal tokens"); the
-- second CHECK enforces that in the database. `auth/tokens.py`'s `create_token`
-- validates the same rule in Python before the insert (CLAUDE.md "validated in
-- Python before insert AND by DB CHECK"), the same two-places convention
-- `0007_token_principal.sql`'s owner/roles pairing already uses.
--
-- `description` is bounded the same shape `0007_token_principal.sql`'s
-- `owner_oid` already is (length, no control characters) - unlike `owner_oid`,
-- whitespace is allowed (it is free text for a human, not an identifier).

alter table static_tokens add column kind text not null default 'service';
alter table static_tokens add column created_by text;
alter table static_tokens add column description text;

alter table static_tokens add constraint static_tokens_kind_known
    check (kind in ('personal', 'agent', 'service'));

alter table static_tokens add constraint static_tokens_personal_requires_owner_and_expiry
    check (kind <> 'personal' or (owner_oid is not null and expires_at is not null));

alter table static_tokens add constraint static_tokens_description_bounded
    check (
        description is null
        or (
            length(description) > 0
            and length(description) <= 500
            and description !~ '[[:cntrl:]]'
        )
    );
