# Client support matrix

One page per client lives in this directory, following [`_template.md`](./_template.md). The
table below is the owner-approved support matrix for F-02 Client Integrations
([`../features/F-02-client-integrations.md`](../features/F-02-client-integrations.md)); its
*Approved level* and *Main caveat* columns are copied verbatim from that approval.

Approved by the owner on 2026-10-08 (O19), based on the desk research of 2026-10-07 in
[`../research/clients/`](../research/clients/). Each level is re-verified with a tested version
when the client's work package starts; a client that turns out weaker is downgraded here and
reported, never promised.

## Legend

- **Approved level / Main caveat** — the 2026-10-08 owner decision: `Full`, `Partial`, or
  `Not possible (reliably)`, each with its blocking caveat where one applies (for example
  `⛔ O18` for a decision gate).
- **Read / Write / Search** — whether `memory_read`/`memory_get`, `memory_write`, and
  `memory_search` work through this client.
- **Instructions** — whether the client loads server `instructions` (and the `memory_guide`
  prompt), or needs a separate file/rules/skill instead.
- **OAuth** — which client-registration method(s) this client speaks against our
  authorization server (CIMD, DCR, a pre-registered client).
- **Token** — whether a static token/header is supported as an alternative to OAuth.
- **Enterprise SSO** — status against the Entra-backed enterprise auth facade (F-01).
- **Namespaces** — whether the client's integration respects namespace scoping beyond the
  default `me`.

Each of the last six columns carries one of three states:

- **verified (date)** — checked against a running server and a pinned client version on that
  date.
- **documented (research)** — established from vendor docs or other secondary sources, not yet
  checked live; see the row's research link.
- **unverified** — not established either way.

The **Research** column links the desk research behind the row. The **Page** column links this
client's own page under `docs/clients/`, where one exists yet.

## Matrix

| Client | Approved level | Main caveat | Read | Write | Search | Instructions | OAuth | Token | Enterprise SSO | Namespaces | Research | Page |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude.ai | Full | — (verified 2026-10-07) | documented | verified (2026-10-07) | documented | yes | CIMD verified (2026-10-07), DCR documented | static_headers beta, per org only | OIDC login verified (2026-10-07); Entra unverified | unverified | [research](../research/mcp-auth-and-connectors.md#4-claudeai-custom-connectors-remote-mcp) | [page](./claude-ai.md) |
| Claude Code | Full | — (verified) | documented | documented | verified (2026-10-07) | yes, ≤ 2,048 chars | OAuth over HTTP verified (2026-10-07, own CIMD), pre-registered `--client-id` | yes, `--header` or `headersHelper` | as claude.ai | unverified | [research](../research/mcp-auth-and-connectors.md#5-claude-code-k1) | [page](./claude-code.md) |
| Open WebUI ≥ 0.6.31 | Full (M13) | tool must be enabled per chat; DCR request shape unverified live | documented (tools only, enable per chat) | documented (tools only, enable per chat) | documented (tools only, enable per chat) | no | DCR with `client_secret_post`, static client, no CIMD; live unverified | service key per connection only, not per user | documented (option A via facade), live unverified | per user with `oauth_2.1`, shared with bearer (documented) | [research](../research/clients/openwebui.md) | — |
| Codex CLI / IDE | Full (cloud agent unverified) | reported MCP call cancellation under `codex exec` | documented; `codex exec` cancellation bug [secondary] | documented; `codex exec` cancellation bug [secondary] | documented; `codex exec` cancellation bug [secondary] | no → `AGENTS.md` | CIMD preferred, DCR, pre-registered | yes (`bearer_token_env_var`) | unverified | unverified | [research](../research/clients/codex.md) | — |
| Gemini CLI | Full | names ≤ 63 chars, `additionalProperties` stripped; free tier status unclear | documented | documented | documented | yes | DCR, no CIMD, strict `iss` check | yes (headers with `${VAR}`) | unverified | unverified | [research](../research/clients/gemini.md) | — |
| Gemini Code Assist | Partial | IDE agent mode only | documented (VS Code); IntelliJ unverified | documented (VS Code); IntelliJ unverified | documented (VS Code); IntelliJ unverified | unverified (approved: "partly") | as Gemini CLI | as Gemini CLI | unverified | unverified | [research](../research/clients/gemini.md) | — |
| GitHub Copilot (VS Code, CLI) | Full | 128 tools per request | documented | documented | documented | yes | CIMD, DCR, pre-registered | yes | unverified | unverified | [research](../research/clients/copilot.md) | — |
| Copilot coding agent, JetBrains | Partial | needs a static token; firewall reachability unknown | cloud agent: tools only, allowlist, no approval; code review readOnlyHint only; JetBrains depth unverified | cloud agent: tools only, allowlist, no approval; code review readOnlyHint only; JetBrains depth unverified | cloud agent: tools only, allowlist, no approval; code review readOnlyHint only; JetBrains depth unverified | no / unverified | cloud agent: no; JetBrains: yes | yes (`COPILOT_MCP_*`) | cloud agent: no; else unverified | unverified | [research](../research/clients/copilot.md) | — |
| Cursor | Partial | reported: static `Authorization` header dropped when the server advertises OAuth | documented | documented | documented | no → rules file | DCR, no CIMD, pre-registered | yes, but header reportedly dropped when OAuth advertised [secondary] | unverified | unverified | [research](../research/clients/cursor.md) | — |
| Google Antigravity | Partial | sources mostly secondary | documented | documented | documented | unverified | DCR, CIMD (CLI changelog) | yes (header); env substitution unverified | unverified | unverified | [research](../research/clients/antigravity.md) | — |
| Hermes Agent | Full, tier 1 | third-party messages can trigger writes (ADR-0013) | documented, tool filter | documented, tool filter | documented, tool filter | unverified → skill | DCR; CIMD semantics unverified | yes | unverified | unverified (ADR-0013 not built) | [research](../research/clients/hermes.md) | — |
| OpenClaw | Full, tier 1 | tier 2 is TypeScript (O17) | documented, `toolFilter` | documented, `toolFilter` | documented, `toolFilter` | unverified → skill | yes, per-requester; DCR/CIMD order unverified | yes | unverified | unverified | [research](../research/clients/openclaw.md) | — |
| ChatGPT | Partial | write actions only on Business/Enterprise/Edu; mobile unclear; secondary sources | documented | Business/Enterprise/Edu only (beta); Pro read-only (snippets) | documented | unverified | CIMD, DCR, static client (snippets) | no (not official; [secondary, unverified]) | unverified | unverified | [research](../research/clients/chatgpt.md) | — |
| Gemini Enterprise | Partial ⛔ O18 | no DCR; ≤ 100 actions | unverified; ≤ 100 actions (admin-enabled) | unverified; ≤ 100 actions (admin-enabled) | unverified; ≤ 100 actions (admin-enabled) | unverified | pre-registered confidential client only, not yet in our AS (⛔ O18, ADR-0015) | no | unverified | unverified | [research](../research/clients/gemini-app.md) | — |
| Gemini app (consumer) | Not possible (reliably) | revisit at M16 | unverified | unverified | unverified | unverified | DCR, token exchange reportedly broken [secondary] | unverified | unverified | unverified | [research](../research/clients/gemini-app.md) | — |
