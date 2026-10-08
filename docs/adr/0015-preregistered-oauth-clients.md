# ADR-0015 — Operators can pre-register confidential OAuth clients in the authorization server

Status: Accepted · Date: 2026-10-08
Relates to: auth, cli, F-02 Client Integrations (WP-57, #196, #197); [ADR-0004](./0004-auth-model.md), [ADR-0006](./0006-enterprise-auth-entra.md)

## Context

The embedded authorization server registers clients through DCR or CIMD (ADR-0004). Gemini Enterprise offers neither. For its custom MCP data stores it accepts only no auth, or OAuth 2.0 with a client id and secret entered by an admin ([research](../research/clients/gemini-app.md), secondary sources). Cursor, Codex and Copilot can also use a fixed client id ([research](../research/clients/)). Without pre-registration, Gemini Enterprise cannot connect to memory-manager with per-user identity.

## Options

### A — Operator-registered confidential clients in our AS
`memory-manager oauth-client create|list|revoke`:
- Exact redirect URIs.
- The secret is shown once and stored encrypted, like confidential DCR clients (ADR-0004 addendum).
- Every action is audited.
- Login still goes through the configured login mode (`password`, `oidc`, `entra`).

Pro: additive to ADR-0004; per-user tokens bound to `oid` in enterprise mode; also serves other clients that prefer a fixed client id.
Con: operators handle one secret per pre-registered client.

### B — Route Gemini Enterprise through Entra
Pro: no AS change.
Con: memory-manager would have to accept Entra-issued tokens directly (resource-server mode, JOSE), which ADR-0006 §8 excludes from v1; it only works in enterprise mode.

### C — Mark Gemini Enterprise as *Not possible*
Pro: no work.
Con: it drops a client that the support matrix approved as *Partial*.

## Decision

**A**, decided by the owner on 2026-10-08. It is built in #197; until it exists, Gemini Enterprise cannot connect.

Checked against the guardrails:
- Few dependencies: none new.
- OSS first: yes.
- Container: unchanged.
- Technology pool: within ADR-0001.
- Security: the secret is shown once and stored encrypted; redirect URIs match exactly; every action is audited.

## Consequences

- One new CLI command group and a `registered_by` marker on OAuth clients. The cleanup of DCR clients without live tokens skips pre-registered clients.
- AS metadata does not change.

## Reversibility

Cheap. Pre-registered clients can be revoked; the command is additive.
