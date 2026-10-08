# Client: ChatGPT (custom MCP apps / developer mode)

Retrieved: 2026-10-07 · Tested version: none (desk research; latest: hosted service, no version)

**Source quality warning:** `help.openai.com`, `developers.openai.com` and `platform.openai.com` were blocked by the research environment's egress proxy. Every OpenAI statement below comes from **search-engine snippets of the official pages**, not from a full read. Claims that only third-party or community sources support are marked `[secondary]`; claims without any source are marked `[unverified]`. This file must be re-checked against the live pages before any decision depends on it.

## Summary

ChatGPT connects to remote MCP servers as custom "apps" (renamed from "connectors" in December 2025, and reportedly to "plugins" in July 2026 [C6], [C9]). Full MCP support **including write actions** is a beta for **Business, Enterprise and Edu** on **ChatGPT web**; Pro users can connect MCP servers in developer mode with read/fetch only; Plus is not named in the official help article [C1]. Auth is OAuth (CIMD preferred, DCR fallback, or static OAuth client credentials), no auth, or mixed [C2], [C3]. On workspaces, admins enable developer mode, control it with RBAC (Enterprise/Edu) and publish apps to the workspace [C1], [C4].

For memory-manager: reachable from Business/Enterprise/Edu with full read/write; from Pro read-only. The OAuth AS already offers CIMD + DCR + PKCE, which fits.

## MCP support

| Aspect | Finding | Source |
|---|---|---|
| Surface | Developer mode → create app with MCP server URL; web only per the official FAQ ("Are MCP apps available on mobile? No – web only") | [C1] |
| Read vs write | Business/Enterprise/Edu: full MCP incl. write/modify (beta). Pro: read/fetch only in developer mode. OpenAI-built apps are search-only | [C1] |
| Write confirmation | ChatGPT asks for confirmation "based on app permissions and the action's context" | [C1] |
| Plus | Not named in the official article; community reports conflict | [C1], [C7] `[secondary]` |
| Developer-mode toggle removed | A post dated 2026-10-05 says custom MCP servers can now be added via Plugins → Add → Create custom MCP server without developer mode | [C8] `[secondary, unverified]` |
| Transport | Remote HTTPS MCP server; Streamable HTTP and SSE accepted per guides | `[secondary]` [C10] |
| Company knowledge | Custom apps can be used in company knowledge (Business/Enterprise/Edu); apps with interactive UI cannot | [C4], [C6] |
| Server `instructions`, prompts, resources | `[unverified]` — no source found on whether ChatGPT uses MCP `instructions` or `prompts/*` | — |
| Memory interplay | Community report: custom MCP connectors in developer mode run with ChatGPT memory off ("No Memory") | [C7] `[secondary]` |
| Mobile | Official: web only. Community: read-only tools reportedly work on iOS, write tools fail | [C1], [C11] `[secondary]` |

## Auth

From OpenAI developer docs (snippets) [C2], [C3]:

- Options when creating an app: **OAuth**, **No authentication**, **Mixed** (initialize and `tools/list` without auth; each tool requires OAuth or not according to its security scheme in tool metadata).
- OAuth registration: static OAuth client credentials if supplied; otherwise **CIMD** when the AS advertises `client_id_metadata_document_supported: true` (with `token_endpoint_auth_method` `none` or `private_key_jwt`); otherwise **DCR**. PKCE is required.
- Request a refresh-capable scope, otherwise ChatGPT may lose access when the first token expires [C1] (snippet).
- **Static bearer token:** A third-party issue describes an "Access token / API key" option in the New App screen [C12] `[secondary, unverified]`; OpenAI's listed options do not include it. memory-manager should rely on OAuth for ChatGPT.

## Configuration

UI only: Settings → (Apps | Plugins) → developer mode / create app → name, MCP server URL, auth type. The location of the toggle has moved several times in 2026 [C9] `[secondary]`. Nothing to configure on the operator side beyond the AS accepting ChatGPT's redirect URI and client metadata.

## Built-in memory and extension points

Not an agent framework; no memory plugin API. ChatGPT has its own memory feature, which reportedly is off while developer mode is in use [C7] `[secondary]`. Integration is MCP only.

## Instructions / skills

No skill system relevant here **[unverified]**. Guidance must therefore live in tool descriptions (and in server `instructions`, if ChatGPT reads them — unverified). Keep memory-manager's tool descriptions self-sufficient.

## Org/admin controls

