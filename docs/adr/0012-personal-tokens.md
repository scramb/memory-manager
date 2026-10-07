# ADR-0012 — Personal tokens: owner-bound static tokens that users create themselves

Status: Proposed · Date: 2026-10-07
Relates to: auth, cli, `/account`, F-02 Client Integrations (WP-39); [ADR-0004](./0004-auth-model.md), [ADR-0006](./0006-enterprise-auth-entra.md) §7, [ADR-0008](./0008-namespace-permissions.md)

## Context

Several clients cannot run our OAuth flow, or run it badly:
- the Open WebUI filter ([ADR-0011](./0011-openwebui-identity.md));
- headless CLI runs in CI (Codex `exec`, Gemini CLI `-p`, Claude Code `-p`);
- agent runtimes on servers;
- IDEs whose OAuth support is partial.

They need a bearer token that belongs to one person.

Static tokens already have most of what that needs (`0002_static_tokens.sql`, `0007_token_principal.sql`): hashed at rest, scopes, a namespace list, expiry, an owner principal and roles (#115), plus `token create|list|revoke`. Two things are missing:

- **Self-service.** Today only the operator creates tokens, through the CLI with database access. Users cannot create, list or revoke their own.
- **Kind and bounds.** A token does not say whether it belongs to a person, an agent (M15) or a service. Nothing stops a token from carrying more rights than its owner has.

## Options

### A — Extend static tokens; self-service on `/account` wherever the embedded AS runs
Add the columns `kind` (`personal|agent|service`), `created_by`, `last_used_at` and `description`. A personal token's scopes and namespaces must be a subset of what its owner may do. That is checked at creation **and** on every request, because the owner's rights can shrink after creation.
- **Expiry is mandatory for personal tokens.** Default 90 days, maximum `PERSONAL_TOKEN_MAX_DAYS`.
- **`/account` gets a "Tokens" section.** It lists the user's tokens, creates them (shown once) and revokes them. The page is available whenever the embedded AS is on, behind the existing login (`password`, `oidc`, `entra`).
- **The CLI keeps `token create`** for operators and gains `--kind`. In single-user mode the owner is the local user, the `sub` of the configured login.

Pro: one token model, one verifier, one audit path; works with Git and Postgres backends.
Con: `/account` must exist outside enterprise mode. ADR-0008 scoped it to Postgres mode, so this widens it to the token section only.

### B — Mint personal tokens as long-lived OAuth refresh tokens
Pro: no new table columns.
Con: refresh rotation breaks clients that store one value; expiry and listing don't fit; the CIMD/DCR client binding is meaningless for a pasted token.

### C — CLI only, no self-service
Pro: smallest change.
Con: in a multi-user deployment every user needs an operator to get a token, and operators then handle other people's secrets.

## Recommendation

**A.** It is the smallest change that gives users their own revocable credentials without an operator in the loop. The self-service page depends on `/account`, which F-01 builds in WP-25. Until then the CLI path works and is sufficient for M12's acceptance.

Checked against the guardrails:
- Few dependencies: none new.
- OSS first: yes.
- Container: unchanged.
- Technology pool: within ADR-0001.
- Security: token hashes only; the token is shown once; every create/revoke is audited; rights are bounded by the owner's on every request.

## Consequences

- One migration adds the token columns. Existing tokens become `kind = service` and keep working unchanged.
- The verifier intersects a personal token's scopes and namespaces with the owner's current rights. In enterprise mode, a disabled user's personal tokens stop working with the delta sync (ADR-0006 §6).
- `/account` widens from enterprise-only to "embedded AS on" for its token section; the rest of the page stays enterprise-only.

## Reversibility

Cheap. Columns are additive; the page section can be switched off by configuration.
