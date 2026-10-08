# ADR-0014 — Agent runtimes: MCP integration only in v1, native memory plugins after 1.0

Status: Accepted · Date: 2026-10-08
Relates to: integrations/hermes, integrations/openclaw, F-02 Client Integrations (WP-53 … WP-55, #190 … #192); [ADR-0013](./0013-agent-identity.md), [ADR-0001](./0001-implementation-language.md)

## Context

Hermes Agent and OpenClaw can use memory-manager in two ways ([research](../research/clients/hermes.md), [research](../research/clients/openclaw.md)):

- **Tier 1: as an MCP server.** Both runtimes have native MCP clients with tool filters, so this works with configuration only. Their built-in memory is switched off or fenced, and that is documented.
- **Tier 2: as the runtime's native memory.**
  - For Hermes this is a `MemoryProvider` in Python.
  - For OpenClaw it is a memory-slot plugin replacing `memory-core`, written in TypeScript. TypeScript is outside the technology pool.
  - OpenClaw's memory capability API changes are dated 2026-10-01.
  - Both runtimes capture and consolidate memories automatically, which conflicts with curated, explicit writes.

## Options

### A — Tier 1 only in v1
Pro: no code outside the pool; no dependency on unstable plugin APIs; every write stays an explicit tool call that ADR-0013 guards.
Con: users switch the built-in memory off by hand; there is no automatic recall unless the runtime's skill tells the agent to search.

### B — Hermes provider in v1 (Python)
Pro: within ADR-0001; deeper integration for one runtime.
Con: automatic capture has to be marked and switched off by default; one more package to version against Hermes releases.

### C — OpenClaw plugin in v1 (TypeScript)
Pro: replaces `memory-core` cleanly.
Con: application code outside the pool; plugin API in flux.

### D — Both

## Decision

**A**, decided by the owner on 2026-10-08. Hermes and OpenClaw are supported through MCP (WP-53, WP-54). #191 and #192 are closed as not planned and are revisited after 1.0.

Checked against the guardrails:
- Few dependencies: none new.
- OSS first: yes.
- Container: unchanged.
- Technology pool: no TypeScript.

## Consequences

- Each runtime's integration consists of a config example, a generated skill with the usage rules, an importer for existing memory files, and docs on fencing the built-in memory.
- The injection guard of ADR-0013 covers every agent write, because there is no write path around MCP.
- Revisit trigger: a stable memory plugin API in OpenClaw, or user demand for automatic recall that the skill cannot provide.

## Reversibility

Cheap. Tier 2 is additive and can follow in a 1.x release.
