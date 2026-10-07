# Client: GitHub Copilot (VS Code, JetBrains, Copilot CLI, Copilot cloud agent)

Retrieved: 2026-10-07 · Tested version: none (desk research; latest VS Code 1.141 released 2026-10-07, Copilot CLI 1.0.93 on npm, JetBrains plugin version `[unverified]`)

**Method:** `docs.github.com` and `code.visualstudio.com` were blocked by the egress proxy, so the official docs were read **from their source repositories** on `raw.githubusercontent.com` (`github/docs` and `microsoft/vscode-docs`, branch `main`, fetched 2026-10-07). These are the files the sites are built from. "Copilot coding agent" is now called **Copilot cloud agent** in the docs [GH5].

## Summary

Copilot's MCP support differs a lot by surface:

- **VS Code** is the most complete MCP client in this set: Streamable HTTP with SSE fallback, OAuth with **CIMD (preferred) → DCR → manual client id**, static headers with `${input:…}` secrets, and support for tools, prompts, resources, elicitation, sampling, roots and **server instructions** [VS1], [VS2], [VS3], [VS4].
- **JetBrains** supports local and remote servers with OAuth or PAT; the documented remote example uses `requestInit.headers` [GH3].
- **Copilot CLI** reads `~/.copilot/mcp-config.json` plus project `.mcp.json`/`.github/mcp.json`, supports headers with `${VAR}` expansion, OAuth (incl. a headless `client_credentials` grant), and runs headless with `copilot -p` [GH6], [GH7].
- **Copilot cloud agent** (and code review) supports **tools only**, **no OAuth**, static headers from `COPILOT_MCP_*` secrets, and needs an explicit `tools` allowlist [GH4], [GH5].

Enterprise control: the "MCP servers in Copilot" policy (off by default for Business/Enterprise) plus an allowlist/denylist in the enterprise managed settings file [GH1], [GH2].

## Transports

| Surface | stdio | Streamable HTTP | SSE | Source |
|---|---|---|---|---|
| VS Code | Yes | Yes (`"type": "http"`, tried first) | Yes (fallback or `"type": "sse"`); also Unix sockets / named pipes | [VS2] |
| JetBrains | Yes | Yes | `[unverified]` | [GH3] |
| Copilot CLI | Yes (`local`/`stdio`) | Yes (`http`, alias `streamable-http`) | Yes (`sse`, deprecated) | [GH7] |
| Cloud agent | Yes (`local`/`stdio`) | Yes (`http`) | Yes (`sse`) | [GH4] |

## Auth

| Mechanism | VS Code | JetBrains | Copilot CLI | Cloud agent |
|---|---|---|---|---|
| OAuth + DCR | Yes [VS3] | Yes ("OAuth or PAT") [GH8] | Yes (default) [GH7] | **No** [GH5] |
| OAuth + CIMD | Yes, preferred since 1.106 [VS5] | `[unverified]` | `[unverified]` | No |
| Preregistered client id | Yes: `oauth.clientId` in config, or prompted when DCR is missing [VS2], [VS3] | `[unverified]` | `oauthClientId`, `oauthScopes`, `oauthPublicClient` [GH7] | No |
| Headless OAuth | — | — | `oauthGrantType: "client_credentials"` (confidential client, secret in keychain) [GH7] | — |
| Static headers / bearer | `headers` [VS2] | `requestInit.headers` [GH3] | `headers` [GH7] | `headers` [GH4] |
| Env/secret substitution | `${input:id}` (prompted, stored securely), predefined variables, `envFile` for stdio; generic `${env:…}` in headers `[unverified]` | `[unverified]` | `$VAR`, `${VAR}`, `${VAR:-default}` [GH7] | `$COPILOT_MCP_X`, `${COPILOT_MCP_X}`, `${…:-default}`; only names prefixed `COPILOT_MCP_` [GH4] |
| Enterprise SSO | `oauth.enterpriseManaged` (ID-JAG via `mcp.enterpriseManagedAuth.idp`, preview) [VS2] | — | Entra broker / OIDC token injection [GH7] | — |

