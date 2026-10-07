# Client: Google Antigravity (Antigravity 2.0 desktop/IDE, Antigravity CLI `agy`)

Retrieved: 2026-10-07 · Tested version: none (desk research; latest Antigravity CLI 1.3.1 per its changelog, desktop version `[unverified]`)

**Source quality warning:** `antigravity.google`, `codelabs.developers.google.com` and `developers.google.com` were blocked by the egress proxy. Primary facts come from the **Antigravity CLI changelog** in `google-antigravity/antigravity-cli` (raw GitHub, fetched 2026-10-07; entries are undated), GitHub's own Antigravity install guide for the GitHub MCP server, and search snippets of the official docs and Google Codelabs. Antigravity itself is closed source. `[secondary]` = third-party only; `[unverified]` = no source.

## Summary

Antigravity is Google's agent IDE ("Antigravity 2.0") plus a terminal client `agy`; both share one agent engine and settings [AG1]. Reports say `agy` replaced Gemini CLI for individual users on 2026-06-18 [AG12] `[secondary]` (see gemini.md). MCP servers are configured in `mcp_config.json` under `mcpServers`, remote servers with `serverUrl` (now also `url`) and optional `headers` [AG2], [AG3]. OAuth supports DCR and, since a 1.x CLI release, **CIMD** [AG3]. Admin controls can block MCP servers (Gemini Enterprise allowlist) [AG3], [AG7]. `agy -p` runs headless with JSON output [AG3]. Undocumented: server `instructions` and MCP prompts. A community report gives a **100-tool limit per MCP server** in the IDE [AG8] `[secondary]`.

## Transports

| Transport | Support | Source |
|---|---|---|
| stdio | Yes (`command`, `args`, `env`) | [AG2] |
| Streamable HTTP | Yes, via `serverUrl` (CLI also accepts `url`) | [AG2], [AG3] |
| SSE | Reportedly yes via `serverUrl` ("Streamable HTTP, SSE, or websocket") | [AG9] `[secondary]` |

How the client picks Streamable HTTP vs SSE for one `serverUrl` is `[unverified]`.

## Auth

| Mechanism | Support | Source |
|---|---|---|
| OAuth + DCR | Yes (changelog fixes DCR responses with HTTP 200 instead of 201) | [AG3] |
| OAuth + CIMD | Yes: "Added support for OAuth client ID metadata documents … no longer require a manually supplied client ID or dynamic client registration" (CLI) | [AG3] |
| Preregistered client | `oauth: { clientId, clientSecret }` in `mcp_config.json`; redirect URI for own clients `https://antigravity.google/oauth-callback` | [AG4] (search snippet), [AG10] `[secondary]` |
| Static headers / bearer | `headers` object next to `serverUrl` | [AG2] |
| Google credentials | Servers can be configured for Google ADC (CLI) | [AG3] |
| Env var substitution | `[unverified]` — no source shows `${VAR}` in `mcp_config.json`; `agy mcp add --header` writes literal values | [AG3] |
| OAuth in headless | Auth code can be pasted via the controlling terminal in `-p`; "truly headless runs fail fast" | [AG3] |
| Relaxed issuer validation | Issuer validation relaxed for non-strict providers; `refresh_token` grant included | [AG3] |

## Configuration

- IDE: `~/.gemini/antigravity/mcp_config.json` (Agent panel → … → MCP Servers → Manage → View raw config) [AG2].
- Shared config for Antigravity 2.0, IDE and CLI: `~/.gemini/config/mcp_config.json` (the CLI migrated to this path) [AG5], [AG3]. Which path a given desktop build reads should be checked in its MCP panel.
- Project-level MCP config: `[unverified]`.
- CLI: `agy mcp add|remove|list|enable|disable` with `--type`, `--env`, `--header` (user-level file) [AG3].
- The file accepts `//` and `/* */` comments and trailing commas (CLI) [AG3].

