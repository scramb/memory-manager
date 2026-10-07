# Client: OpenClaw (formerly Clawdbot / Moltbot)

Retrieved: 2026-10-07 · Tested version: none (desk research; latest v2026.9.8, marked "Latest" on 2026-10-03; pre-release 2026.10.1-beta.1 on 2026-10-05 [O1])

`docs.openclaw.ai` was not reachable from the research environment (egress block). Docs were read from the Markdown sources in the repository (`docs/**` on `main`) [O2–O9], which may be ahead of the latest release. `[unverified]` marks claims without a primary source.

## Summary

OpenClaw is a self-hosted TypeScript/Node gateway (Node 24.16+ or 26.1+) that connects a personal agent to Discord, iMessage, Slack, Teams, Telegram, WhatsApp, Google Chat, Signal and 20+ more channels [O2]. It has **native MCP client support** (`mcp.servers` in `~/.openclaw/openclaw.json`, stdio + Streamable HTTP + SSE, headers, OAuth, tool filters) [O3]. Memory is a **plugin slot** (`plugins.slots.memory`, default `memory-core`), whose plugins provide `memory_search`/`memory_get` over Markdown files in the workspace, with "dreaming" consolidation into `MEMORY.md` [O4]. Plugins are TypeScript modules with `openclaw.plugin.json`, published via ClawHub or npm [O5].

memory-manager can be attached today as an MCP server (tier 1). A native memory plugin (tier 2) that replaces `memory-core` is possible, but it is TypeScript application code (outside the pool), and the memory capability API is in flux (deprecations dated 2026-10-01) [O1].

## MCP support

| Aspect | Finding | Source |
|---|---|---|
| Native client | Yes: `mcp.servers.<name>`; `openclaw mcp list/show/set/unset/login` | [O3], [O6] |
| Transports | stdio (`command`, `args`); remote `url` with `transport: "streamable-http"` or `"sse"` (`type: "http"` is normalised) | [O3] |
| Headers | `headers: { Authorization: "Bearer ${MCP_REMOTE_TOKEN}" }` | [O3] |
| Tool filter | `toolFilter.include` / `toolFilter.exclude`, exact names or `*` globs; also applies to the utility tools `resources_list`, `resources_read`, `prompts_list`, `prompts_get` | [O3] |
| Timeouts / sessions | `requestTimeoutMs`, `connectionTimeoutMs`; `mcp.sessionIdleTtlMs` idle eviction | [O3] |
| mcporter | Separate registry in `config/mcporter.json`, listed with `mcporter list`; not needed for native MCP | [O6] |
| Server `instructions` | **[unverified]** Not mentioned in the config or CLI docs | [O3], [O6] |
| OpenClaw as MCP server | `openclaw mcp serve` (stdio) exposes gateway conversations; not relevant here | [O6] |
| Recent fixes | 2026.8.35 "repaired remote MCP startup"; 2026.10.1-beta.1 synchronises MCP forms, files and context | [O1] |

## Auth

- **Static bearer:** through `headers` with `${ENV}` interpolation [O3].
- **OAuth:** `auth: "oauth"`, then `openclaw mcp login <name>` stores tokens [O3], [O6]. The `oauth` block has `scope`, `identity: "shared" | "per-requester"` and overrides for the redirect URL and client metadata URL (CIMD); the exact key names of those two are not documented on the page that was read [O3].
- **`oauth.identity: "per-requester"`** gives each requester its own token. It needs `gateway.publicOrigin` [O3]. This maps well onto memory-manager's per-user tokens and namespaces.
- DCR vs CIMD order, loopback redirect and headless behaviour: **[unverified]** (not described on the CLI page [O6]).

## Configuration

`~/.openclaw/openclaw.json` (JSON5). `openclaw config schema` prints the live JSON Schema [O3]. Generic example:

```json5
{
  mcp: {
    servers: {
      memory: {
        url: "https://memory.example.org/mcp",
        transport: "streamable-http",
        headers: { Authorization: "Bearer ${MEMORY_MANAGER_TOKEN}" },
        // or: auth: "oauth", oauth: { identity: "per-requester" }
        toolFilter: { exclude: ["memory_archive"] }
      }
    }
  }
}
```

