# Open WebUI as a memory-manager client

Retrieved: 2026-10-07 · Tested version: none (desk research; latest release v0.11.4)

Method: `docs.openwebui.com` is not reachable from the research environment, so the documentation was read from its source repository (`open-webui/docs`, `main` at commit `f687d16`, 2026-10-07). The pages are the same Markdown that the site renders; links below point at the published site. Behaviour was cross-checked against the Open WebUI source at tag `v0.11.4` (commit `8bd8b4f`, which is also `main` on the retrieval date). Code references are given as `path` at `v0.11.4`. Anything not confirmed by docs or code is marked **unverified**.

## Summary

- Open WebUI has had **native MCP support since v0.6.31** (2025-09-25). It supports **Streamable HTTP only**, with no stdio or SSE. For those transports the project points to `mcpo`.
- **Only admins can add MCP servers**, under *Settings > Admin > Integrations > External Tool Servers* or with the `TOOL_SERVER_CONNECTIONS` env var. Admins scope a server to users or groups with Access Control. Users can add their own *OpenAPI* tool servers only, and only with the "Direct Tool Servers" permission.
- MCP auth modes are `none`, `bearer` (static key), `session` (the user's Open WebUI JWT), `system_oauth` (the user's upstream SSO access token), `oauth_2.1` (DCR) and `oauth_2.1_static` (pre-registered client). With both OAuth 2.1 modes the **grant is per user** and the **client registration is per connection**.
- Open WebUI can also **forward identity**: `X-OpenWebUI-User-*` headers (`ENABLE_FORWARD_USER_INFO_HEADERS`), optionally as **one HS256-signed JWT** (`FORWARD_USER_INFO_HEADER_JWT_SECRET`). Per-connection header templates (`{{USER_ID}}`, `{{USER_EMAIL}}`, `{{USER_GROUPS}}` and others) are available too.
- Open WebUI uses MCP **tools only**. Server `instructions`, prompts and resource listing are not used. It opens a **new MCP session per chat request** and exposes tool names as `<server_id>_<tool>`.
- The **built-in memory** feature stores memories in Open WebUI's own database, per user. Since v0.10.0 it exposes native memory tools (`add_memory`, `search_memories` and others). It can be switched off globally (`ENABLE_MEMORIES`) or per group.
- The latest release is **v0.11.4** (2026-09-21), and **Helm chart 16.6.0** has `appVersion: 0.11.4`.

## MCP support

