# ADR-0010 — Client compatibility: one strict tool surface, profiles for delivery, explicit selection

Status: Accepted · Date: 2026-10-08
Relates to: mcp, cli, F-02 Client Integrations (WP-38, WP-40); [ADR-0009](./0009-stateless-replicas.md)

## Context

F-02 brings memory-manager to about a dozen clients: claude.ai, Claude Code, Open WebUI, Cursor, Codex, GitHub Copilot, Antigravity, Gemini CLI, Hermes Agent, OpenClaw, ChatGPT and Gemini Enterprise. The research notes in [`docs/research/clients/`](../research/clients/) show where they differ:

- **Tool names.** Several clients prefix the server name (Open WebUI `<server_id>_<tool>`, Cursor, Gemini CLI), and some models cap the combined length at 64 characters.
- **Schemas.** Gemini-based clients accept only an OpenAPI subset of JSON Schema: no `$ref`, limited `oneOf`/`anyOf`, no `additionalProperties` in some versions.
- **Server `instructions` and prompts.** Claude Code and claude.ai use them. Open WebUI, and probably most others, never pass them to the model (unverified for several clients).
- **Tool annotations.** Hermes (`trust: untrusted`) and ChatGPT decide on approval and write handling from `readOnlyHint`. Our tools set no annotations today.

The prompt for F-02 asks for compatibility profiles detected from `clientInfo` in the MCP handshake, with a manual override. Two constraints make detection alone unworkable:

1. **ADR-0009 makes the server stateless.** On protocol 2026-07-28, every request carries `clientInfo` in its `_meta` envelope (`mcp` 2.3.0, `shared/inbound.py`). On 2025-11-25, which claude.ai speaks today, `clientInfo` arrives only with `initialize`. A later `tools/list` on another replica cannot know it.
2. **No breaking changes to the tool contract.** Tool names, parameters and result shapes must stay the same for every client. A profile that renames a tool or reshapes a parameter is a fork in disguise.

## Options

### A — Per-client tool surfaces selected by `clientInfo`
Each profile may rename tools and rewrite schemas.
Pro: maximal fit per client · Con: breaks the single contract; it cannot work statelessly on 2025-11-25 clients; every client becomes its own test matrix.

### B — One tool surface that satisfies the strictest supported profile; profiles change delivery only
The canonical tool definitions are kept within the intersection of all supported clients' limits: name length, schema subset, description length, annotations. A schema linter in CI enforces this. A profile only decides **how the usage rules are delivered**: full `instructions`, the rules folded into tool descriptions, or the short form. It also sets the result-size budget and holds the client's documented limits for the linter. The profile is chosen explicitly through the URL query `?profile=<name>` or the header `MM-Client-Profile`. Without either, `clientInfo` is used when present in the request envelope (2026-07-28). Without that, the `default` profile applies.
Pro: one contract and one test matrix; works on any replica; the override stays the only way to change behaviour, and it is visible in the client's config · Con: the strictest client caps everyone. Today that costs nothing, because our schemas use only flat objects, strings, integers and string arrays.

### C — No profiles; lint only
Pro: least code · Con: clients that drop `instructions` get no usage rules, and the per-client limits that the linter checks have no home.

## Decision

**B**, accepted by the owner on 2026-10-08. The tool contract stays single and additive. Profiles live in `src/memory_manager/compat/` as data: limits, a delivery mode and a result budget. They do not live as code paths in the tool handlers.

Additive changes that come with this decision:
- Every tool gets MCP annotations: `readOnlyHint` (index, search, read), `destructiveHint: false`, and `idempotentHint` where true.
- Tool descriptions carry a two-sentence core of the usage rules (data-not-instructions, search before write) for profiles without `instructions`.
- Unknown profile names are rejected with a clear error, never silently mapped.

Checked against the guardrails:
- Few dependencies: none new; the linter is a small Python module plus a CLI subcommand.
- OSS first: yes.
- Container: unchanged.
- Technology pool: within ADR-0001.

## Consequences

- The `default` profile keeps today's behaviour for claude.ai and Claude Code byte for byte. The conformance suite proves it.
- Adding a client means adding a profile data file and running the linter, not branching code.
- A client whose limits our contract cannot meet without a breaking change is documented as *Partial* or *Not possible* in the support matrix rather than accommodated.

## Reversibility

Cheap. Profiles are server-side data, and the override is an optional URL parameter. Moving to option A later would be additive, but it would break the "one contract" promise of `docs/compatibility.md`.