## Configuration

VS Code: `.vscode/mcp.json` (key `servers`), workspace-root `.mcp.json` (key `mcpServers`, "portable"), user profile `mcp.json`, or `~/.copilot/mcp-config.json`; VS Code now recommends the portable files [VS1], [VS2].

```json
{
  "inputs": [
    { "type": "promptString", "id": "mm-token", "description": "memory-manager token", "password": true }
  ],
  "servers": {
    "memory": {
      "type": "http",
      "url": "https://memory.example.com/mcp",
      "headers": { "Authorization": "Bearer ${input:mm-token}" }
    }
  }
}
```

Note: the Agent Host does not receive servers that need `${input:…}` [VS1].

JetBrains (`mcp.json` opened from Copilot Chat → Agent → tools icon → Add MCP Tools; file location `[unverified]`) [GH3]:

```json
{
  "servers": {
    "memory": {
      "url": "https://memory.example.com/mcp",
      "requestInit": { "headers": { "Authorization": "Bearer YOUR_TOKEN" } }
    }
  }
}
```

Copilot CLI (`~/.copilot/mcp-config.json`, or project `.mcp.json` / `.github/mcp.json`) [GH6], [GH7]:

```json
{
  "mcpServers": {
    "memory": {
      "type": "http",
      "url": "https://memory.example.com/mcp",
      "headers": { "Authorization": "Bearer ${MEMORY_MANAGER_TOKEN}" },
      "tools": ["*"]
    }
  }
}
```

Cloud agent: repository Settings → Copilot → MCP servers (JSON, same `mcpServers` shape); secret stored as Agents secret `COPILOT_MCP_MEMORY_TOKEN` and referenced as `"Authorization": "Bearer $COPILOT_MCP_MEMORY_TOKEN"`; `tools` is **required** [GH4].

## Limits

| Limit | Value | Source |
|---|---|---|
| Max tools per request (VS Code) | 128 ("Cannot have more than 128 tools per request") | [VS6] |
| Tool name length/charset | Not documented for MCP tools in VS Code `[unverified]`; depends on the selected model | — |
| JSON Schema | Not documented `[unverified]` | — |
| Cloud agent tools | Must be listed in `tools` (or `"*"`); tools run **without approval** | [GH4] |
| Code review | Only tools with `annotations.readOnlyHint: true` are used | [GH4] |
| CLI timeout | Default 30,000 ms per server, connection budget floored at 60,000 ms | [GH7] |

## Instructions/prompts/resources

| Feature | VS Code | Copilot CLI | Cloud agent | Source |
|---|---|---|---|---|
| Tools | Yes | Yes | Yes | [VS4], [GH5] |
| Prompts | Yes, as `/mcp.<server>.<prompt>` | `[unverified]` | **No** | [VS4], [GH5] |
| Resources | Yes (Add Context → MCP Resources, templates) | `[unverified]` | **No** | [VS4], [GH5] |
| Server instructions | Yes ("Server instructions" listed as supported) | Allowlisted servers' instructions go into the system prompt up front, others on demand; `--allow-all-mcp-server-instructions` to include all | `[unverified]` | [VS4], [GH7] |
| Elicitation, sampling, roots, MCP Apps | Yes | `[unverified]` | No | [VS4], [VS1] |

JetBrains: prompts/resources/instructions `[unverified]`.

## Instruction files

Per the official support matrix [GH9]:

- `.github/copilot-instructions.md` (repository-wide): all surfaces.
- `.github/instructions/**/*.instructions.md` (path-specific): VS Code, JetBrains, Xcode, CLI, cloud agent, code review.
- `AGENTS.md`: VS Code chat (`AGENTS.md` only); VS Code cloud-agent sessions, JetBrains, Eclipse, Xcode, CLI and cloud agent read `AGENTS.md`, `CLAUDE.md` or `GEMINI.md`.
- Personal and organization instructions on github.com; organization instructions also apply to the cloud agent.