| Topic | Finding | Source |
|---|---|---|
| Since | v0.6.31 (2025-09-25) | docs `features/extensibility/mcp`; `CHANGELOG.md` |
| Transports | Streamable HTTP only. "Native MCP support in Open WebUI is **Streamable HTTP only**." stdio and SSE are reached through `mcpo`. | docs MCP FAQ |
| Client library | The official `mcp` Python SDK: `streamablehttp_client` with `ClientSession` | `backend/open_webui/utils/mcp/client.py` |
| Where configured | *Settings > Admin > Integrations > External Tool Servers > + Add Connection*, Type **MCP (Streamable HTTP)**. Env: `TOOL_SERVER_CONNECTIONS` (JSON array with `url`, `path`, `auth_type`, `key`, `config`, `info`, …). | docs MCP; env reference |
| Who can add | Admins only. Admins use Access Control to scope a server to users or groups. The user-level "Direct Tool Servers" option (`USER_PERMISSIONS_FEATURES_DIRECT_TOOL_SERVERS`, default off) allows **OpenAPI only**, and the type is locked. | docs MCP "MCP servers are admin-only"; CHANGELOG 0.8.x (#22615) |
| Activation | Global tool servers are **hidden by default** in chat, and each user enables them per chat through *Integrations > Tools*. A model can also have them as default tools, but OAuth 2.1 tools must not be set as defaults. | docs OpenAPI servers; docs MCP warning |
| Session lifecycle | `connect_mcp_server()` connects, runs `initialize()` and `list_tools` on every chat request that has the server enabled. There is no long-lived MCP session. | `utils/middleware.py` (`connect_mcp_server`, `process_chat_payload`) |
| Tool naming | The model sees `f'{server_id}_{tool_name}'`. A long server id plus a long tool name can exceed model-provider name limits. The 64-character limit for OpenAI-style function names is **unverified** for each provider. | `utils/middleware.py` |
| Tool filter | A "Function Name Filter List" per connection limits which tools are exposed. | docs MCP |
| Pagination | `tools/list` pagination has been followed since v0.11.4. | CHANGELOG 0.11.4 |
| JSON schema | `inputSchema` is passed through as `parameters`, and `outputSchema` is ignored (`# TODO`). In v0.11.4, Responses-API models no longer receive forced strict schemas (#30046). No other schema restrictions are documented, so behaviour with complex schemas such as `oneOf` or `$ref` is **unverified**. | `utils/mcp/client.py`; CHANGELOG 0.11.4 |
| Tool count limit | None documented (**unverified**). Many tools can exceed the handshake timeout `MCP_INITIALIZE_TIMEOUT` (default 10 s). | env reference |
| Results | Text content is used, and `resource` and binary/image content are handled; images are attached as files. On `isError` the client raises. | `utils/mcp/client.py`; CHANGELOG 0.9.x |
| `instructions` | **Not used.** The return value of `initialize()` is discarded. | `utils/mcp/client.py` |
| Prompts | **Not used.** The code never calls `list_prompts` or `get_prompt`. | grep over `backend/` |
| Resources | `list_resources` and `read_resource` exist in `MCPClient`, but nothing calls them. Only embedded resources in tool results are consumed. | grep over `backend/` |
| Other env | `MCP_INITIALIZE_TIMEOUT` (10), `AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER`, and `AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL` (TLS verify). `WEBUI_SECRET_KEY` must be stable, or stored OAuth tokens become undecryptable. | env reference; docs MCP |
| Stability statement | "Supported and improving … expect occasional breaking changes." For most deployments, OpenAPI "remains the **preferred** integration path". | docs MCP |

## Auth and identity forwarding

Auth modes are the `auth_type` values in `TOOL_SERVER_CONNECTIONS` or the UI. One function, `build_tool_server_headers()` in `utils/tools.py`, is "Shared by MCP and OpenAPI paths".

| Mode (`auth_type`) | How it works | Identity per user? | Secured how | Applies to |
|---|---|---|---|---|
| `none` | No `Authorization` header | No | Network only | MCP, OpenAPI |
| `bearer` | `Authorization: Bearer <key>` with one static key per connection. An empty key sends no header in v0.11.4 code, although the docs warn about `Bearer` with an empty value. | No, it is a single service credential | The key is stored in the Open WebUI config | MCP, OpenAPI |
| `session` | Forwards the user's **Open WebUI JWT** (`request.state.token.credentials`) as a bearer token | Yes | The receiver must validate an Open WebUI-issued token, which in practice means calling back into Open WebUI | MCP, OpenAPI |
| `system_oauth` (UI: "OAuth") | Forwards the **access token from the user's Open WebUI SSO login** (`__oauth_token__.access_token`). Tokens are stored encrypted server-side in table `oauth_session` and refreshed automatically when `offline_access` is granted. | Yes | The token is issued by the shared IdP, and its audience and scopes depend on what Open WebUI requested at login (`OAUTH_SCOPES` / `MICROSOFT_OAUTH_SCOPE`) | MCP, OpenAPI (also model connections) |
| `oauth_2.1` | Open WebUI acts as an **OAuth client of the MCP server's AS**. It discovers metadata through RFC 9728 and RFC 8414, registers with **DCR** once per connection, and runs authorization code + **PKCE S256** with `resource` (RFC 8707). Each user consents once in the browser, and tokens are stored and refreshed per user. | Yes | Our AS issues the token, and it is audience-bound | MCP (docs). OpenAPI is **unverified**. |
| `oauth_2.1_static` | Same flow as `oauth_2.1`, but with a **pre-registered `client_id`/`client_secret`** and an optional separate "OAuth Server URL" | Yes | Same as `oauth_2.1` | MCP (docs). OpenAPI is **unverified**. |
| User info headers | `ENABLE_FORWARD_USER_INFO_HEADERS=true` adds `X-OpenWebUI-User-Name`, `-User-Id`, `-User-Email`, `-User-Role`, and for tool servers also `X-OpenWebUI-Chat-Id` and `X-OpenWebUI-Message-Id`. Header names can be changed with `FORWARD_USER_INFO_HEADER_*` and `FORWARD_SESSION_INFO_HEADER_*`. This is **global**: it goes to every outbound backend (models, embeddings, web search, …). | Yes | Plain headers can be spoofed unless the network is trusted | MCP, OpenAPI (added to any auth mode) |
| Signed user JWT | With `FORWARD_USER_INFO_HEADER_JWT_SECRET` set, the four user headers are replaced by **one HS256 JWT** in `X-OpenWebUI-User-Jwt` (renamed with `FORWARD_USER_INFO_HEADER_JWT`). Claims are `sub` (Open WebUI user id), `email`, `name`, `role`, `iss=open-webui`, `iat` and `exp`. The lifetime is `FORWARD_USER_INFO_HEADER_JWT_EXPIRES_SECONDS` (300). The version that introduced it is **unverified** (it is in the v0.11.4 code but not in the CHANGELOG). | Yes | A shared HMAC secret, which every backend that receives it also holds | MCP, OpenAPI (and other backends) |
| Per-connection header templates | A JSON "Headers" field on each connection. Tokens: `{{USER_ID}}`, `{{USER_NAME}}`, `{{USER_EMAIL}}`, `{{USER_ROLE}}`, `{{AUTH_TYPE}}`, `{{USER_GROUPS}}`, `{{USER_GROUP_IDS}}`, `{{CHAT_ID}}` and `{{MESSAGE_ID}}`. MCP interpolation was fixed in v0.9.6. | Yes | Only as trustworthy as the channel. A static secret header can be added next to it. | MCP, OpenAPI, OpenAI model connections |
| Forward cookies | Off by default. When on, it forwards all browser cookies to an admin-configured server. | Yes (cookie) | Same-site SSO only | Admin tool servers |

Details of the OAuth 2.1 client (`utils/oauth.py`):

- **DCR**: `client_name='Open WebUI'`, `redirect_uris=['{WEBUI_URL}/oauth/clients/{client_id}/callback']`, `grant_types=['authorization_code','refresh_token']` and `response_types=['code']`. The default `token_endpoint_auth_method` is `client_secret_post`; Open WebUI falls back to the first method the AS advertises if that one is not supported. The scope comes from PRM `scopes_supported` or an admin override. If the AS metadata has no `registration_endpoint`, Open WebUI falls back to `<base>/register`. The `client_id` path segment appears to be `<type>:<server-id>`, for example `mcp:…` (inferred from code, **unverified**).
- **CIMD (Client ID Metadata Document)**: **not supported**. The v0.11.4 backend has no code path for it, so only DCR or a static client is possible.
- **Per user**: there is one client registration per connection, shared by all users. Each user goes through their own authorization in the browser, and the grant is stored "against their account alone". The flow must be completed in the same browser session at `WEBUI_URL`. An admin can start it with "Authorize OAuth", which only authorizes the admin's own account.
- **PKCE**: S256 by default. It is dropped only if AS metadata lists methods without S256.
- **Resource indicator**: Open WebUI reads `resource` from PRM and sends it on the authorize, token and refresh requests. The "OAuth Resource Parameter" setting accepts Automatic, Include or Omit.
- **Limitation**: OAuth 2.1 tools cannot be pre-enabled default tools. Since v0.10.0, Open WebUI starts the authorization flow automatically when a model uses such tools (CHANGELOG 0.10.0, "Automatic auth for models with OAuth 2.1 tools").
- Stored client info and tokens are encrypted with `OAUTH_CLIENT_INFO_ENCRYPTION_KEY` and `OAUTH_SESSION_TOKEN_ENCRYPTION_KEY`, both defaulting to `WEBUI_SECRET_KEY`.

Related: `ENABLE_OAUTH_TOKEN_EXCHANGE` (with `OAUTH_TOKEN_EXCHANGE_TRUSTED_CLIENT_IDS`) does the reverse. It exchanges an IdP access token for an Open WebUI JWT, and is not relevant for outbound calls.

## mcpo

- Repository: `open-webui/mcpo`. The latest tag is **v0.0.20**, whose commit is dated 2026-02-27, and there have been no newer commits on `main` since then. It is a proxy from MCP (stdio, SSE or Streamable HTTP) to OpenAPI, protected with `--api-key`, and it also supports OAuth 2.1 (DCR by default) towards upstream MCP servers. The image is `ghcr.io/open-webui/mcpo:main`.
- Status: the README still has a section "Why Use mcpo Instead of Native MCP?". The docs present it as the bridge for **stdio/SSE** servers and point to native MCP for Streamable HTTP. It has not been deprecated, but for a Streamable HTTP server like ours it is **not needed**. The low commit activity suggests maintenance mode; this is an interpretation and **unverified**.

## OpenAPI tool servers

- **Still supported, and documented as the "preferred" path** for most deployments.
- There are two kinds of server:
  - **Global**, added by an admin: the Open WebUI backend makes the requests and can use all auth modes above.
  - **User / direct**, added in the user's own Settings: the **browser** makes the requests, so CORS is required. These need the Direct Tool Servers permission and support OpenAPI only.
- Auth modes are shared with MCP (`none`, `bearer`, `session`, `system_oauth`, and header templates). OAuth 2.1 for OpenAPI connections is **unverified**. Which auth modes direct (user) servers offer is also **unverified**.
- Session and System OAuth connections **no longer forward cookies automatically**. Cookie forwarding is a separate "Forward cookies" toggle, off by default.

## Functions/Filters/Valves

- Functions are admin-installed Python plugins that **run inside the Open WebUI server**, and "execute arbitrary Python code".
- **Filter** methods:
  - `inlet(body)`: once per user turn.
  - `request(body)`: before **every** model call, including tool-loop hops. It was added in v0.11.2.
  - `stream(event)`: per streamed chunk.
  - `outlet(body)`: after the response. For pure API callers it only runs through `/api/chat/completed`.
- Filters can be global or per model, always-on or toggleable (`self.toggle = True`), and ordered by priority.
- **Valves** hold admin config and **UserValves** hold per-user config. UserValves are read through `__user__["valves"]`.
- Reserved arguments a filter can declare:
  - `__user__`: the full `UserModel` dump, including `id`, `email`, `name`, `role`, and `oauth` with the provider `sub`.
  - `__metadata__`: `chat_id`, `session_id`, `user_id`, …
  - `__oauth_token__`: the user's SSO token dict with `access_token`, refreshed automatically, or `None`. It is passed to Pipes, Tools, `inlet()` and `request()`, **but not to `outlet()`**.
  - Others: `__request__`, `__event_emitter__`, `__chat_id__`, `__message_id__`, `__model__` and `__id__`.
- **Calling an external service:** a filter is plain Python, so it can make an HTTP request (for example with `aiohttp` or `requests`) to memory-manager. It can authenticate with a Valve-held service token plus `__user__["id"]` or `__user__["email"]`, or as the user with `__oauth_token__["access_token"]`. An example use is an `inlet` that runs `memory_search` and injects the hits, or an `outlet` that proposes writes. HTTP calls from filters are documented in principle, but this specific pattern is **unverified**.
- **Pipe** functions add a custom "model" with its own logic. They receive the same reserved args, and since v0.8.x also the MCP and built-in tools in `__tools__`.

## Built-in memory

- **What it is:** per-user memory snippets with a `type` (`user` or `context`) and an optional `path`, stored in **Open WebUI's own database**. Memories are "scoped to your user account" and not shared.
  - Memories are managed manually in *Settings > Personalization > Memory*, or by the model.
  - By default memories are injected into the system context, with budgets `MEMORIES_USER_CHAR_LIMIT` / `MEMORIES_CONTEXT_CHAR_LIMIT` (2000 characters each).
  - With `ENABLE_MEMORY_BACKGROUND_REVIEW` (default off), the chat model periodically reviews the conversation and edits memories.
- **Native memory tools**, available with native function calling, are `add_memory`, `update_memory`, `replace_memory_content`, `delete_memory`, `search_memories`, `list_memories`, `list_memory_paths` and `read_memory_path`. They are switched on per model through *Workspace > Models > Builtin Tools > Memory*, which is enabled by default. The rework into persistent and contextual memories came in v0.10.0, and the "Memory System Context" toggle in v0.10.2.
- **API** (router prefix `/api/v1/memories`, from `routers/memories.py`):
  - `GET /` lists the caller's memories, which serves as an export.
  - `POST /add`, `/update`, `/query`, `/search`, `/paths`, `/path`, `/reindex` and `/reset`.
  - `DELETE /delete/user` and `DELETE /{id}`, plus `POST /{id}/update`.
  - There is no dedicated export endpoint (**unverified** beyond this list).
- **Disable** in one of three ways:
  - Globally with `ENABLE_MEMORIES=false` (Admin > General > Features > Memories). This hides the tab, blocks the API, stops injection and removes the tools; v0.11.4 fixed leaks behind this switch.
  - Per role or group with `USER_PERMISSIONS_FEATURES_MEMORIES=false`. Admins are exempt.
  - Injection only, keeping the tools, with `ENABLE_MEMORY_SYSTEM_CONTEXT=false`.

## Versions and deployment

| Version | Date | Note |
|---|---|---|
| **v0.11.4** (latest stable) | 2026-09-21 | Paginated MCP tool lists, fixes for the memory-off switch |
| v0.11.3 / v0.11.2 / v0.11.1 / v0.11.0 | 2026-08-31 / 08-31 / 08-25 / 07-27 | Filter `request()` (0.11.2) |
| v0.10.2 / v0.10.1 / v0.10.0 | 2026-07-01 / 06-29 / 06-29 | Memory rework, MCP OAuth scope and resource settings |
| v0.9.6 … v0.9.0 | 2026-06-01 … 04-20 | `{{USER_EMAIL}}` and `{{USER_ROLE}}`, `MCP_INITIALIZE_TIMEOUT` |

- **Cadence:** a new minor version every 1–2 months (0.9.0 Apr → 0.10.0 Jun → 0.11.0 Jul), with frequent patch releases. Pre-1.0 minors contain breaking changes.
- **Images:**
  - `ghcr.io/open-webui/open-webui:vX.Y.Z` (also `-cuda` and `-ollama`)
  - `:main` (moving)
  - `:slim` (defaults to PGVector)
  - The docs recommend pinning a version in production.
- **Helm:** chart `open-webui` in `open-webui/helm-charts`. Version **16.6.0** has `appVersion: 0.11.4`; the image `ghcr.io/open-webui/open-webui` defaults to the chart's `appVersion`. The same repository has `pipelines` and `terminals` charts.
- Open WebUI's own license is a BSD-3 derivative with a branding clause. The DCR code says the `client_name='Open WebUI'` identifier is "covered by LICENSE". This does not affect us.

## Implications for memory-manager

Open WebUI connects to our server as an admin-configured **MCP (Streamable HTTP)** connection, scoped to groups through Access Control. Our `instructions` and the `memory_guide` prompt will **not** reach the model, so tool descriptions must carry the guidance, for example the warning that note content is data. The per-request MCP session means `initialize` must stay cheap.

| Option | Possible? | Pros | Cons |
|---|---|---|---|
| **A — OAuth 2.1 per user against our AS** (`oauth_2.1` with DCR, or `oauth_2.1_static`) | **Yes**, natively and documented | Real per-user tokens from **our** AS, with audience binding, PKCE S256, `resource`, and scopes `memory:read`/`memory:write`. Same model as claude.ai, so there is no new trust path. Enterprise works through our Entra facade (the user logs in to our AS, which delegates to Entra). | Each user must enable the tool and consent once per chat. The tool cannot be a model default. The first flow must happen in the browser. Requires a stable `WEBUI_SECRET_KEY`. There is no CIMD, so our AS must allow DCR or we pre-register a confidential client. Open WebUI uses `client_secret_post` by default, so our DCR must accept that or advertise `none`. |
| **B — forward Open WebUI's OIDC token** (`system_oauth`) to our facade | **Possible, with conditions** | No second consent: it is seamless SSO when Open WebUI and our facade share the IdP (Entra). Refresh is automatic with `offline_access`. | The token is the **IdP's** access token, not ours. Open WebUI must request our API scope at login, for example `MICROSOFT_OAUTH_SCOPE=openid email profile offline_access api://<app>/<scope>`. The effect on Graph-dependent features such as profile pictures is **unverified**. Our server would have to act as a resource server for Entra-issued tokens (validate `aud`, `iss`, `oid`). That is a new auth path next to the facade and **needs an ADR**. It only works when users log in to Open WebUI through that IdP, so not for local accounts or API keys without a stored session. |
| **C — trusted user headers** (`ENABLE_FORWARD_USER_INFO_HEADERS` plus the signed `X-OpenWebUI-User-Jwt`, or header templates plus a static bearer or mTLS) | **Yes** | Zero user interaction, so it works as a default tool and for automations. The HS256 JWT gives a verifiable `sub`, `email` and `role` with a 5-minute expiry. `{{USER_GROUPS}}` could map to group namespaces. | Open WebUI is a fully trusted impersonator. The identity is Open WebUI's user id or email, not the IdP `oid`, so it needs a mapping. The HS256 secret is **global** and shared with every backend that receives the header (models, embeddings, …), so any of them could mint identities. Plain headers can be spoofed without mTLS or network policy. We would need a new "trusted proxy" auth mode, which is a security model change and **needs an ADR**. |
| **D — static personal tokens per user** | **No, not natively.** `bearer` is one key per connection, and MCP servers are admin-only. A workaround would be one admin connection per user, scoped by Access Control. | Uses our existing token model. | It does not scale, and admins see every user's token. Users cannot enter their own key for MCP. Direct (user) tool servers are OpenAPI-only and browser-side. |
| **E — single service token** (`bearer`) | **Yes** | Trivial. Good for a shared or team namespace or a demo. | No per-user identity: every user shares one namespace and one audit identity, and `me` is meaningless. This could be combined with C for identity. |

Recommendation for evaluation, not a decision: **A** is the only option that fits the current auth model without an ADR. We still need to check that our AS's DCR handles Open WebUI's registration request (`client_secret_post`, redirect `/oauth/clients/<id>/callback`). B and C would be separate decisions to make via ADR. An Open WebUI filter calling our HTTP API is a further non-MCP path (identity from `__user__` or `__oauth_token__`), but it needs a REST surface and a trust model, so it is out of scope for v1.

## Open questions

- Does our DCR endpoint accept `token_endpoint_auth_method=client_secret_post` with `client_name='Open WebUI'`? This needs a live test against v0.11.4.
- Are OAuth 2.1 modes offered for **OpenAPI** connections, or only for MCP? (unverified)
- Which auth modes do user-level direct OpenAPI servers support? (unverified)
- What tool name length or schema features does each model provider accept with the `<server_id>_` prefix? We should keep server ids and tool names short. (unverified)
- In which version was `FORWARD_USER_INFO_HEADER_JWT_SECRET` introduced? It is present at v0.11.4 but has no CHANGELOG entry. (unverified)
- For B with Entra: does adding a custom API scope to `MICROSOFT_OAUTH_SCOPE` break Open WebUI features that call Graph with that token? (unverified)
- For `system_oauth`, which token does Open WebUI pick when a user has several OAuth sessions or providers? The code prefers the `oauth_session_id` cookie and falls back to the most recent session.

## Sources

- https://docs.openwebui.com/features/extensibility/mcp (source: https://github.com/open-webui/docs/blob/main/docs/features/extensibility/mcp.mdx)
- https://docs.openwebui.com/reference/env-configuration (source: https://github.com/open-webui/docs/blob/main/docs/reference/env-configuration.mdx)
- https://docs.openwebui.com/features/authentication-access/auth/sso (source: https://github.com/open-webui/docs/blob/main/docs/features/authentication-access/auth/sso/index.mdx)
- https://docs.openwebui.com/features/extensibility/plugin/tools/openapi-servers/open-webui (source: https://github.com/open-webui/docs/blob/main/docs/features/extensibility/plugin/tools/openapi-servers/open-webui.mdx)
- https://docs.openwebui.com/features/extensibility/plugin/tools/openapi-servers/mcp (mcpo page; source: https://github.com/open-webui/docs/blob/main/docs/features/extensibility/plugin/tools/openapi-servers/mcp.mdx)
- https://docs.openwebui.com/features/extensibility/plugin/functions/filter (source: https://github.com/open-webui/docs/blob/main/docs/features/extensibility/plugin/functions/filter.mdx)
- https://docs.openwebui.com/features/extensibility/plugin/functions/pipe (source: https://github.com/open-webui/docs/blob/main/docs/features/extensibility/plugin/functions/pipe.mdx)
- https://docs.openwebui.com/features/extensibility/plugin/development/reserved-args (source: https://github.com/open-webui/docs/blob/main/docs/features/extensibility/plugin/development/reserved-args.mdx)
- https://docs.openwebui.com/features/chat-conversations/memory (source: https://github.com/open-webui/docs/blob/main/docs/features/chat-conversations/memory.mdx)
- https://docs.openwebui.com/getting-started/updating (source: https://github.com/open-webui/docs/blob/main/docs/getting-started/updating.mdx)
- https://github.com/open-webui/open-webui/releases
- https://github.com/open-webui/open-webui/blob/v0.11.4/CHANGELOG.md
- https://github.com/open-webui/open-webui/blob/v0.11.4/backend/open_webui/utils/mcp/client.py
- https://github.com/open-webui/open-webui/blob/v0.11.4/backend/open_webui/utils/tools.py
- https://github.com/open-webui/open-webui/blob/v0.11.4/backend/open_webui/utils/headers.py
- https://github.com/open-webui/open-webui/blob/v0.11.4/backend/open_webui/utils/oauth.py
- https://github.com/open-webui/open-webui/blob/v0.11.4/backend/open_webui/utils/middleware.py
- https://github.com/open-webui/open-webui/blob/v0.11.4/backend/open_webui/env.py
- https://github.com/open-webui/open-webui/blob/v0.11.4/backend/open_webui/routers/memories.py
- https://github.com/open-webui/mcpo
- https://github.com/open-webui/helm-charts/blob/main/charts/open-webui/Chart.yaml
