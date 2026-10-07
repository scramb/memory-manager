# Client: Gemini CLI and Gemini Code Assist (agent mode)

Retrieved: 2026-10-07 · Tested version: none (desk research; latest Gemini CLI v0.63.0 stable, released 2026-10-06; Code Assist IDE extension version `[unverified]`)

**Method:** Gemini CLI is open source (Apache-2.0). Facts were read from its **docs and source** in `google-gemini/gemini-cli` on `raw.githubusercontent.com` (branch `main`, fetched 2026-10-07) and npm. `geminicli.com`, `cloud.google.com/gemini` (redirects to the blocked `docs.cloud.google.com`) and `developers.google.com` were blocked by the egress proxy; Code Assist and Gemini API facts come from search snippets. `[secondary]` = third-party only; `[unverified]` = no source.

## Summary

Gemini CLI is a complete MCP client: stdio, SSE and Streamable HTTP; `headers` with `$VAR` expansion anywhere in `settings.json`; automatic OAuth with discovery and **DCR** (no CIMD in the source); tools, **prompts as slash commands**, resources via `@server://…`, roots, and **server `instructions` appended to the system prompt** [GE1], [GE2], [GE3]. It sanitizes tool names (63 chars, `[A-Za-z0-9_.:-]`) and strips schema keywords for the Gemini API [GE1]. Headless runs use `gemini -p` with JSON output [GE4]. Admins get `mcp.allowed` in system settings and immutable Enterprise Admin Controls with an MCP allowlist [GE5], [GE6]. Gemini Code Assist agent mode in VS Code reads the same `~/.gemini/settings.json` [GE8].

**Availability caveat:** multiple third-party reports say Google stopped serving Gemini CLI and Code Assist IDE requests for free, Google AI Pro and Ultra individual accounts on **2026-06-18** and pointed individuals to the Antigravity CLI, while Code Assist Standard/Enterprise keep access [GE10] `[secondary]`. The repo's own quota page and README still describe the free tier [GE9]. Treat Gemini CLI as an **enterprise (Code Assist Standard/Enterprise, Vertex, API key)** client until confirmed.

## Transports

| Config key | Transport | Source |
|---|---|---|
| `command` | stdio | [GE1] |
| `httpUrl` | Streamable HTTP | [GE1] |
| `url` + `"type": "http"` | Streamable HTTP | [GE3] |
| `url` + `"type": "sse"` | SSE | [GE3] |
| `url` without `type` | Docs say SSE; **source** creates a Streamable HTTP transport and falls back to SSE on 404 | [GE1] vs [GE3] |

Recommendation for memory-manager docs: use `httpUrl` (unambiguous).

## Auth

| Mechanism | Support | Source |
|---|---|---|
| OAuth discovery on 401 | Yes (`WWW-Authenticate`, protected-resource and AS metadata) | [GE1], [GE3] |
| DCR | Yes ("Perform dynamic client registration if supported"; public client, `token_endpoint_auth_method: none`) | [GE1], [GE2] |
| CIMD | **No** — no `client_id_metadata_document` handling in `oauth-provider.ts`/`oauth-utils.ts` | [GE2] |
| Preregistered client | `oauth: { enabled, clientId, clientSecret, authorizationUrl, tokenUrl, issuer, scopes, redirectUri, audiences }` | [GE1] |
| RFC 9207 `iss` | Validated: if AS metadata says `authorization_response_iss_parameter_supported: true`, the callback **must** carry a matching `iss` | [GE1] |
| Redirect | `http://localhost:<random-port>/oauth/callback` (or fixed `redirectUri`); needs a local browser — no headless OAuth | [GE1] |
| Static headers / bearer | `headers` on `url`/`httpUrl` entries; `gemini mcp add -H "Authorization: Bearer …"` | [GE1] |
| Env var substitution | `$VAR`, `${VAR}`, `${VAR:-default}` in **any string** in `settings.json` (resolved at load) | [GE6] |
| Google ADC / SA impersonation | `authProviderType: google_credentials` / `service_account_impersonation` | [GE1] |
| Token store | `~/.gemini/mcp-oauth-tokens.json`, refreshed automatically | [GE1] |

memory-manager impact: if the AS metadata advertises `authorization_response_iss_parameter_supported: true`, the `/authorize` redirect must include `iss`. Check before claiming Gemini support.

## Configuration

