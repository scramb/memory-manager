# MCP transport, authorization, claude.ai Custom Connectors and Claude Code

Retrieved: 2026-10-06

Method: spec text read from the `modelcontextprotocol/modelcontextprotocol` repo (HEAD 8e12bf3, 2026-10-05). Claude docs fetched as Markdown from claude.com/docs and code.claude.com on 2026-10-06. modelcontextprotocol.io itself was blocked by the egress proxy, so URLs below point at the canonical site paths, which serve the same files.

---

## 1. Spec revisions

| Revision | Status (2026-10-06) | Source |
|---|---|---|
| **2026-07-28** | **Latest** ("Version 2026-07-28 (latest)" in docs nav; `LATEST_PROTOCOL_VERSION = "2026-07-28"` in schema.ts) | [S1], [S2] |
| 2025-11-25 | Previous; last "legacy" (handshake-based) revision | [S3] |
| 2025-06-18 | Older legacy revision | [S4] |
| draft | Work in progress after 2026-07-28 | [S1] |

- The roadmap (last updated 2026-08-22) calls it "the 2026-07-28 release". It says that release "made a remote MCP server a normal HTTP workload". [S5]
- The spec defines two **eras**. "Modern" is 2026-07-28 and later: version and capabilities travel in each request's `_meta`. "Legacy" is 2025-11-25 and earlier: there is an `initialize` handshake. A **dual-era** server may serve both eras on the same endpoint. [S6]
- **Decision impact:** Claude's own connector docs say Claude follows the **2025-03-26, 2025-06-18 and 2025-11-25** authorization specs. They do not list 2026-07-28 ([C1]). Claude's connector troubleshooting still talks about `initialize` timeouts ([C5]). A server built for claude.ai today must therefore serve the **legacy (2025-11-25) Streamable HTTP shape**, ideally as dual-era. All four Tier-1 SDKs reviewed support both eras (see mcp-sdks.md).
- **Tool names (SEP-986).** Retrieved 2026-10-10 · Feeds: #133 (`compat/lint.py`). Tool names SHOULD match `^[A-Za-z0-9._-]{1,128}$` (case-sensitive, 1-128 characters) [S13]. `mcp` 2.3.0 carries the identical pattern as `TOOL_NAME_REGEX` in `mcp/shared/tool_name_validation.py`, enforced only as a logged warning, not a hard rejection. Codex's own sanitizer is stricter and replaces non-matching characters instead of warning: `^[a-zA-Z0-9_-]+$`, no dot (docs/research/clients/codex.md:53 [CX5]).

## 2. Streamable HTTP transport