```json
{
  "mcpServers": {
    "memory": {
      "serverUrl": "https://memory.example.com/mcp",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" }
    }
  }
}
```

OAuth variant: omit `headers`; the client discovers the AS (CIMD or DCR).

## Limits

| Limit | Value | Source |
|---|---|---|
| Tools per MCP server (IDE) | 100; larger servers are rejected | [AG8] `[secondary]` (forum + vendor docs) |
| Tools per session (CLI) | Per-session declaration limit was raised; value not stated | [AG3] |
| Rules budget | Rules get a dedicated 20,000-token budget; oversized rules are cut | [AG3] |
| Tool schema | CLI validates arguments strictly against the server schema (undeclared args rejected; open objects preserved) | [AG3] |
| Model-side schema | When a Gemini model runs, the Gemini function-calling subset applies (see gemini.md) `[unverified]` for Antigravity specifically | — |
| Tool name length/charset | `[unverified]`; plugin servers are namespaced `<plugin>_<server>` | [AG3] |

## Instructions/prompts/resources

| Feature | Support | Source |
|---|---|---|
| Tools | Yes | [AG2] |
| Resources | Yes (`read_resource` handles text, image and binary blobs; embedded resources in tool results surface) | [AG3] |
| Prompts | `[unverified]` — no changelog entry or doc snippet found | — |
| Server `instructions` | `[unverified]` | — |
| Progress notifications | Yes (fixed in CLI) | [AG3] |

Consequence: put the memory rules into `AGENTS.md`/`GEMINI.md` or a global rule for Antigravity.

## Instruction files

- Global: `~/.gemini/AGENTS.md` or `~/.gemini/GEMINI.md` (or under `~/.gemini/config/`), always on; modular `~/.gemini/config/rules/*.md` with frontmatter [AG6], [AG3].
- Workspace: `AGENTS.md`, `GEMINI.md`, `.agents/rules/*.md` (frontmatter `trigger` required) in the root or subdirectories; legacy `.agent/rules/*.md` still read [AG6].

## Org policies

- Admin controls apply to MCP servers in the CLI (changelog fix: controls were skipped at startup for five minutes) [AG3].
- Gemini Enterprise: admins enable AI developer tools and keep a **central MCP server allowlist**; unapproved servers show "Blocked by Admin Policy" [AG7].
- Business sign-in for Gemini Enterprise (GE-Standard / GE-Plus seats), with admin control [AG3].

## Availability

- Individual free tier with weekly rate limit; Google AI Pro/Ultra get higher limits refreshed every five hours [AG11].
- Organisations: Gemini Enterprise seats or Google Cloud consumption [AG3], [AG13] `[secondary]`.
- Antigravity CLI is not open source (the repo hosts README, changelog and issues) [AG1], [AG12] `[secondary]`.

## Headless/CI

- `agy -p "<prompt>"` / `--prompt`, `--output-format text|json|stream-json`, `--print-timeout`, `--mode`; exit code 3 plus an `AGY_ERROR` JSON line on agent/model failure [AG3].
- Headless runs block until MCP servers are loaded, so the scripted turn sees all tools [AG3].
- Tools needing confirmation are soft-denied in `-p` and reported as `denied_actions`; persisted `settings.json` permissions apply; `always-proceed` auto-approves MCP calls [AG3].
- A GitHub issue reported `agy -p` hanging when stdout is piped (v1.0.6); later changelog entries fix several pipe-related hangs [AG14], [AG3].
- Auth in CI: `GEMINI_API_KEY` sessions are supported [AG3]; MCP auth should be a static bearer header.

## Proposed support level

**Partial** — remote Streamable HTTP with bearer header or OAuth (CIMD/DCR) and a usable headless mode are documented, but server `instructions` and MCP prompts are undocumented, env-var substitution for secrets is unverified, and the config path is in flux.

## Open questions