Locations (highest precedence last): system defaults → `~/.gemini/settings.json` (user) → `.gemini/settings.json` (project) → system settings `/etc/gemini-cli/settings.json` (Linux; Windows/macOS equivalents) [GE6]. `gemini mcp add -s user|project` writes the file [GE1].

```json
{
  "mcpServers": {
    "memory": {
      "httpUrl": "https://memory.example.com/mcp",
      "headers": { "Authorization": "Bearer ${MEMORY_MANAGER_TOKEN}" },
      "timeout": 30000
    }
  }
}
```

Server alias must not contain `_` (policy parser splits FQNs on the first underscore) [GE1]: use `memory` or `memory-manager`.

Gemini Code Assist: VS Code agent mode uses `~/.gemini/settings.json` (`mcpServers`), then "Developer: Reload Window"; IntelliJ uses a separate `mcp.json` in the IDE configuration directory (exact path `[unverified]`) [GE8].

## Limits

| Limit | Value | Source |
|---|---|---|
| Tool name | FQN `mcp_{server}_{tool}`; characters other than `A-Za-z0-9_-.:` replaced with `_`; names > 63 chars truncated in the middle | [GE1] |
| Schema sanitizing | `$schema` and `additionalProperties` removed; `default` removed inside `anyOf` (Vertex compatibility); recursive | [GE1] |
| Gemini API schema | OpenAPI 3.0 subset (`type`, `description`, `enum`, `items`, `properties`, `required`, `nullable`); `$ref`/`$defs` reportedly rejected; empty-object `properties` rejected | [GE11] (Vertex reference) + `[secondary]` |
| Function declarations | 128 per request (Vertex AI reference) | [GE11] |
| Max tools (CLI) | Feature request to raise "MCP tool limit from 100 to 500" exists | [GE12] `[secondary]` |
| Timeout | Default 600,000 ms per request | [GE1] |

memory-manager impact: FQNs like `mcp_memory_memory_supersede` (27 chars) fit. Optional params rendered as `anyOf: [{type: string}, {type: null}]` with `default: null` lose the default; that is harmless. Avoid `$ref` in **input** schemas.

## Instructions/prompts/resources

| Feature | Support | Source |
|---|---|---|
| Tools | Yes | [GE1] |
| Server `instructions` | Yes — "appended to the system instructions" | [GE1], [GE3] (`getInstructions()`) |
| Prompts | Yes — each prompt becomes a slash command, e.g. `/memory_guide` | [GE1] |
| Resources | Yes — `resources/list` at discovery, `@server://path` in chat calls `resources/read` | [GE1] |
| Roots | Yes (`roots/list` handler) | [GE3] |
| Rich content | text, image, audio, `resource`, `resource_link` blocks | [GE1] |

Code Assist agent mode: tools confirmed; instructions/prompts `[unverified]` (it is powered by Gemini CLI per the docs snippet, so likely the same).

## Instruction files

`GEMINI.md`, hierarchical (global `~/.gemini/GEMINI.md`, project root and parents, subdirectories); file names configurable via `context.fileName`, e.g. `["AGENTS.md", "GEMINI.md"]` [GE6], [GE7]. `AGENTS.md` is **not** read by default.

## Org policies

- System settings: `mcp.allowed` / `mcp.excluded` by server name; recommended pattern is defining canonical servers **and** naming them in `mcp.allowed` in the system file [GE5].
- `admin.mcp.enabled`, `admin.mcp.config` (allowlist), `admin.mcp.requiredConfig` (always injected), `admin.secureModeEnabled` [GE6].
- Enterprise Admin Controls (Management Console, cannot be overridden locally): MCP on/off (default **disabled**), MCP server allowlist (preview) matching by name; `url`, `type`, `trust` come from the admin entry; required MCP servers (preview) [GE13].
- CLI flag `--allowed-mcp-server-names` per session [GE4].

## Availability

- Official quota table: Google account (Code Assist for individuals) 1,000 req/day; AI Pro 1,500; AI Ultra 2,000; API key free 250 (Flash only); Code Assist Standard 1,500; Enterprise 2,000 [GE9].
- Third-party reports of the 2026-06-18 end of individual access (see Summary) [GE10] `[secondary]`; the v0.52.0 release notes (2026-07-22) add "clear error messages when user account has no Code Assist tier" [GE14].
- Code Assist IDE extensions: VS Code and JetBrains [GE8].

## Headless/CI