## Org policies

- **"MCP servers in Copilot" policy**: disabled by default; applies only to Copilot Business/Enterprise seats from the configuring org/enterprise. Free, Pro, Pro+ and Max users are not governed by it [GH1].
- **Allowlist/denylist** (`allowedMcpServers` / `deniedMcpServers`) in the enterprise managed settings file, stored in a `.github-private` repo or deployed via MDM; match by name, `serverUrl` (wildcards) or `serverCommand`. Order: built-ins always allowed → deny → allow → block servers whose URL/command contains an unresolved `${VAR}`. A malformed list blocks everything except built-ins [GH2].
- **Custom MCP registry** with "Restrict MCP access to registry servers": public preview, weaker (name/ID match) [GH10].
- VS Code device policies: `ChatMCP` (`chat.mcp.access`), `ChatAllowedMcpServers`, `ChatDeniedMcpServers`, `ChatAllowManagedMcpServersOnly`, `McpGalleryServiceUrl`, `McpEnterpriseManagedAuthIdp` [VS7].

memory-manager impact: an allowlist entry like `{ "serverUrl": "https://memory.example.com/mcp" }` works; a URL containing `${VAR}` would be blocked by rule 4.

## Availability

- MCP in IDEs and CLI: all Copilot plans incl. Free (the policy only gates Business/Enterprise) [GH1].
- Cloud agent: all paid plans (Pro, Pro+, Business, Enterprise; Max listed on one page); Business/Enterprise need the policy enabled [GH11] (search snippets).
- VS Code ≥ 1.99 for MCP; CIMD since 1.106 [GH3], [VS5].

## Headless/CI

- **Copilot CLI**: `copilot -p "<prompt>"` with `--allow-tool='memory'` (or `--allow-all-tools`, required for programmatic use); `--additional-mcp-config @file.json` adds a server for one run; project MCP files load in `-p` only in trusted dirs or with `GITHUB_COPILOT_PROMPT_MODE_WORKSPACE_MCP=true` [GH6], [GH7]. Static bearer via `${VAR}` header works without a browser.
- **Cloud agent**: runs unattended on GitHub Actions runners by design; MCP tools run without approval [GH4].
- VS Code / JetBrains: interactive only.

## Proposed support level

**Full** in VS Code and Copilot CLI (OAuth incl. CIMD/DCR or bearer, instructions, prompts, resources, headless CLI); **Partial** for the cloud agent (tools only, static bearer from `COPILOT_MCP_*` secret, no OAuth) and JetBrains (remote + PAT documented, feature depth unverified).

## Open questions

1. Does the cloud agent's firewall block outbound calls to a self-hosted MCP host, or are MCP servers started outside it? The docs only mention adding `api.github.com` for the GitHub MCP server [GH4].
2. JetBrains: config file location, CIMD, prompts and instructions support, current plugin version.
3. Does the Copilot CLI treat memory-manager's `instructions` as "allowlisted" by default, or only when the enterprise allowlist names it?
4. Cloud agent: is `instructions` passed to the model at all?

## Sources