## Built-in memory and extension points

- **Files (workspace, default `~/.openclaw/workspace`):** `MEMORY.md` (curated long-term, loaded at session start), optional `USER.md`, daily notes `memory/YYYY-MM-DD.md` (today and yesterday loaded on `/new`), optional `DREAMS.md` [O4].
- **Tools from the active memory plugin:** `memory_search` (hybrid search with embeddings), `memory_get` (file or line range), `intent` (standing intents) [O4].
- **Slot:** `plugins.slots.memory: "<plugin-id>" | "none"`; default `memory-core` (SQLite engine). Alternatives include `memory-lancedb`, Honcho; `memory-wiki` runs alongside the active plugin rather than replacing it [O3], [O4]. A gateway restart is needed after changes **[unverified, secondary source]** [O10].
- **Dreaming:** On by default; promotes short-term recall into `MEMORY.md` only past gates; untrusted and system-derived candidates are excluded. Turned off with `plugins.entries.memory-core.config.dreaming.enabled: false` [O4].
- **Compaction memory flush:** A silent turn before compaction asks the agent to save context; `agents.defaults.compaction.memoryFlush.enabled` [O4].
- **Plugin API relevant to memory:**
  - Entry: `definePluginEntry` from `openclaw/plugin-sdk/plugin-entry`; `api.registerTool(...)`; tools must also be listed in `contracts.tools` in `openclaw.plugin.json` [O5].
  - Hooks: `before_prompt_build` (auto-recall injection), `agent_end` (auto-capture), `session_end`, `gateway_stop`, `message_received` [O7].
  - Non-bundled plugins need `plugins.entries.<id>.hooks.allowConversationAccess: true` for conversation hooks; `hooks.allowPromptInjection: false` blocks prompt-mutating hooks [O3], [O7].
  - Claiming the slot: third-party plugins call `api.registerMemoryCapability({ promptBuilder })` **[secondary source]** [O11]. The official hooks page only lists this under deprecations and points to a migration guide [O7].
  - Hook context `ctx.agentId`, `ctx.sessionKey`, `ctx.runId` is optional and may be missing; the docs say missing fields do not prove a different sender [O7].
- **Reference implementation `memory-lancedb`:** tools `memory_recall`, `memory_store`, `memory_forget`; `autoRecall` via `before_prompt_build`, `autoCapture` via `agent_end` (max 3 memories per turn; rejects prompt-injection-like text and envelope metadata; incognito sessions skipped) [O8].

**Fit:** A tier-2 plugin would claim the memory slot, map `memory_search`/`memory_get` to memory-manager's `memory_search`/`memory_read`, and inject recalled notes in `before_prompt_build`. Auto-capture and dreaming write into `MEMORY.md` on their own; that contradicts "curated explicit writes" and would stay off or be routed through `if_version`.

## Instructions / skills

- `SKILL.md` with YAML frontmatter (`name`, `description`), AgentSkills spec; gating under `metadata.openclaw.requires` (bins, env, config, os) [O9].
- Locations, highest priority first: `<workspace>/skills`, `<workspace>/.agents/skills`, `~/.agents/skills`, `~/.openclaw/skills`, bundled, then plugin skills [O9].
- ClawHub: `openclaw skills install @owner/<slug>`; third-party skills are to be treated as untrusted code [O9].
- Practical path: a `memory-manager` skill that carries the `memory_guide` workflow, published to ClawHub; prompts reachable through `prompts_get`.

## Org/admin controls

None as a product. The security model assumes **one trusted operator boundary per gateway**; it is explicitly "not a hostile multi-tenant security boundary" [O12]. Controls: `plugins.allow`/`deny`, per-plugin hook permissions, `security.installPolicy` for skill installs, DM pairing and group allowlists with mention gating [O2], [O3], [O9], [O12].

## Availability

