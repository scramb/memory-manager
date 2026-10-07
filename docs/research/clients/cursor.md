# Client: Cursor (IDE agent, Cursor CLI, cloud agents)

Retrieved: 2026-10-07 · Tested version: none (desk research; latest 3.18.25 per a third-party tracker, `[secondary]`)

**Source quality warning:** `cursor.com`, `docs.cursor.com` and `forum.cursor.com` were blocked by the research environment's egress proxy. Every Cursor statement below comes from **search-engine snippets of the official pages** (cursor.com/docs, cursor.com/help) or forum threads, not from a full read. `[secondary]` marks claims that only third-party or community sources support; `[unverified]` marks claims with no source. Re-check against the live docs before a decision depends on this file.

## Summary

Cursor is a first-class remote MCP client: `mcp.json` (project or global) takes a `url` with optional `headers`, OAuth runs automatically with Dynamic Client Registration, and a static `auth` block covers servers without DCR [CU1], [CU2]. The docs list Tools, Prompts, Resources, Roots, Elicitation and the Apps extension as supported [CU3]. CIMD is **not** supported as of a staff reply on the forum (undated in the snippet) [CU4]. Two known issues matter directly for memory-manager: (1) a forum bug report says Cursor **ignores a configured `Authorization` header when the server advertises OAuth discovery** [CU5], which is exactly what memory-manager does; (2) historic 40-tool and 60-character (server + tool name) limits [CU6], [CU7]. Headless use goes through the Cursor CLI (`agent`/`cursor-agent -p --approve-mcps`) with reported reliability problems in CI [CU8], [CU9].

## Transports

| Transport | Support | Source |
|---|---|---|
| stdio | Yes (`command`, `args`, `env`) | [CU1] |
| Streamable HTTP | Yes (`url`); docs describe remote entries as "HTTP or SSE" | [CU1] |
| SSE | Yes (legacy, same `url` key) | [CU1] |

How Cursor chooses between Streamable HTTP and SSE for a `url` entry is not stated in the snippets `[unverified]`.

## Auth

| Mechanism | Support | Source |
|---|---|---|
| OAuth with DCR | Yes; browser opens on first connect | [CU2], [CU4] |
| CIMD (Client ID Metadata Document) | **No**; staff: "supports OAuth via DCR and static credentials", no timeline for CIMD | [CU4] |
| Preregistered client | Yes: `auth` object with `CLIENT_ID`, optional `CLIENT_SECRET`, `scopes` | [CU2] |
| Static headers / bearer | Yes: `headers` object on remote entries | [CU1] |
| Env var substitution | `${env:NAME}`, `${workspaceFolder}`, `${userHome}` in `command`, `args`, `env`, `url`, `headers` (and `auth`) | [CU1], [CU10] `[secondary]` |
| Header ignored when OAuth discovery exists | Forum bug report (Cursor 2.6.21, Linux): Cursor tries OAuth first and never sends the configured `Authorization` header | [CU5] `[secondary]`, fix status `[unverified]` |
| Header interpolation bug | `${env:...}` in remote `headers` was sent literally; staff said fixed in a later release | [CU10] `[secondary]` |

OAuth redirect URI used by Cursor: `[unverified]` (a custom `cursor://` scheme is widely cited but not confirmed in an official snippet).

## Configuration

- Project: `.cursor/mcp.json`; global: `~/.cursor/mcp.json`; top-level key `mcpServers` [CU1].
- The Cursor CLI reads the same files with the same precedence (project → global → nested) [CU8] `[secondary]`.
- Cloud agents use servers configured in the Cloud Agents dashboard; Team admins configure shared servers under Dashboard → Integrations & MCP [CU11]. A forum report says config interpolation does not work for cloud-agent MCP servers [CU12] `[secondary]`.

Minimal remote server with a bearer header (token from the environment):

```json
{
  "mcpServers": {
    "memory": {
      "url": "https://memory.example.com/mcp",
      "headers": { "Authorization": "Bearer ${env:MEMORY_MANAGER_TOKEN}" }
    }
  }
}
```

