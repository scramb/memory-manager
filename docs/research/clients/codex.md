# Client: OpenAI Codex (CLI, IDE extension, cloud)

Retrieved: 2026-10-07 · Tested version: none (desk research; latest CLI 0.161.0 on npm, published 2026-10-07)

**Source quality warning:** `developers.openai.com` (the Codex docs) and `github.com`/`api.github.com` were blocked by the research environment's egress proxy. Config facts below were read **directly from the Codex source** on `raw.githubusercontent.com` (`openai/codex`, branch `main`, fetched 2026-10-07) and from search snippets of the official docs. Source code shows what the client can do, not what OpenAI documents as supported. `[secondary]` = third-party only; `[unverified]` = no source.

## Summary

Codex CLI and the IDE extension share `~/.codex/config.toml` (and a trusted project's `.codex/config.toml`) [CX1]. Remote servers use **Streamable HTTP only** (no SSE transport in the config enum) [CX2]. Auth: a bearer token from an env var (`bearer_token_env_var`), static or env-sourced headers, or OAuth via `codex mcp login`, which **prefers CIMD and falls back to DCR**, with optional preregistered `client_id`/`client_secret` [CX2], [CX3], [CX4]. Tool names are sanitized to `^[a-zA-Z0-9_-]+$` and kept ≤ 128 characters including the namespace [CX5]. Large tool input schemas are compacted above 5,000 bytes [CX4]. Prompts are not exposed and no handling of server `instructions` was found [CX6]. `codex exec` runs headless; MCP approvals in `exec` have a reported bug [CX9]. Admins can restrict MCP servers by name plus command/URL via `requirements.toml` [CX7], [CX8]. MCP support in Codex **cloud** is not documented [CX1].

## Transports

| Transport | Support | Source |
|---|---|---|
| stdio | Yes (`command`, `args`, `env`, `env_vars`, `cwd`) | [CX2] |
| Streamable HTTP | Yes (`url`) | [CX2] |
| SSE | **No** — `McpServerTransportConfig` has only `Stdio` and `StreamableHttp` | [CX2] |

## Auth

| Mechanism | Support | Source |
|---|---|---|
| Static bearer | `bearer_token_env_var = "NAME"` → `Authorization: Bearer <value of NAME>`; the secret must come from the environment | [CX2], [CX1] |
| Static headers | `http_headers` (literal), `env_http_headers` (header → env var name), `http_headers_helper` (local command printing JSON headers) | [CX2] |
| OAuth + DCR | Yes (fallback when CIMD not advertised) | [CX3] |
| OAuth + CIMD | Yes, **preferred** when the AS metadata has `client_id_metadata_document_supported` and token endpoint auth method `none`; needs a loopback callback | [CX3] |
| Preregistered client | `[mcp_servers.<name>.oauth]` with `client_id`, `client_secret`, `callback_url`, `callback_port` | [CX4] |
| Scopes / resource | `scopes = [...]`, `oauth_resource = "..."` (RFC 8707) | [CX4], [CX1] |
| Auth mode selector | `auth = "oauth" \| "chatgpt" \| "ema_auth"` (EMA = enterprise IdP token exchange) | [CX4] |
| Env var substitution in strings | No generic `${VAR}` interpolation found; env indirection is per field (`bearer_token_env_var`, `env_http_headers`, `env_vars`) | [CX2] |
| Login | `codex mcp login <server>` | [CX1] |

## Configuration

`~/.codex/config.toml` (global) or `.codex/config.toml` (project, trusted projects only); the CLI and IDE extension read the same file [CX1].

```toml
[mcp_servers.memory]
url = "https://memory.example.com/mcp"
bearer_token_env_var = "MEMORY_MANAGER_TOKEN"
# optional: enabled_tools = ["memory_index", "memory_search", "memory_read"]
# optional: default_tools_approval_mode = "writes"
```

OAuth variant: omit `bearer_token_env_var`, then run `codex mcp login memory`.

Other per-server keys from the schema: `enabled`, `required`, `enabled_tools`, `disabled_tools`, `tools.<name>.approval_mode`, `startup_timeout_sec`, `tool_timeout_sec`, `supports_parallel_tool_calls`, `startup_readiness`, `tool_input_schema_max_bytes` [CX4].

## Limits

| Limit | Value | Source |
|---|---|---|
| Tool name charset | `^[a-zA-Z0-9_-]+$` (Responses API); other characters replaced with `_` | [CX5] |
| Tool name length | `MAX_TOOL_NAME_LENGTH = 128` for namespace + name; longer names are hashed/shortened | [CX5] |
| Schema size | Input schemas larger than `tool_input_schema_max_bytes` (default 5,000 bytes) are compacted | [CX4] |
| Max tools | No hard cap found `[unverified]` | — |
| JSON Schema keywords | No `$ref`/`oneOf` restriction found in the MCP layer `[unverified]` | — |

memory-manager impact: names are plain ASCII with underscores and well below the limits. Check that every input schema stays under 5,000 bytes, or that compaction keeps the descriptions that matter (`if_version` semantics).

## Instructions/prompts/resources

| Feature | Support | Source |
|---|---|---|
| Tools | Yes | [CX2] |
| Resources | Yes (`list_resources`, `read_resource` in the client) | [CX6] |
| Prompts | Not found: no `list_prompts` in the rmcp client | [CX6] `[unverified]` |
| Server `instructions` | Not found in the `codex-mcp` crate | [CX6] `[unverified]` |
| Elicitation | Yes (MCP elicitation plus an OpenAI form extension) | [CX6] |
| Tool annotations | `default_tools_approval_mode = "writes"` lets read-only tools run and prompts for write tools (uses MCP annotations) | [CX4], [CX10] `[secondary]` |

Consequence: the memory rules must also live in `AGENTS.md` for Codex. Set `readOnlyHint` correctly on memory_index/search/read so `writes` mode works.

## Instruction files

`AGENTS.md` (Codex docs page "AGENTS.md", linked from the repo) [CX11]. Global and nested lookup rules are in the official docs, which were not readable here `[unverified]`.

## Org policies

- `requirements.toml` can restrict MCP servers: a server must match on **name and** transport identity (stdio command, or HTTP URL as exact/prefix/regex). Servers that do not match are disabled, not fatal [CX7], [CX8].
- Requirement layers per a third-party guide: cloud-managed requirements (ChatGPT Business/Enterprise) → macOS MDM (`com.openai.codex:requirements_toml_base64`) → `/etc/codex/requirements.toml` [CX12] `[secondary]`.
- `allow_managed_hooks_only` exists for hooks [CX13].

## Availability

Codex is included in ChatGPT Free (limited), Go, Plus, Pro, Business, Edu and Enterprise; it can also run with an API key [CX14] `[secondary]` (official pricing page not readable). Apache-2.0 open source CLI.

## Headless/CI

- `codex exec "<prompt>"` runs non-interactively and reads the same `config.toml` [CX1].
- Static bearer via `bearer_token_env_var` fits CI secrets.
- Open issue: MCP tool calls get cancelled under `codex exec` even with `default_tools_approval_mode`; only `--dangerously-bypass-approvals-and-sandbox` worked for the reporter [CX9] `[secondary]`.
- OAuth needs a loopback browser flow, so CI should use the bearer path.

## Proposed support level

**Full** (CLI and IDE extension) — Streamable HTTP with CIMD/DCR OAuth or env-sourced bearer, resources and elicitation supported; prompts and server `instructions` missing, so the rules ship via `AGENTS.md`. Codex cloud: not verified.

## Open questions

1. Does Codex inject server `instructions` anywhere (core crate not fully read)?
2. Codex cloud: can a cloud environment reach a remote MCP server, and with which auth?
3. Is the `codex exec` approval bug [CX9] fixed in 0.161.0? Needed before using Codex in the CI eval.
4. Which callback URL does Codex publish in its CIMD document, and does the memory-manager AS accept loopback redirects with any port?

## Sources

- [CX1] OpenAI, Codex docs "Model Context Protocol", https://developers.openai.com/codex/mcp (search snippets only), retrieved 2026-10-07
- [CX2] `openai/codex` source, `codex-rs/config/src/mcp_types.rs` (`McpServerTransportConfig`), https://raw.githubusercontent.com/openai/codex/main/codex-rs/config/src/mcp_types.rs, retrieved 2026-10-07
- [CX3] `openai/codex` source, `codex-rs/rmcp-client/src/oauth_client_registration.rs` ("Prefer a supported native CIMD and otherwise use advertised DCR"), https://raw.githubusercontent.com/openai/codex/main/codex-rs/rmcp-client/src/oauth_client_registration.rs, retrieved 2026-10-07
- [CX4] `openai/codex` config JSON schema, `codex-rs/core/config.schema.json` (`RawMcpServerConfig`, `McpServerOAuthConfig`, `McpServerAuth`, `AppToolApproval` = auto/prompt/writes/approve), https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/config.schema.json, retrieved 2026-10-07
- [CX5] `openai/codex` source, `codex-rs/codex-mcp/src/tools.rs` (`MAX_TOOL_NAME_LENGTH = 128`) and `codex-rs/codex-mcp/src/mcp/mod.rs` (`sanitize_responses_api_tool_name`), https://raw.githubusercontent.com/openai/codex/main/codex-rs/codex-mcp/src/tools.rs, retrieved 2026-10-07
- [CX6] `openai/codex` source, `codex-rs/rmcp-client/src/rmcp_client.rs` and `codex-rs/codex-mcp/src/*.rs` (grep for resources/prompts/instructions), https://raw.githubusercontent.com/openai/codex/main/codex-rs/rmcp-client/src/rmcp_client.rs, retrieved 2026-10-07
- [CX7] `openai/codex` source, `codex-rs/config/src/mcp_requirements.rs`, https://raw.githubusercontent.com/openai/codex/main/codex-rs/config/src/mcp_requirements.rs, retrieved 2026-10-07
- [CX8] `openai/codex` PR #9101 "Restrict MCP servers from requirements.toml", https://github.com/openai/codex/pull/9101 (search snippet only), retrieved 2026-10-07
- [CX9] `openai/codex` issue #24135 (MCP calls cancelled under `codex exec`), https://github.com/openai/codex/issues/24135 (search snippet only), retrieved 2026-10-07
- [CX10] Third-party blog on `writes` approval mode, https://codex.danielvaughan.com/2026/07/24/codex-cli-writes-app-approval-mode-mcp-tool-annotations-read-only-hint-approval-flow/ (search snippet only), retrieved 2026-10-07
- [CX11] `openai/codex` `docs/agents_md.md` → https://developers.openai.com/codex/guides/agents-md, https://raw.githubusercontent.com/openai/codex/main/docs/agents_md.md, retrieved 2026-10-07
- [CX12] WikiDocs (Korean), "관리 구성 (Managed Configuration)", https://wikidocs.net/365684 (search snippet only), retrieved 2026-10-07
- [CX13] `openai/codex` `docs/config.md`, https://raw.githubusercontent.com/openai/codex/main/docs/config.md, retrieved 2026-10-07
- [CX14] Third-party pricing guides, e.g. https://codex.danielvaughan.com/2026/04/10/codex-subscription-tiers-pro-100-pricing-guide and https://seawork.ai/en/blogs/codex-pricing/ (search snippets only), retrieved 2026-10-07
- [CX15] npm registry, `@openai/codex` dist-tags (latest 0.161.0, 2026-10-07T16:04Z), https://registry.npmjs.org/@openai/codex, retrieved 2026-10-07