Self-hosted on any Node-capable host (macOS, Linux, Windows); install `npm install -g openclaw@latest`; MIT license [O14].

## Headless/CI usability

Good for a gateway daemon. OAuth requires an interactive `openclaw mcp login` at least once (manual fallback exists) [O6]; for unattended hosts a static bearer token is simpler.

## Proposed support level

- **Level: Full** for tier 1 — Streamable HTTP, static bearer and OAuth, tool filters, read and write.
- **Tier: 1 (MCP tool) now; tier 2 (memory-slot plugin) later, optional.** Tier 2 is TypeScript (pool deviation needing an owner decision), and the memory capability API is in transition (aliases due for removal on 2026-10-01 still pending [O1]).

## Security notes (third-party message injection)

- OpenClaw's own prompt-injection guide: "Prompt injection does not require public DMs"; web pages, e-mails and attachments carry it too. External content is wrapped with boundary markers; chat-template special tokens are stripped [O13].
- Group chats are supported, with mention gating and `contextVisibility`; in public rooms "anyone can post untrusted content" [O13].
- Agents with message-tool access can message across conversations by default [O12].
- Recommendations for operators: a read-only or narrowly scoped token for agents in groups; `toolFilter.exclude` for destructive tools; `per-requester` OAuth so each person writes only to their own namespace; never enable automatic capture into memory-manager from group contexts.

## Open questions

1. Is MCP server `instructions` surfaced to the model?
2. Which OAuth registration does `openclaw mcp login` use against an AS that offers both CIMD and DCR, and what redirect URI does it use?
3. Current (non-deprecated) API to claim the memory slot after the 2026-10-01 removals.
4. Is a TypeScript plugin acceptable as a pool deviation, given React/Vue are in the pool but Node server code is not?

## Sources

- [O1] Releases, https://github.com/openclaw/openclaw/releases (2026.10.1-beta.1, 2026.9.8, 2026.8.35), retrieved 2026-10-07
- [O2] README, https://github.com/openclaw/openclaw, retrieved 2026-10-07
- [O3] Configuration — MCP, skills and plugins, https://raw.githubusercontent.com/openclaw/openclaw/main/docs/gateway/config-extensions.md (site: https://docs.openclaw.ai/gateway/config-extensions), retrieved 2026-10-07
- [O4] Memory concept, https://raw.githubusercontent.com/openclaw/openclaw/main/docs/concepts/memory.md, retrieved 2026-10-07
- [O5] Building plugins, https://raw.githubusercontent.com/openclaw/openclaw/main/docs/plugins/building-plugins.md, retrieved 2026-10-07
- [O6] CLI `openclaw mcp`, https://raw.githubusercontent.com/openclaw/openclaw/main/docs/cli/mcp.md, retrieved 2026-10-07
- [O7] Plugin hooks, https://raw.githubusercontent.com/openclaw/openclaw/main/docs/plugins/hooks.md, retrieved 2026-10-07
- [O8] memory-lancedb, https://raw.githubusercontent.com/openclaw/openclaw/main/docs/plugins/memory-lancedb.md, retrieved 2026-10-07
- [O9] Skills, https://raw.githubusercontent.com/openclaw/openclaw/main/docs/tools/skills.md, retrieved 2026-10-07
- [O10] Third-party guide (Spanish) on plugins and memory slot, https://open-claw.bot/docs/es/tools/plugins (search snippet only), retrieved 2026-10-07
- [O11] agentmemory OpenClaw integration README, https://cdn.jsdelivr.net/gh/rohitg00/agentmemory@main/integrations/openclaw/README.md (search snippet only), retrieved 2026-10-07
- [O12] Security overview, https://raw.githubusercontent.com/openclaw/openclaw/main/docs/gateway/security/index.md, retrieved 2026-10-07
- [O13] Prompt injection, https://raw.githubusercontent.com/openclaw/openclaw/main/docs/gateway/security/prompt-injection.md, retrieved 2026-10-07
- [O14] LICENSE, https://raw.githubusercontent.com/openclaw/openclaw/main/LICENSE, retrieved 2026-10-07