- Admins/owners enable developer mode in workspace settings (Permissions & Roles) [C1], [C4].
- Enterprise/Edu: RBAC grants developer mode to selected members; RBAC controls who can use a published app and which actions it may take, before publishing [C1], [C4].
- Only admins/owners publish apps (Workspace settings → Apps → Drafts → Publish; safety warning for write actions). Business: changes after publishing require re-creating and re-publishing; Enterprise/Edu: actions can be toggled after publishing [C4].
- Admin controls, security and compliance article [C5].

## Availability

Web (chatgpt.com) for creation and use; mobile officially not supported for MCP apps [C1]. Plans: Business, Enterprise, Edu (full), Pro (read/fetch), Plus unclear, Free none [C1].

## Headless/CI usability

None. ChatGPT is an interactive product; no headless use of custom MCP apps.

## Proposed support level

**Partial.** Full read/write on Business/Enterprise/Edu web via OAuth (CIMD or DCR); read-only on Pro; not on mobile; Plus unclear; static tokens not reliably supported; use of server `instructions` unverified. memory-manager's tools need descriptions that work without `instructions` and the `memory_guide` prompt.

## Security notes (third-party message injection)

- ChatGPT can combine several apps in one conversation; content returned by another app (e-mail, web) can steer the model into calling `memory_write` **[inference]**. ChatGPT's write confirmations mitigate this but depend on the user reading them [C1].
- memory-manager mitigations: tool annotations (`readOnlyHint`, `destructiveHint`) so ChatGPT asks for confirmation on writes; `if_version` required; audit log with the OAuth client identity; scopes or namespace permissions per token (ADR-0008).

## Open questions

1. Confirm on the live help page: Plus support, Pro read-only, and whether the developer-mode toggle was removed on 2026-10-05.
2. Does ChatGPT use MCP server `instructions` and `prompts`?
3. Exact redirect URI(s) and CIMD `client_id` URL ChatGPT uses (needed for an allowlist in the AS).
4. Does ChatGPT honour `readOnlyHint` to skip confirmations for `memory_search`/`memory_read`?
5. Does "company knowledge" require specific tool names (historically `search`/`fetch`)?

## Sources

- [C1] Developer mode and MCP apps in ChatGPT, https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt (search snippets only), retrieved 2026-10-07
- [C2] Authentication (plugins/apps), https://developers.openai.com/plugins/build/auth (search snippets only), retrieved 2026-10-07
- [C3] Building MCP servers for plugins and API integrations, https://developers.openai.com/api/docs/mcp and Add custom MCP server, https://developers.openai.com/api/docs/guides/custom-mcp-server (search snippets only), retrieved 2026-10-07
- [C4] ChatGPT Enterprise and Edu release notes, https://help.openai.com/en/articles/10128477-chatgpt-enterprise-and-edu-release-notes; ChatGPT Business release notes, https://help.openai.com/en/articles/11391654-chatgpt-business-release-notes (search snippets only), retrieved 2026-10-07
- [C5] Admin controls, security, and compliance for plugins and apps, https://help.openai.com/en/articles/11509118-admin-controls-security-and-compliance-for-plugins-and-apps (title only), retrieved 2026-10-07
- [C6] Company knowledge in ChatGPT, https://help.openai.com/en/articles/12628342-company-knowledge-in-chatgpt-business-enterprise-and-edu (search snippet only), retrieved 2026-10-07
- [C7] Community thread "MCP server tools now in ChatGPT — developer mode", https://community.openai.com/t/mcp-server-tools-now-in-chatgpt-developer-mode/1357233 (search snippet only), retrieved 2026-10-07
- [C8] Post on X, 2026-10-05, https://x.com/AGTPinsights/status/2107191525499420778 (search snippet only), retrieved 2026-10-07
- [C9] ChatGPT, Wikipedia, https://en.wikipedia.org/wiki/ChatGPT (search snippet: apps renamed to plugins in July 2026), retrieved 2026-10-07
- [C10] Third-party guide, https://matagi.ai/blog/guides/how-to-connect-chatgpt-to-mcp-server (search snippet only), retrieved 2026-10-07
- [C11] Community thread "Apps-SDK on mobile devices", https://community.openai.com/t/apps-sdk-on-mobile-devices/1366422 (search snippet only), retrieved 2026-10-07
- [C12] frontmcp issue #544, https://github.com/agentfront/frontmcp/issues/544 (search snippet only), retrieved 2026-10-07
