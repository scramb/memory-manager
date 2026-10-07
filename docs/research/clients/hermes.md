# Client: Hermes Agent (Nous Research)

Retrieved: 2026-10-07 · Tested version: none (desk research; latest v0.21.5 / v2026.9.24, released 2026-09-24 [H1])

The docs site `hermes-agent.nousresearch.com` was not reachable from the research environment (egress block). Docs were therefore read from the Markdown sources in the repository (`website/docs/**` on `main`) [H2–H7]. These are the same files the site is built from, but `main` may be ahead of the v0.21.5 release. `[unverified]` marks claims without a primary source.

## Summary

Hermes Agent is an MIT-licensed, self-hosted Python agent with a CLI, a desktop app and a messaging gateway (Telegram, Discord, Slack, Signal, e-mail and about 25 more). It is a full MCP client: stdio, Streamable HTTP and SSE, static headers, OAuth 2.1 + PKCE with DCR and a documented CIMD option, device flow for headless hosts, and include/exclude tool filters. Built-in memory is two small Markdown files (`MEMORY.md`, `USER.md`) injected as a frozen snapshot, plus FTS5 search over past sessions. One external **memory provider** plugin can run beside the built-in memory through a documented Python `MemoryProvider` interface (Honcho, Mem0, Supermemory and others ship this way).

memory-manager can be integrated today as an MCP server (tier 1) with no code on either side. A native memory provider (tier 2) is feasible and well specified, but it is new code in Python, which is a pool deviation for application code (see open questions).

## MCP support

| Aspect | Finding | Source |
|---|---|---|
| Transports | stdio (`command`/`args`/`env`), HTTP with Streamable HTTP as default, `transport: sse` for legacy SSE | [H2] |
| Tool naming | Registered as `mcp__<server>__<tool>`; non-alphanumerics become `_` | [H2] |
| Tool filter | `tools.include` (whitelist) / `tools.exclude` (blacklist, only if no include); exact names or fnmatch globs; include wins | [H2] |
| Resources / prompts | Wrapper tools `list_resources`/`read_resource` and `list_prompts`/`get_prompt`, only if the server advertises the capability; can be turned off per server | [H2], [H3] |
| Trust per server | `trust: full` (default) or `untrusted`; with `untrusted`, every tool without `readOnlyHint: true` needs user approval | [H2] |
| Sampling / elicitation | Configurable per server; elicitation form mode goes through approval, URL mode is declined | [H2] |
| Result sanitising | Invisible Unicode TAG characters stripped from tool results, resource content and tool descriptions | [H3] |
| Server `instructions` | **[unverified]** The client stores the `InitializeResult` but the excerpt read shows no code that injects `instructions` into the prompt | [H8] |
| Reload | `/reload-mcp` | [H2] |
| Hermes as MCP server | `hermes mcp serve`: stdio-only, 10 tools over its own conversations; not relevant here | [H3] |

v0.21.5 replaced the desktop MCP tab with a "Connectors" page [H1]; its behaviour was not checked.

## Auth

- **Static bearer:** `headers: { Authorization: "Bearer ${MM_TOKEN}" }`. `${VAR}` / `${env:VAR}` are resolved from the profile secret scope (`~/.hermes/.env`), then the process environment. An unset variable in a remote `url` or `headers` fails closed [H2].
- **OAuth 2.1:** `auth: oauth` on an HTTP server uses the MCP SDK flow: metadata discovery, DCR, PKCE, token exchange and refresh [H2]. The `oauth` block also accepts `client_id`, `client_secret`, `client_metadata_url`, `cimd`, `scope`, `token_endpoint_auth_method` and `redirect_uri`/`redirect_port` [H2]. The exact CIMD semantics are not described **[unverified]**.
- **Token storage:** `~/.hermes/mcp-tokens/<server>.json` (or per profile), mode 0600. Refresh tokens are dropped if the authorization server changes [H2], [H3].
- **Headless:** Background gateway processes never open a browser. Options are pasting the redirect URL back, device-code flow (`oauth.flow: device` / `hermes mcp login <server> --flow device`) if the AS supports it, SSH port forward, or a proxied `oauth.redirect_uri` [H3]. memory-manager's AS has no device flow, so headless hosts use paste-back or a static token.

## Configuration

`~/.hermes/config.yaml` (per profile `~/.hermes/profiles/<name>/`), secrets in `~/.hermes/.env` [H2]. Generic example:

```yaml
mcp_servers:
  memory:
    url: "https://memory.example.org/mcp"
    headers:
      Authorization: "Bearer ${MEMORY_MANAGER_TOKEN}"
    # or: auth: oauth
    tools:
      exclude: [memory_archive]
      prompts: true
```

## Built-in memory and extension points