- [GH1] `github/docs` `data/reusables/copilot/mcp/mcp-policy.md`, https://raw.githubusercontent.com/github/docs/main/data/reusables/copilot/mcp/mcp-policy.md, retrieved 2026-10-07
- [GH2] GitHub Docs, "Configuring an MCP server allowlist for your enterprise", https://docs.github.com/en/copilot/how-tos/administer-copilot/manage-mcp-usage/configure-enterprise-allowlist (source: `github/docs` `content/copilot/how-tos/administer-copilot/manage-mcp-usage/configure-enterprise-allowlist.md`), retrieved 2026-10-07
- [GH3] GitHub Docs, "Extending Copilot Chat with MCP" (VS Code / Visual Studio / JetBrains / Xcode / Eclipse tabs) and reusable `mcp-chat-json-snippet-for-other-ides-remote.md`, https://docs.github.com/en/copilot/how-tos/copilot-in-your-ide/customize-copilot/extend-copilot-with-tools-and-context/extend-copilot-chat-with-mcp (source: `github/docs`), retrieved 2026-10-07
- [GH4] GitHub Docs, "Configure MCP servers for your repository" (cloud agent and code review), https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/configure-mcp-servers (source: `github/docs`), retrieved 2026-10-07
- [GH5] GitHub Docs, "Model Context Protocol (MCP) and GitHub Copilot cloud agent" and reusable `repo-mcp-limitations.md`, https://docs.github.com/en/copilot/concepts/agents/cloud-agent/mcp-and-cloud-agent (source: `github/docs`), retrieved 2026-10-07
- [GH6] GitHub Docs, "Adding MCP servers for GitHub Copilot CLI", https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers (source: `github/docs`), retrieved 2026-10-07
- [GH7] GitHub Docs, "GitHub Copilot CLI command reference" (MCP server configuration, OAuth fields, env vars, `-p`), https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference (source: `github/docs`), retrieved 2026-10-07
- [GH8] GitHub Docs, "About Model Context Protocol (MCP)", https://docs.github.com/en/copilot/concepts/context/mcp (source: `github/docs` `content/copilot/concepts/context/mcp.md`), retrieved 2026-10-07
- [GH9] GitHub Docs, "Support for different types of custom instructions", https://docs.github.com/en/copilot/reference/custom-instructions-support (source: `github/docs`), retrieved 2026-10-07
- [GH10] GitHub Docs, "MCP server usage in your company", https://docs.github.com/en/copilot/concepts/enterprise/mcp-management (source: `github/docs`), retrieved 2026-10-07
- [GH11] GitHub Docs, "About GitHub Copilot cloud agent" and troubleshooting page, https://docs.github.com/en/copilot/concepts/agents/cloud-agent/about-cloud-agent (search snippets only), retrieved 2026-10-07
- [GH12] npm registry, `@github/copilot` (latest 1.0.93, 2026-10-07T14:09Z), https://registry.npmjs.org/@github/copilot, retrieved 2026-10-07
- [VS1] VS Code docs, "Add and manage MCP servers", https://code.visualstudio.com/docs/agent-customization/mcp-servers (source: `microsoft/vscode-docs` `docs/agent-customization/mcp-servers.md`, DateApproved 10/7/2026), retrieved 2026-10-07
- [VS2] VS Code docs, "MCP configuration reference", https://code.visualstudio.com/docs/agents/reference/mcp-configuration (source: `microsoft/vscode-docs` `docs/agents/reference/mcp-configuration.md`), retrieved 2026-10-07
- [VS3] VS Code API docs, "MCP developer guide" §Authorization, https://code.visualstudio.com/api/extension-guides/ai/mcp (source: `microsoft/vscode-docs` `api/extension-guides/ai/mcp.md`), retrieved 2026-10-07
- [VS4] same as [VS3], feature list (tools, prompts, resources, elicitation, sampling, server instructions, roots), retrieved 2026-10-07
- [VS5] VS Code 1.106 release notes, "Authentication: Client ID Metadata Document authentication flow"; 1.107 notes on 2025-11-25 spec support, https://code.visualstudio.com/updates/v1_106 (source: `microsoft/vscode-docs` `release-notes/v1_106.md`, `v1_107.md`), retrieved 2026-10-07
- [VS6] VS Code docs, "Use tools with agents" FAQ (128 tools per request), https://code.visualstudio.com/docs/agents/run/tools (source: `microsoft/vscode-docs` `docs/agents/run/tools.md`), retrieved 2026-10-07
- [VS7] VS Code docs, "Enterprise policies", https://code.visualstudio.com/docs/enterprise/policies (source: `microsoft/vscode-docs` `docs/enterprise/policies.md`), retrieved 2026-10-07
- [VS8] VS Code 1.141 release notes ("Release date: October 7, 2026"), https://code.visualstudio.com/updates/v1_141 (source: `microsoft/vscode-docs` `release-notes/v1_141.md`), retrieved 2026-10-07