OAuth variant: omit `headers`; Cursor discovers the AS and registers via DCR. Preregistered variant: add `"auth": { "CLIENT_ID": "${env:MM_CLIENT_ID}", "scopes": ["..."] }` [CU2].

## Limits

| Limit | Value | Source |
|---|---|---|
| Max tools | Historically 40 across all enabled servers (warning "some models may not respect more than 40 tools"); newer builds reportedly load tools on demand | [CU6] `[secondary]`, current state `[unverified]` |
| Tool name length | Server name + tool name ≤ 60 characters (staff: "a more conservative internal limit") | [CU7] `[secondary]` |
| Tool name charset | `[unverified]` | — |
| JSON Schema restrictions | None documented in Cursor; effective limits depend on the selected model provider `[unverified]` | — |

memory-manager impact: 7 tools; longest name `memory_supersede` (16 chars) leaves 44 characters for the server key. Fine.

## Instructions/prompts/resources

| Feature | Support | Source |
|---|---|---|
| Tools | Yes | [CU3] |
| Prompts | Yes (listed in "Protocol support") | [CU3]; older forum posts say otherwise [CU13] `[secondary]` |
| Resources | Yes (listed) | [CU3] |
| Roots, Elicitation, Apps | Yes (listed) | [CU3] |
| Server `instructions` | **No source found**; whether Cursor injects the `initialize` `instructions` into the agent context is `[unverified]` | — |

Consequence: the `memory_guide` prompt should be usable; the server `instructions` must not be the only place where the write rules live. A short Cursor rule or `AGENTS.md` snippet is the safe fallback.

## Instruction files

- `AGENTS.md` in the project root, picked up automatically; Cursor reads `CLAUDE.md` the same way [CU14].
- Project rules: `.cursor/rules/*.mdc` (modes: Always Apply, Apply Intelligently, Apply to Specific Files, Apply Manually) [CU14].
- User rules (settings, synced to the account) and Team rules (dashboard, Team/Enterprise only; precedence Team > Project > User) [CU14].
- `.cursorrules` is legacy [CU14].

## Org policies

- Enterprise: "MCP Configuration" in the team dashboard restricts which MCP servers members can use; entries are command or URL, with per-server tool controls and network policy [CU15].
- Allowlist hierarchy: team dashboard > managed `~/.cursor/permissions.json` (MDM) > editor approvals; higher sources replace, not merge. `mcpAllowlist` entries look like `server:tool`, e.g. `linear:*`, `*:search` [CU16].
- Staff confirmed that on Enterprise an MCP server must be on the admin allowlist before the CLI can call it, on top of local approval [CU9] `[secondary]`.

## Availability

- MCP in the IDE: plan restrictions not found; MCP appears to be available on all plans `[unverified]`.
- Team rules and the shared MCP dashboard: Team/Enterprise [CU11], [CU14]. Admin MCP allowlist: Enterprise [CU15].
- Cloud agents with MCP: supported via the dashboard; per-plan breakdown not found [CU11].

## Headless/CI

- Cursor CLI print mode: `agent -p --approve-mcps "<prompt>"` (binary also called `cursor-agent`) auto-approves MCP servers [CU8].
- Forum threads report that MCP approval in headless/CI is incomplete, that `--force --trust --approve-mcps` is needed, and that approval state lives in local files that do not carry over to a fresh CI machine [CU9] `[secondary]`.
- OAuth needs a browser; for CI use a static bearer header (but see the header-vs-OAuth bug above).

Verdict: possible, not reliable; not a good first target for the retrieval eval in CI.

## Proposed support level

**Partial** — tools, prompts and resources work over Streamable HTTP with OAuth (DCR) or a static bearer, but static bearer may be ignored while the server advertises OAuth metadata, `instructions` handling is undocumented, and there is no CIMD.

## Open questions