- `gemini -p "<prompt>"` (or any non-TTY run) with `--output-format json|stream-json`; exit codes 0/1/42/53 [GE4], [GE15].
- Approvals: `--approval-mode yolo|auto_edit|plan|default`, or `"trust": true` per server; Admin "Strict Mode" (default on) blocks yolo [GE4], [GE13].
- v0.63.0 enabled autonomous plan execution in non-interactive mode [GE14].
- OAuth does not work headless; use `headers` with `${VAR}`. Auth to Gemini itself in CI: API key or Vertex.

## Proposed support level

**Full** (Gemini CLI) — Streamable HTTP with DCR OAuth or env-expanded bearer, server instructions, prompts and resources all supported, headless `-p` works; caveats: no CIMD, strict RFC 9207 `iss` check, and individual-account availability is doubtful since 2026-06. Code Assist agent mode: **Partial** (same config on VS Code, IntelliJ path and feature depth unverified).

## Open questions

1. Is the 2026-06-18 end of individual access real and final? The repo docs still show a free tier.
2. Does the memory-manager AS metadata set `authorization_response_iss_parameter_supported`, and does `/authorize` return `iss`?
3. Does the Gemini API (not Vertex) accept `anyOf` with `null` in tool schemas for current models?
4. Code Assist (IntelliJ): exact `mcp.json` location and whether instructions/prompts work.
5. Is there a hard tool-count limit in Gemini CLI, and what is it?

## Sources

- [GE1] Gemini CLI docs, "MCP servers with Gemini CLI", https://geminicli.com/docs/tools/mcp-server (source: `google-gemini/gemini-cli` `docs/tools/mcp-server.md`), retrieved 2026-10-07
- [GE2] Gemini CLI source, `packages/core/src/mcp/oauth-provider.ts` and `oauth-utils.ts`, https://raw.githubusercontent.com/google-gemini/gemini-cli/main/packages/core/src/mcp/oauth-provider.ts, retrieved 2026-10-07
- [GE3] Gemini CLI source, `packages/core/src/tools/mcp-client.ts` (transport selection, `getInstructions()`, roots handler), https://raw.githubusercontent.com/google-gemini/gemini-cli/main/packages/core/src/tools/mcp-client.ts, retrieved 2026-10-07
- [GE4] Gemini CLI docs, CLI reference (`-p`, `--approval-mode`, `--allowed-mcp-server-names`), https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/cli-reference.md, retrieved 2026-10-07
- [GE5] Gemini CLI docs, Enterprise guide (MCP allowlisting patterns), https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/enterprise.md, retrieved 2026-10-07
- [GE6] Gemini CLI docs, Configuration reference (locations, env var syntax, `admin.mcp.*`, `context.fileName`), https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/reference/configuration.md, retrieved 2026-10-07
- [GE7] Gemini CLI docs, GEMINI.md, https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/gemini-md.md, retrieved 2026-10-07
- [GE8] Google Cloud docs, "Use the Gemini Code Assist agent mode", https://docs.cloud.google.com/gemini/docs/codeassist/use-agentic-chat-pair-programmer (search snippet only), retrieved 2026-10-07
- [GE9] Gemini CLI docs, "Quotas and pricing", https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/resources/quota-and-pricing.md, and README, retrieved 2026-10-07
- [GE10] Third-party reports, e.g. https://www.shopifreaks.com/google-ends-free-gemini-cli-access-for-non-enterprise-users-on-june-18-replacing-it-with-closed-source-antigravity-cli/ and https://amux.io/guides/gemini-cli-to-antigravity-cli/ (search snippets only), retrieved 2026-10-07
- [GE11] Google Cloud, Vertex AI "Function calling reference" and `FunctionDeclaration` REST type, https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/models/function-calling (search snippets only), retrieved 2026-10-07
- [GE12] `google-gemini/gemini-cli` issue #21823 "Increase MCP tool limit from 100 to 500", https://github.com/google-gemini/gemini-cli/issues/21823 (search snippet only), retrieved 2026-10-07
- [GE13] Gemini CLI docs, "Enterprise Admin Controls", https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/admin/enterprise-controls.md, retrieved 2026-10-07
- [GE14] Gemini CLI release notes (v0.63.0 2026-10-06; v0.52.0 auth messages), https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/changelogs/index.md, retrieved 2026-10-07
- [GE15] Gemini CLI docs, Headless mode reference, https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/headless.md, retrieved 2026-10-07
- [GE16] npm registry, `@google/gemini-cli` (latest 0.63.0, preview 0.64.0-preview.0), https://registry.npmjs.org/@google/gemini-cli, retrieved 2026-10-07
