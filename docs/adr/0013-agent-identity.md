# ADR-0013 — Autonomous agents: own identity, own namespace, guarded writes

Status: Accepted · Date: 2026-10-08
Relates to: auth, mcp, storage, F-02 Client Integrations (M15, WP-52 … WP-55); [ADR-0008](./0008-namespace-permissions.md), [ADR-0012](./0012-personal-tokens.md)

## Context

Hermes Agent and OpenClaw run unattended and read messages from third parties: Telegram, WhatsApp, Slack and mail ([research](../research/clients/hermes.md), [research](../research/clients/openclaw.md)). Anyone who can message the agent can ask it to "remember" something. Without a guard, that text lands in its owner's memory and is later read back as trusted context. This is persistent prompt injection.

F-02 requires the following:
- an agent identity distinct from any human;
- a choice between an own agent namespace and delegated writes into the owner's namespace, with the own namespace as default;
- reading the owner's namespace only after explicit grant;
- write modes: read-only, own namespace only, or an approval queue the owner confirms;
- rate limits and quotas per agent token, and audit with agent and triggering channel.

Constraints from the existing design:
- **The namespace charset is `^[a-z0-9][a-z0-9-]{0,39}$`** (ADR-0005, ADR-0008). The `agent:<name>` from the prompt is therefore not a valid path segment.
- **The tool contract must not break.**

## Options

### A — Agent tokens + namespace kind `agent` + server-side write policy + approval queue table
- **Tokens and namespaces.** An agent token is a token with `kind = agent` ([ADR-0012](./0012-personal-tokens.md)), an owner and an agent name. Its default namespace is an alias `agent-<name>` of the new kind `agent`, owned by the owner.
- **Write policy.** Each agent has a policy: `read_only`, `own_namespace` (default) or `approval`.
  - `approval` turns a write into a pending entry in a new `pending_writes` table instead of a note.
  - The owner approves or rejects it on `/account` or with `memory-manager agent approve|reject`.
  - Approval runs the normal write path with the original `if_version` checks.
- **Delegation and reads.** Delegated writes into the owner's `me` require an explicit `delegate_write` grant. Reads of `me` require `delegate_read`.
- **Channel in the audit.** The audit log records `agent` and, when the runtime sends it, `channel`, through an optional header `MM-Agent-Channel`. It is free text, logged only, and never used for authorization.

Pro: the guard sits on the server, where a compromised agent prompt cannot switch it off; everything is additive to the contract. A pending write returns a normal tool result that says "queued for owner approval".
Con: one new namespace kind, one new table, and an approval UI. In the Git backend, `agent-<name>` is just a namespace string and the policy lives in the token row.

### B — Only token scopes (read-only tokens), no queue
Pro: nothing new.
Con: the approval mode the owner asked for does not exist; operators have to choose between a useless and an unsafe agent.

### C — Rely on the runtime's own guards (Hermes `trust: untrusted`, OpenClaw tool filters)
Pro: zero server work.
Con: the guard lives in the component the attacker is talking to; a misconfiguration silently opens the owner's memory.

## Decision

**A**, accepted by the owner on 2026-10-08. Integration tier 2 (native memory plugins) is not part of v1 ([ADR-0014](./0014-agent-integration-tier.md)). The deciding reason is that the write guard must sit on the server, outside the reach of the agent's prompt. The runtime guards in C are documented as an additional layer, not as the protection.

Checked against the guardrails:
- Few dependencies: none new.
- OSS first: yes.
- Container: unchanged.
- Technology pool: within ADR-0001. A native OpenClaw memory plugin (integration tier 2) would be TypeScript, outside the pool, and is not part of v1 (ADR-0014).

## Consequences

- New namespace kind `agent` in the registry (ADR-0008), with RLS policies to match. New table `pending_writes`, which holds content until approval and is purged after the decision or after `PENDING_WRITE_TTL_DAYS`.
- Per-token rate limits exist (#39). Per-agent quotas reuse the quota mechanism of F-01 (WP-27), so M15 waits for it.
- An injection test becomes part of the acceptance: a third party's "remember …" never reaches the owner's namespace.

## Reversibility

Medium. The namespace kind and the `agent-` aliases end up in data and exports; the policy modes are configuration.