### 2026-07-28 (latest) [S7]
- **Endpoint:** the server MUST provide a single MCP endpoint path that supports POST, for example `https://example.com/mcp`.
- **Every client message is a new HTTP POST.** The client MUST send `Accept: application/json, text/event-stream`. The body is a single JSON-RPC request or notification.
- **Response:** for a request, the server returns either `application/json` or `text/event-stream` (SSE), and the client MUST support both. The server MAY send related notifications (progress, log) on the SSE stream before the final response. It MUST NOT send independent JSON-RPC *requests* on that stream. Server-to-client asks use **MRTR** (`InputRequiredResult`) instead.
- **Sessions removed:** `Mcp-Session-Id` is gone. A modern-only server receiving it SHOULD ignore it and not mint or echo one. GET and DELETE on the endpoint → `405`. `Last-Event-ID` is ignored, because stream resumability was removed. [S7 §Backward Compatibility], [S2 #1, #9]
- **No handshake:** `initialize` and `notifications/initialized` are removed. Each request carries `_meta` with `io.modelcontextprotocol/protocolVersion` and `io.modelcontextprotocol/clientCapabilities`. The new RPC `server/discover` (servers MUST implement it) returns supportedVersions, capabilities and `instructions`. [S2 #2, #3], [S8]
- **Long-lived notifications:** the GET stream and `resources/subscribe` are replaced by `subscriptions/listen`, a long-lived POST whose response is an SSE stream. [S2 #4]
- **Headers:**
  - `MCP-Protocol-Version` is required on every POST. It MUST match `_meta` or the server returns `400` with a `HeaderMismatch` error (-32020). An unsupported version returns `400` with `UnsupportedProtocolVersionError` (-32022) listing the supported versions. [S7 §Protocol Version Header]
  - New **required** headers `Mcp-Method` (all requests) and `Mcp-Name` (for `tools/call`, `resources/read` and `prompts/get`). Tool params may be mirrored into `Mcp-Param-*` headers via `x-mcp-header`. [S7 §Standard Request Headers], [S2 minor #4]
  - Servers SHOULD send `X-Accel-Buffering: no` on SSE responses. [S7]
- **Cancellation:** on HTTP, closing the SSE response stream is the cancellation signal. [S7 §Cancellation]
- **Origin validation:** servers MUST validate `Origin` on all incoming connections, to prevent DNS rebinding. A present but invalid Origin → **403**. Local servers SHOULD bind to 127.0.0.1. [S7 §Security]. This rule has been unchanged since the 2025-11-25 clarification [S3 minor #3]. Claude's troubleshooting warns that an "overly strict check rejects Anthropic's requests". [C5]
- Other 2026-07-28 changes:
  - `ping` and `logging/setLevel` are removed, and the log level moves into `_meta`.
  - Roots, Sampling and Logging are **deprecated**.
  - Tasks moved to an extension.
  - A `resultType` field is required on results.
  - `ttlMs` and `cacheScope` are required on list results.
  - `tools/list` SHOULD be deterministic.
  - "Resource not found" is now `-32602`.
  - HTTP+SSE (2024-11-05) is formally Deprecated.

  Source: [S2]

### 2025-11-25 / 2025-06-18 (legacy shape, what Claude speaks today) [S3], [S7 §Earlier Streamable HTTP Revisions]
- Single endpoint for POST plus an optional GET to open a standalone SSE stream for server-initiated messages.
- The server MAY assign a session via the `Mcp-Session-Id` response header on `initialize`. The client echoes it, and HTTP DELETE terminates the session.
- Servers may send JSON-RPC requests (sampling, elicitation) on SSE streams. Streams are resumable via SSE event IDs and `Last-Event-ID`.
- `MCP-Protocol-Version` header was introduced in 2025-06-18. A server MAY treat a missing header as 2025-03-26. [S7 §Protocol Version Header]
- 2025-11-25 additions:
  - Explicit 403 for an invalid Origin.
  - SSE polling: servers may disconnect at will (SEP-1699).
  - Optional `WWW-Authenticate` with `.well-known` fallback (SEP-985).
  - OIDC Discovery support.
  - Incremental scope consent (SEP-835).
  - CIMD (SEP-991).
  - Experimental tasks.

  Source: [S3]

## 3. MCP Authorization (2026-07-28) [S9], [S10], [S11], [S12]

Scope: HTTP transports SHOULD conform. stdio SHOULD NOT use this flow and should take credentials from the environment instead. [S9]

| Topic | Requirement (2026-07-28) | Src |
|---|---|---|
| OAuth 2.1 | The AS MUST implement OAuth 2.1 (draft-ietf-oauth-v2-1-13/14) for confidential and public clients | [S9] |
| Protected Resource Metadata (RFC 9728) | MCP servers **MUST** implement PRM. The document MUST contain `authorization_servers` with at least one entry. Clients MUST use it to discover the AS | [S9], [S10] |
| PRM discovery | Server MUST offer either (a) `WWW-Authenticate: Bearer resource_metadata="…"` on 401, or (b) a well-known URI: path-suffixed `/.well-known/oauth-protected-resource/<mcp-path>` or root `/.well-known/oauth-protected-resource`. Clients try the header first, then path-suffixed, then root | [S10] |
| AS metadata | The AS MUST provide RFC 8414 `/.well-known/oauth-authorization-server` **or** OIDC Discovery `openid-configuration`. Clients MUST support both | [S9], [S10] |
| PKCE | Clients MUST use PKCE with **S256**. Clients MUST refuse to proceed if `code_challenge_methods_supported` is absent from AS metadata. An OIDC-discovery AS MUST include that field | [S12] |
| Resource indicators (RFC 8707) | Clients MUST send `resource` (the canonical MCP server URI: lowercase scheme/host, no trailing slash preferred) in **both** the authorization and token requests, even if the AS ignores it | [S9] |
| Audience binding | Servers MUST validate that tokens were issued for them (audience) and MUST reject others. Invalid or expired token → 401 | [S9], [S12] |
| Token passthrough | Servers "MUST NOT accept or transit any other tokens". When calling upstream APIs the server is a separate OAuth client and "MUST NOT pass through the token it received" | [S9], [S12] |
| Token transport | `Authorization: Bearer` on **every** HTTP request. Never in the query string | [S9] |
| Client registration | Priority order: (1) pre-registered client, (2) **CIMD** if the AS advertises `client_id_metadata_document_supported: true`, (3) **DCR** if there is a `registration_endpoint`, (4) prompt the user. CIMD is **SHOULD**. **DCR (RFC 7591) is MAY and is Deprecated in 2026-07-28 (PR #2858)**, kept for backward compatibility. Clients SHOULD support static/pre-registered credentials | [S9], [S11], [S2 deprecated #4] |
| CIMD details | The client_id is an HTTPS URL with a path. The document has at least `client_id`, `client_name` and `redirect_uris`. The AS fetches it, MUST check that `client_id` equals the URL and MUST validate `redirect_uri` against the document | [S11] |
| DCR details | Clients MUST send an appropriate `application_type` (`native` for loopback, `web` for hosted). Credentials are bound to the issuing AS: key them by issuer and re-register if the AS changes | [S11], [S2 minor #8, #9] |
| Mix-up (RFC 9207) | AS SHOULD return `iss` in authorization responses and advertise `authorization_response_iss_parameter_supported`. Clients MUST validate a present `iss` | [S9], [S2 minor #7] |
| Scopes / step-up | Server SHOULD put `scope` in the `WWW-Authenticate` 401 challenge. Insufficient scope at runtime → **403** `error="insufficient_scope"` with the needed scopes. Clients SHOULD run step-up re-authorization with retry limits | [S9] |
| Refresh tokens | Clients MUST keep refresh tokens confidential, SHOULD include `refresh_token` in `grant_types`, and MAY request `offline_access` if the AS lists it. The server (RS) SHOULD NOT list `offline_access` in its challenge or PRM. The AS **MUST rotate** refresh tokens for public clients and SHOULD issue short-lived access tokens | [S9], [S12] |
| Redirects | Redirect URIs must be `localhost` or HTTPS. The AS MUST exact-match registered redirect URIs. The consent screen MUST show the redirect hostname, with extra warnings for localhost-only redirect URIs (CIMD) | [S12] |
| Status codes | 401 for missing or invalid token, 403 for insufficient scope, 400 for a malformed request | [S9] |

Changes relative to earlier revisions:
- **2025-06-18 → 2025-11-25:**
  - OIDC Discovery was added as an alternative to RFC 8414.
  - CIMD was added as the recommended registration method, and DCR became MAY.
  - `WWW-Authenticate` became optional, with a `.well-known` fallback.
  - Incremental scope / step-up was added.

  Source: [S3]
- **2025-11-25 → 2026-07-28:**
  - DCR is formally **Deprecated**.
  - RFC 9207 `iss` validation was added.
  - `application_type` is required in DCR.
  - Issuer-bound credentials.

  Source: [S2]

## 4. claude.ai Custom Connectors (remote MCP)

**Availability:** custom connectors by URL are available on **Free, Pro, Max, Team and Enterprise**. Free is limited to **one** custom connector. On Team and Enterprise, an Owner adds the connector and members then connect with their own account. [C3], [C7]

**Transport:** use **Streamable HTTP**. Claude also supports legacy HTTP+SSE, which is being deprecated. In the add dialog, a URL ending in `/sse` selects SSE (Advanced > Transport). [C1], [C3]

**Network:**
- Requests come **from Anthropic's cloud, not from the user's device**, even in Claude Desktop. [C7]
- The egress range is **`160.79.104.0/21`**. [C2], [S-IP]
- Discovery requests to the **authorization server** come from the same range, so a WAF in front of the IdP can break the flow. [C2]
- The server must be publicly reachable. **MCP tunnels** (cloudflared plus Anthropic `mcp-proxy`, Helm-deployable) let a private-network server be used without inbound ports. Tunnels are a research preview, **Enterprise plan by request only**. [C8]

**Supported auth types** [C2]:

| Type | Notes | Availability |
|---|---|---|
| `oauth_dcr` | RFC 7591 DCR | Default |
| `oauth_cimd` | CIMD | Default |
| `oauth_anthropic_creds` | You create a confidential client and Anthropic holds the secret | Directory listings; contact mcp-review@anthropic.com |
| `custom_connection` | Client id/secret (and URL) entered at connection time | Directory listings; contact Anthropic |
| `static_headers` | Owner enters an API key or bearer token sent as a header on every call | **Beta, limited orgs** |
| `none` | Authless | Default |

- **Custom connector dialog** (no review needed) [C3]:
  - **Use Claude's published identity**: Claude's CIMD, which is recommended.
  - Automatic registration: DCR.
  - **Use your own OAuth client**: enter a client ID you registered with the server, and leave the secret blank unless the AS requires it. With no secret, Claude acts as a public client. [C2]
  - **Request headers**: fixed API key or bearer token, beta. The value is sent verbatim, so include `Bearer `. Up to 4 headers. Custom header names need Anthropic approval. On an OAuth connection you cannot set `Authorization` as a header. Auth settings cannot be edited later: remove and re-add the connector.
- **Static bearer token on claude.ai:** possible only through the `static_headers` beta ("Request headers"). The credential is per organization, not per user. [C2], [C3]
- **Client selection logic:**
  - Claude uses CIMD only if AS metadata has **both** `"client_id_metadata_document_supported": true` **and** `"none"` in `token_endpoint_auth_methods_supported`. Otherwise it falls back to DCR. [C2]
  - DCR registers a **new client on every fresh connection**, which can produce very many clients. [C2]
  - Claude can be told a DCR client was deleted when the token endpoint returns 401 `invalid_client`. [search snippet of C2]
- **Callback URLs** [C2]:
  - Hosted surfaces (claude.ai web, Desktop, mobile, Cowork): **`https://claude.ai/api/mcp/auth_callback`**.
  - Claude Code: RFC 8252 loopback on an ephemeral port, for example `http://localhost:3118/callback`. Its CIMD (`https://claude.ai/oauth/claude-code-client-metadata`) declares `http://localhost/callback` and `http://127.0.0.1/callback`. The AS must match **both, ignoring the port**.
- **Claude-specific OAuth behaviour** [C2]:
  - A **401** is required to start sign-in. `WWW-Authenticate` on a 200 is ignored.
  - PRM `resource` must equal the user-entered URL exactly, including the path.
  - Only the **first** `authorization_servers` entry is used.
  - PKCE S256 is sent on every authorization request. The AS should advertise `code_challenge_methods_supported: ["S256"]`.
  - Scopes come from the `scope` in the 401 challenge, otherwise from PRM `scopes_supported`. Claude appends `offline_access` if the AS lists it.
  - `client_credentials` (M2M) is **not supported**.
  - Endpoint timeouts are 10 s for discovery, registration and token, and 30 s for refresh.
  - Refresh happens reactively on 401 and proactively up to 5 min before expiry. The AS must return `invalid_grant` for a dead refresh token and **rotate refresh tokens** for public clients (DCR and CIMD clients are public).
  - `/token` must accept form-urlencoded. `/register` uses JSON.
- **Audience:** Claude sends RFC 8707 `resource` set to the **canonical** MCP URL (lowercase scheme and host, no trailing slash, no default port, path included). The server should compare `aud` against that canonical value. [C4]
- **Redirects:** a 3xx to another host drops the `Authorization` header and fails the connection. [C4]
- **Step-up:** a 403 with a `scope` challenge triggers re-authorization and a retry of the call. Lazy auth is possible, with some tools authless. [C6]
- **Features exposed:** tools, prompts and resources. Text and image tool results. Text and binary resources. **Not supported:** resource subscriptions, sampling, "advanced or draft capabilities". [C1]
- **Limits:**
  - claude.ai and Desktop: max tool result **~150,000 characters**, tool call timeout **240 s**.
  - Claude Code: 25,000 tokens (`MAX_MCP_OUTPUT_TOKENS`) and `MCP_TOOL_TIMEOUT`.
  - No tool-count limit is documented in the pages reviewed. [C1]
- **How users see it:** connectors are toggled per conversation via the "+" menu. Tools need per-tool approval (Allow once / Always allow / Needs approval / Blocked). [C7], [C9]
- **Sharing with Claude Code:** connectors added in claude.ai are automatically available in Claude Code when you are logged in with that account. [K1]

## 5. Claude Code [K1]

- **Remote HTTP:**
  ```
  claude mcp add --transport http <name> <url>
  claude mcp add --transport http secure-api https://api.example.com/mcp --header "Authorization: Bearer your-token"
  ```
  The JSON `type` accepts `http` and its alias `streamable-http`. If HTTP fails, Claude Code falls back to SSE. `--transport sse` is deprecated.
- **stdio:**
  ```
  claude mcp add [--env K=V] [--scope local|project|user] <name> -- <command> [args...]
  ```
- **WebSocket:** `type: "ws"` via `claude mcp add-json`.
- **OAuth:**
  - Run `/mcp` in a session, or use `claude mcp login <name>` / `claude mcp logout <name>`. `--no-browser` prints the URL for SSH sessions.
  - A server is flagged as needing auth on 401 or 403. Claude Code refreshes on 401 and retries once.
  - Pre-registered client: `--client-id`, `--client-secret` (masked prompt), and `--callback-port` to fix the loopback port so it matches a pre-registered `http://localhost:<port>/callback`.
  - `oauth.authServerMetadataUrl` overrides discovery. `oauth.scopes` pins the requested scopes.
- **Custom auth:** `headersHelper` runs a script that emits headers at connect time. If you configure an `Authorization` header yourself, a 401/403 is reported as a failure rather than triggering OAuth.
- **Instructions:** server instructions are loaded at session start, which matters with tool search (the default). Tool descriptions and instructions are truncated at **2,048 characters** each.
- **Prompts:** MCP prompts appear as slash commands `/servername:promptname (MCP)` or `/mcp__server__prompt`.
- Claude Code uses its **own** CIMD and loopback redirect, not the claude.ai callback or Anthropic-held credentials. [C2]

---

## Sources
- [S1] MCP docs navigation / schema 2026-07-28 — https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/schema/2026-07-28/schema.ts ; https://modelcontextprotocol.io/specification/2026-07-28
- [S2] Changelog 2026-07-28 — https://modelcontextprotocol.io/specification/2026-07-28/changelog (source: docs/specification/2026-07-28/changelog.mdx)
- [S3] Changelog 2025-11-25 — https://modelcontextprotocol.io/specification/2025-11-25/changelog
- [S4] Spec 2025-06-18 — https://modelcontextprotocol.io/specification/2025-06-18
- [S5] Roadmap (updated 2026-08-22) — https://modelcontextprotocol.io/development/roadmap
- [S6] Versioning & compatibility — https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning
- [S7] Streamable HTTP — https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
- [S8] server/discover (DiscoverResult.instructions) — https://modelcontextprotocol.io/specification/2026-07-28/server/discover
- [S9] Authorization — https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- [S10] AS discovery — https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/authorization-server-discovery
- [S11] Client registration — https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration
- [S12] Security considerations — https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations
- [S13] SEP-986 tool names — https://modelcontextprotocol.io/specification/2025-11-25/server/tools#tool-names, retrieved 2026-10-10
- [S-IP] Anthropic IP addresses — https://platform.claude.com/docs/en/api/ip-addresses
- [C1] Build an MCP server for Claude — https://claude.com/docs/connectors/building
- [C2] Authentication for connectors — https://claude.com/docs/connectors/building/authentication
- [C3] Add a connector by URL (custom) — https://claude.com/docs/connectors/custom/add-unlisted
- [C4] Troubleshooting — https://claude.com/docs/connectors/building/troubleshooting
- [C5] Testing — https://claude.com/docs/connectors/building/testing
- [C6] Lazy authentication / step-up — https://claude.com/docs/connectors/building/lazy-authentication
- [C7] Getting started with custom connectors (support) — https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp
- [C8] MCP tunnels — https://claude.com/docs/connectors/mcp-tunnels/overview
- [C9] Connectors getting started — https://claude.com/docs/connectors/getting-started
- [K1] Claude Code MCP — https://code.claude.com/docs/en/mcp
- Claude Code CIMD — https://claude.ai/oauth/claude-code-client-metadata