1. Does Antigravity read server `instructions` and expose MCP prompts?
2. Can `mcp_config.json` reference environment variables, so CI does not need a literal token in the file?
3. Is there a project-scoped MCP config file?
4. Which `client_id` URL does Antigravity publish for CIMD, and which redirect URIs does it list (`https://antigravity.google/oauth-callback` for IDE, loopback for CLI)?
5. Which models (Gemini only, or others) can drive MCP tools, and therefore which schema subset applies?

## Sources

- [AG1] `google-antigravity/antigravity-cli` README, https://raw.githubusercontent.com/google-antigravity/antigravity-cli/main/README.md, retrieved 2026-10-07
- [AG2] `github/github-mcp-server`, "Installing GitHub MCP Server in Antigravity", https://raw.githubusercontent.com/github/github-mcp-server/main/docs/installation-guides/install-antigravity.md, retrieved 2026-10-07
- [AG3] `google-antigravity/antigravity-cli` CHANGELOG.md (versions 1.0.0–1.3.1; entries undated), https://raw.githubusercontent.com/google-antigravity/antigravity-cli/main/CHANGELOG.md, retrieved 2026-10-07
- [AG4] Google Workspace developer docs, "Configure the Google Workspace MCP servers", https://developers.google.com/workspace/guides/configure-mcp-servers (search snippet only), retrieved 2026-10-07
- [AG5] Google Codelab, "Google Workspace MCP servers in Google Antigravity 2.0, IDE, and/or CLI", https://codelabs.developers.google.com/google-workspace-mcp-antigravity (search snippet only), retrieved 2026-10-07
- [AG6] Antigravity docs, Rules, https://antigravity.google/docs/rules/ and https://antigravity.google/docs/rules-workflows (search snippets only), retrieved 2026-10-07
- [AG7] Google Codelab, "Google Workspace MCP servers with Antigravity in Gemini Enterprise", https://codelabs.developers.google.com/google-workspace-mcp-antigravity-ge (search snippet only), retrieved 2026-10-07
- [AG8] Google AI Developers Forum, "Antigravity IDE Hardcoded MCP Tool Limit (100)", https://discuss.ai.google.dev/t/critical-dx-issue-antigravity-ide-hardcoded-mcp-tool-limit-100-configuration-fragmentation-with-gemini-cli/143969; Iterable MCP PR https://github.com/Iterable/mcp-server/pull/20/files (search snippets only), retrieved 2026-10-07
- [AG9] Arcade blog, "How to Connect MCP Servers to Google Antigravity (2026)", https://www.arcade.dev/blog/how-to-connect-mcp-antigravity/ (search snippet only), retrieved 2026-10-07
- [AG10] CloudBees docs, "Connect Google Antigravity", https://docs.cloudbees.com/docs/cloudbees-unify/latest/unify-ai/how-to-guides/connect-google-antigravity; WikiDocs https://wikidocs.net/312519 (search snippets only), retrieved 2026-10-07
- [AG11] Google blog, "New Antigravity rate limits for Pro and Ultra subscribers", https://blog.google/feed/new-antigravity-rate-limits-pro-ultra-subsribers/ (search snippet only), retrieved 2026-10-07
- [AG12] Third-party reports on the Gemini CLI → Antigravity CLI switch, e.g. https://www.shopifreaks.com/google-ends-free-gemini-cli-access-for-non-enterprise-users-on-june-18-replacing-it-with-closed-source-antigravity-cli/ (search snippet only), retrieved 2026-10-07
- [AG13] Third-party pricing guides, e.g. https://www.codeagentswarm.com/en/guides/antigravity-plans-and-pricing (search snippet only), retrieved 2026-10-07
- [AG14] `google-antigravity/antigravity-cli` issue #318 (`agy -p` hangs when piped), https://github.com/google-antigravity/antigravity-cli/issues/318 (search snippet only), retrieved 2026-10-07
- [AG15] Antigravity docs, CLI headless, https://antigravity.google/docs/cli/headless (search snippet only), retrieved 2026-10-07