1. Does the "headers ignored when OAuth discovery exists" bug [CU5] still occur on 3.18.x? If so, the static-token path needs a separate endpoint or a switch to stop advertising `/.well-known/oauth-protected-resource` for that path. Must be tested.
2. Does Cursor pass server `instructions` to the model?
3. Is the 40-tool cap gone in current builds?
4. Exact OAuth redirect URI(s) and whether Cursor sends the RFC 8707 `resource` parameter.
5. Latest stable version from cursor.com/changelog (tracker shows 3.18.9 stable / 3.18.25 latest as of 2026-09-01 [CU17]).

## Sources

- [CU1] Cursor docs, Model Context Protocol, https://cursor.com/docs/mcp and https://cursor.com/docs/mcp.md (search snippets only), retrieved 2026-10-07
- [CU2] Cursor docs, MCP "Static OAuth" / `auth` object, https://cursor.com/docs/context/mcp (search snippets only), retrieved 2026-10-07
- [CU3] Cursor docs, MCP "Protocol support" table, https://cursor.com/docs/mcp (search snippet only), retrieved 2026-10-07
- [CU4] Forum, "MCP OAuth: CIMD Support Plans and Timelines", https://forum.cursor.com/t/mcp-oauth-cimd-support-plans-and-timelines/148096 (staff reply, search snippet only), retrieved 2026-10-07
- [CU5] Forum bug report, "MCP headers config ignored when server has OAuth discovery", https://forum.cursor.com/t/mcp-headers-config-ignored-when-server-has-oauth-discovery/156054 (search snippet only), retrieved 2026-10-07
- [CU6] Forum, "Tools limited to 40 total", https://forum.cursor.com/t/tools-limited-to-40-total/67976 and https://forum.cursor.com/t/mcp-server-40-tool-limit-in-cursor-is-this-frustrating-your-workflow/81627 (search snippets only), retrieved 2026-10-07
- [CU7] Forum, "PostHog MCP Tool Names Too Long", https://forum.cursor.com/t/posthog-mcp-tool-names-too-long/155408 and https://forum.cursor.com/t/google-gws-cli-tool-names-too-long/153918 (staff replies, search snippets only), retrieved 2026-10-07
- [CU8] Cursor docs, CLI MCP, https://cursor.com/docs/cli/mcp.md (search snippet only), retrieved 2026-10-07
- [CU9] Forum threads on headless MCP: https://forum.cursor.com/t/cursor-agent-p-mode-does-not-inject-mcp-server-tools-into-agent-context/155275, https://forum.cursor.com/t/mcp-servers-are-not-recognized-with-cursor-cli-in-a-ci-environment/138036, https://forum.cursor.com/t/cursor-cli-doesnt-work-with-jira-mcp-in-headless-mode/158269 (search snippets only), retrieved 2026-10-07
- [CU10] Forum, "Config interpolation ${env:NAME} not working in headers for remote MCP servers", https://forum.cursor.com/t/config-interpolation-env-name-not-working-in-headers-for-remote-mcp-servers/156069; Zuplo guide https://zuplo.com/docs/mcp-gateway/connect-clients/cursor (search snippets only), retrieved 2026-10-07
- [CU11] Cursor help, MCP, https://cursor.com/help/customization/mcp (search snippet only), retrieved 2026-10-07
- [CU12] Forum, "Cloud Agents + MCP", https://forum.cursor.com/t/cloud-agents-mcp/152033 (search snippet only), retrieved 2026-10-07
- [CU13] Forum, "Integrate MCP Prompts", https://forum.cursor.com/t/integrate-mcp-prompts/76065 (search snippet only), retrieved 2026-10-07
- [CU14] Cursor docs, Rules, https://cursor.com/docs/rules and https://cursor.com/help/customization/rules (search snippets only), retrieved 2026-10-07
- [CU15] Cursor docs, Model and integration management, https://cursor.com/docs/enterprise/model-and-integration-management (search snippet only), retrieved 2026-10-07
- [CU16] Cursor docs, Deployment patterns, https://cursor.com/docs/enterprise/deployment-patterns.md (search snippet only), retrieved 2026-10-07
- [CU17] Tech Dev Notes version tracker, https://techdevnotes.com/releases/version-tracker/20260901-064503Z-828772ba6d81 (search snippet only, third party), retrieved 2026-10-07