- **Built-in:** `~/.hermes/memories/MEMORY.md` (agent notes, 2,200 characters) and `USER.md` (user profile, 1,375 characters). The `memory` tool has `add`/`replace`/`remove`; writes over the limit fail instead of evicting; duplicates are rejected; entries are scanned for injection and exfiltration patterns. Memory is rendered into the system prompt **once per session** (frozen snapshot, for prefix caching) [H4].
- **Session search:** All CLI and gateway sessions live in SQLite (`~/.hermes/state.db`) with FTS5; no LLM calls, no size cap [H4].
- **External providers:** Exactly one active at a time, alongside the built-in files. Bundled: Holographic, RetainDB, ByteRover; installable: Honcho, Hindsight, Supermemory, Mem0, OpenViking. `hermes memory setup` / `hermes memory status` [H4].
- **`MemoryProvider` interface** (Python ABC, `agent/memory_provider.py`) [H5]:
  - Required: `name`, `is_available()` (no network), `initialize(session_id, **kwargs)`, `get_tool_schemas()`, `handle_tool_call()`, `get_config_schema()`, `save_config()`.
  - Optional hooks: `system_prompt_block()`, `prefetch(query, *, session_id)` before each API call (auto-recall), `queue_prefetch()`, `sync_turn(user, assistant, *, session_id, messages)` after each turn (auto-capture, must be non-blocking), `on_session_end()`, `on_pre_compress()`, `on_memory_write()` (mirrors built-in memory writes), `shutdown()`.
  - `initialize` kwargs include `platform` (`cli`, `telegram`, …), `gateway_session_key`, `user_id`, `user_name`, `chat_id`, `agent_identity` and `agent_context` (`primary`, `cron`, `subagent`). The docs advise skipping automatic writes unless the context is `primary` [H5].
  - Registration via `register(ctx)` → `ctx.register_memory_provider(...)`, or the entry-point group `hermes_agent.memory_providers` [H5].

**Fit:** A provider could call memory-manager over HTTP: `prefetch` → `memory_search`, `on_memory_write` → mirror into a note, tools → proxy to `memory_*`. Automatic `sync_turn` capture conflicts with memory-manager's "curated, explicit writes only" model and would have to stay off or only propose writes.

## Instructions / skills

- Skills are `SKILL.md` files with YAML frontmatter under `~/.hermes/skills/`, compatible with the agentskills.io standard and with progressive disclosure. They are installable from a hub that includes ClawHub; installs are security-scanned [H6].
- The agent can create and edit skills itself (`skill_manage`); `skills.write_approval` stages these changes for review [H6].
- Practical path: ship a `memory-manager` skill that restates the `memory_guide` workflow, because use of MCP server `instructions` is unverified. MCP prompts are reachable through the `get_prompt` wrapper [H2].

## Org/admin controls

None as a product (single-operator tool). Controls are local config only: gateway allowlists (`TELEGRAM_ALLOWED_USERS`, `GATEWAY_ALLOWED_USERS`), DM pairing codes, admin tiers that gate slash commands only, per-server `trust: untrusted` and tool filters [H2], [H7].

## Availability

Linux, macOS, WSL2 and native Windows; CLI, desktop app and gateway; self-hosted; MIT license [H1], [H9].

## Headless/CI usability

Good. The gateway runs as a daemon and the CLI has `--format stream-json` (v0.21.4) [H1]. Use a static bearer token, because the gateway never opens a browser and memory-manager has no device flow [H3].

## Proposed support level

- **Level: Full.** Streamable HTTP, static bearer and OAuth (DCR) all match what memory-manager offers; read and write tools work; tool filters allow a read-only profile.
- **Tier: 1 (MCP tool) now; tier 2 (native `MemoryProvider`) as optional later work.** Tier 2 adds auto-recall (`prefetch`) and mirroring of built-in memory writes, but it is Python application code (outside the technology pool → needs an owner decision) and must follow Hermes's provider API, which changes between releases (signature differences already exist between doc copies [H5]).

## Security notes (third-party message injection)

- The gateway is default-deny (allowlist or pairing) because the agent may have terminal access [H7]. In groups, every allowed member's text reaches the model. Text from other people can make the agent call `memory_write`.
- The gateway passes platform, chat, thread and sender as per-message JSON context; with `privacy.redact_pii: true` they are hashed [H7]. The docs do not describe this as a defence against injection.
- Recommendations for operators: a separate token per agent, tool filter `exclude: [memory_archive, memory_supersede]` or a read-only token for agents attached to group chats, `trust: untrusted` so writes need approval, and namespace-scoped tokens (ADR-0008) so one gateway cannot write another person's namespace.
- memory-manager side: the audit log should record the client and token so writes from a gateway can be traced; note content stays data, not instructions.

## Open questions

1. Does Hermes inject MCP server `instructions` into the system prompt? (code not fully read)
2. Exact CIMD behaviour of `oauth.cimd` / `client_metadata_url` — which `client_id` URL does Hermes publish, and does it work against memory-manager's AS?
3. Is a tier-2 provider in Python acceptable (pool deviation), or should it be a thin config-only recipe?
4. Can the provider learn the triggering channel reliably enough to map `platform`/`chat_id` to a memory-manager namespace?

## Sources

- [H1] Releases, https://github.com/NousResearch/hermes-agent/releases (v0.21.5, v0.21.4, v0.21.3), retrieved 2026-10-07
- [H2] MCP config reference, https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/reference/mcp-config-reference.md (site: https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference), retrieved 2026-10-07
- [H3] MCP feature guide, https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/user-guide/features/mcp.md, retrieved 2026-10-07
- [H4] Memory, https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/user-guide/features/memory.md, retrieved 2026-10-07
- [H5] Building a memory provider plugin, https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/developer-guide/memory-provider-plugin.md, retrieved 2026-10-07
- [H6] Skills, https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/user-guide/features/skills.md, retrieved 2026-10-07
- [H7] Messaging gateway, https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/user-guide/messaging/index.md, retrieved 2026-10-07
- [H8] MCP client code, https://raw.githubusercontent.com/NousResearch/hermes-agent/main/tools/mcp_tool.py (partial read), retrieved 2026-10-07
- [H9] README, https://github.com/NousResearch/hermes-agent, retrieved 2026-10-07
