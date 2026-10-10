# Open WebUI as a memory-manager client

Retrieved: 2026-10-07, live spike 2026-10-10 (#142, WP-42) · Tested version: 0.11.4

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

**Re-verified 2026-10-10** for the pinned local stack (#141, WP-42,
`deploy/openwebui/docker-compose.yml`): v0.11.4 is still the latest release
(GitHub releases API, published 2026-09-21, tag commit `8bd8b4f`). The
`0.11.4` and `v0.11.4` image tags on `ghcr.io/open-webui/open-webui` share
one index digest, `sha256:9591b13f13843c7721c2b8eaf7382846c81b3ffe126526d1888d1fed50c6a33f`;
the previous two minors for a later version matrix (WP-46) are 0.10.2 and
0.9.6. The OCI image carries **no** `HEALTHCHECK` of its own (the compose
file defines one), but does carry `curl` and `jq`; its `/health` endpoint
returns `{"status": true}`. Pulling it is about 1.65 GB. Ollama is pinned
at `docker.io/ollama/ollama:0.40.2` for the same stack's opt-in `ollama`
profile. Source: GitHub releases API and `ghcr.io` manifest, both retrieved
2026-10-10.

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

## Spike results (2026-10-10)

Driven live against the pinned stack (#141/#142,
`deploy/openwebui/docker-compose.yml`, `scripts/openwebui-up.sh`,
`tests/e2e/openwebui_driver.py`) with memory-manager's embedded AS
(ADR-0004) enabled: `LOGIN_MODE=password`, `PORT=18081`,
`PUBLIC_URL=http://localhost:18081`. Tested version: 0.11.4 throughout
this section; every claim below is either an observed, reproduced result
or a code citation, never recall.

### Networking: a shared network namespace, not a DNS name

The SDK's own issuer check (`.venv/.../mcp/server/auth/routes.py`
`validate_issuer_url`, `v2.3.0` as installed) only accepts `https` or
`localhost`/`127.0.0.1`/`[::1]` - the compose DNS name `memory-manager`
fails it, and Open WebUI's **backend**, not only the browser, must reach
`PUBLIC_URL` directly (DCR, token exchange). The fix verified live:
`open-webui` joins memory-manager's own network namespace
(`network_mode: "service:memory-manager"`; podman-compose 1.3 turns this
into `--network=container:<name>`, confirmed against
`podman_compose.py`'s `get_net_args_from_network_mode`), memory-manager
listens on `PORT=18081` inside that shared namespace, and both services'
host-facing ports are published on memory-manager's own `ports:` list
(`127.0.0.1:18081:18081`, `127.0.0.1:3000:8080`). Verified:

- `curl -s localhost:18081/.well-known/oauth-protected-resource` from the
  host → `{"resource":"http://localhost:18081/mcp", ...}`.
- `podman exec mm-openwebui_open-webui_1 curl -fsS
  http://localhost:18081/.well-known/oauth-authorization-server` → 200
  with a full AS metadata document (same loopback, from inside
  open-webui's shared namespace).
- `podman exec mm-openwebui_open-webui_1 getent hosts scripted-model` →
  resolves (`10.89.1.6 scripted-model.dns.podman`) - the shared namespace
  still carries the project's own DNS, so open-webui's existing
  `OPENAI_API_BASE_URLS=http://scripted-model:8091/v1` keeps working
  unchanged.

**Production consequence (#154):** with a real `https://` `PUBLIC_URL`
this whole workaround disappears (`validate_issuer_url` passes `https`
unconditionally) - ordinary same-cluster service DNS is enough, and no
network-namespace sharing is needed. What stays true in both cases: Open
WebUI's **backend**, not just a user's browser, must have egress to
`PUBLIC_URL` (DCR and token exchange are server-to-server), so a
production NetworkPolicy must allow it, not only ingress from users'
browsers.

### DCR and the per-user OAuth flow: both work, unmodified

`tests/e2e/openwebui_driver.py connect` (signup as the first/admin user →
`POST /api/v1/configs/oauth/clients/register?type=mcp` → `POST
/api/v1/configs/tool_servers` with `auth_type=oauth_2.1` → `GET
/oauth/clients/mcp:<id>/authorize`, following redirects through
memory-manager's `/login?pending=...` form, the admin password, and back
to Open WebUI's own callback) **completed end to end with no `?error=`**,
confirming ADR-0011's load-bearing assumption and closing the note's
previous top open question. Observed (never a token or secret value, only
shapes - `oauth_clients.client_info`/`oauth_tokens`/`oauth_pending` in
memory-manager's own Postgres, `user`/`oauth_session` in Open WebUI's
SQLite):

| Code finding (already established) | Live confirmation |
|---|---|
| DCR: `client_name='Open WebUI'`, redirect `{WEBUI_URL}/oauth/clients/mcp:<id>/callback`, `token_endpoint_auth_method=client_secret_post` default (`utils/oauth.py#L503-L645`) | `oauth_clients.client_info` carries exactly this: `client_name="Open WebUI"`, `redirect_uris=["http://localhost:3000/oauth/clients/mcp:memmgr/callback"]`, `token_endpoint_auth_method="client_secret_post"`, and a `client_secret` is present (encrypted at rest, ADR-0004 addendum) |
| PKCE S256, `resource` from PRM verbatim on `/authorize` (`utils/oauth.py#L862-L877`, `#L776-L797`) | `oauth_pending` (kind `authorize`): `params->>'resource' = 'http://localhost:18081/mcp'` (byte-for-byte the PRM `resource`), a `code_challenge` is set |
| Discovery via anonymous `initialize` + `WWW-Authenticate` (`utils/oauth.py#L390-L466`) | memory-manager's access log: `POST /mcp` → `401`, immediately followed by `GET /.well-known/oauth-protected-resource/mcp` → `200`, both **before** any `/authorize` call - triggered by saving the tool-server connection, not by a user's own consent click |
| `client_id` path segment `mcp:<server_id>` is Open WebUI's own lookup key, not the AS-issued `client_id` (unverified before this spike) | Confirmed: the AS-issued `client_id` in `oauth_clients`/`oauth_tokens` is a UUID (`9d3acf46-...`); `mcp:memmgr` only ever appears in Open WebUI's own URLs (`/oauth/clients/mcp:memmgr/{authorize,callback}`) and its SQLite `oauth_session.provider` column |
| `oauth_session` stores the per-user token server-side, keyed by provider (module docstring, "Auth and identity forwarding") | `oauth_session` (Open WebUI SQLite): one row, `provider='mcp:memmgr'`, `user_id` = the signed-in user, `token` present (460 bytes, encrypted - length only read, never the content) |

### M2 confirmed live: Open WebUI is mislabeled `"claude-code"` in our own audit trail

`_client_label_for` (`src/memory_manager/auth/provider.py:641-647`) labels
a registered client `"claude-ai"` only if a redirect URI's host is in
`_CLAUDE_AI_HOSTS`, else `"claude-code"` - there is no third label.
Live: `oauth_tokens.client_label = 'claude-code'` for the Open WebUI
connection just completed (redirect host `localhost`, not a claude.ai
host). This is **not** a WP-42 blocker (ADR-0011 does not depend on the
label), but every audit entry and `token list` row for an Open WebUI user
will read "claude-code" until #143/#144 add a real label - tracked there,
not fixed here.

### UserValves and filter token access (#142's own scope)

A throwaway probe `Filter` function (installed via `POST
/api/v1/functions/create`, exercised through one `/api/chat/completions`
turn, then deleted via `DELETE /api/v1/functions/id/<id>/delete` - the
code never left `/tmp`, per this task's Blast-Radius) observed, live:

- `__user__` is a flat dict with (among others) `id`, `email`, `role`,
  `oauth`, `settings` and `valves` as top-level keys - confirming
  "Functions/Filters/Valves"'s existing claim.
- Setting a `UserValves` field through `POST
  /api/v1/functions/id/<id>/valves/user/update` round-trips, **in
  plaintext**, as `user.settings->'functions'->'valves'->'<function_id>'`
  in Open WebUI's own database (confirmed by reading the row directly:
  `{"functions": {"valves": {"probe_wp42": {"marker": "hello-from-probe"}}}}`)
  - `ENABLE_VALVE_ENCRYPTION` was not set (default `false`, `env.py:767`),
    matching the code finding; the Fernet-encrypted case
    (`ENABLE_VALVE_ENCRYPTION=true`) was not re-run live in this spike
    (a second `openwebui-data` volume lifecycle for one already-settled
    code citation), so that half stays a code finding
    (`utils/valves.py#L13-L25`), not a live one.
  - The same resolved `UserValves` instance is *also* available as
    `__user__["valves"]` (a typed object, `marker='hello-from-probe'`
    observed directly) - the two paths coexist: `__user__["valves"]` is
    the per-function, already-resolved view; `__user__["settings"]` is
    the raw, persisted JSON every other function (and, unencrypted, every
    DB reader) can see too.
- `__oauth_token__` was `None` in both `inlet` and `outlet` for the user
  who just completed the `mcp:memmgr` OAuth connection - confirming
  `get_system_oauth_token` filters out `mcp:` sessions
  (`utils/tools.py`/`utils/middleware.py#L3796-L3826`).
- The undocumented internal path **does** reach the MCP token:
  `__request__.app.state.oauth_client_manager.get_oauth_token(__user__["id"], "mcp:memmgr")`
  returned a non-`None` value inside `inlet` (checked as a boolean only,
  never logged) - confirming code finding 4's "undocumented" internal API
  actually works, not merely that it exists in source.

### Owner hints for #143/#144/#155 (not decided here)

- **(a)** A filter could fetch the MCP token through the internal API
  above instead of requiring a personal token pasted into `UserValves`
  (ADR-0011 Option A's "Filter" half) - demonstrated reachable, not
  evaluated as a design option; ADR-0011 already reserves that choice to
  the owner.
- **(b)** UserValves are plaintext by default and readable by every
  function with access to `__user__["settings"]`, confirmed live above -
  docs for #155 must say plainly that `ENABLE_VALVE_ENCRYPTION=true` is
  required before any function stores something sensitive in a
  `UserValves` field (a personal memory-manager token, for instance).

### Predicted AS mismatches for #143/#144 (ADR-0004/ADR-0009 vs. Open WebUI's client)

| # | Mismatch | Status |
|---|---|---|
| M1 | Issuer/network: our SDK rejects a non-`https`, non-loopback issuer | **Resolved for this spike** by the shared network namespace above; **#154** (Helm/production) needs `https://` instead, not this workaround |
| M2 | `_client_label_for` has no "Open WebUI" label | **Confirmed live** above; cosmetic (audit trail only), not a functional blocker |
| M3 | Refresh-token replay detection revokes the whole family (`provider.py:519-527`) on reuse, while Open WebUI refreshes without a lock 5 minutes before expiry on every request (`utils/oauth.py#L1060-L1180`) | **Code finding only** - reproducing the race needs two genuinely concurrent Open WebUI requests inside the access token's last 5 minutes, a timing window too narrow to force cheaply in this spike; left for #143/#144 to design a fix (e.g. a short grace window on first-reuse) and test for themselves |
| M4 | A server-side preflight `GET /authorize` (no redirect follow) could leave an orphan `oauth_pending` row per admin click, and every Open WebUI user shares one egress IP against our `oauth_limiter` (`http.py:160,1005-1007`) | **Partially confirmed**: the anonymous-`initialize`-plus-discovery preflight on `/mcp` (above) is real and happens on every connection save, but this spike's own `/authorize` call always completed (no abandoned `oauth_pending` row observed after a successful run); the shared-egress-IP rate-limit bucket is a code finding only (this spike never drove two users through the same connection in parallel) |
| M5 | `LOGIN_MODE=password` has exactly one subject (`login_password.py#L100-L104`) | **Confirmed by construction**: this spike's own admin user is the only subject `LOGIN_MODE=password` can ever authenticate; a second real user needs `LOGIN_MODE=oidc` plus a mock IdP (#144) |

None of the above broke ADR-0011's load-bearing assumption (the AS
accepting Open WebUI's DCR request and completing the per-user OAuth
dance) - no STOP/Gate condition from this task's contract was triggered.

## Open questions

None of the following blocks WP-42 … WP-46: ADR-0011 already picked
Option A (per-user OAuth 2.1 for tools, personal tokens for the filter),
which is the one path this spike had to prove and did (see "Spike
results" above). Every remaining question below belongs to a path ADR-0011
left for a later ADR (B, C) or to a provider-specific detail no planned
WP depends on; each line says why.

- **Resolved, live:** does our DCR endpoint accept `token_endpoint_auth_method=client_secret_post` with `client_name='Open WebUI'`? Yes - Tested version: 0.11.4, see "Spike results" above (`tests/e2e/openwebui_driver.py connect` completed with no `?error=`).
- Are OAuth 2.1 modes offered for **OpenAPI** connections, or only for MCP? (still unverified) - blocks WP-4x: no, Option A only ever uses the MCP connection; OpenAPI is not part of any WP-42 … WP-46 task.
- Which auth modes do user-level direct OpenAPI servers support? (still unverified) - blocks WP-4x: no, same reason.
- What tool name length or schema features does each model provider accept with the `<server_id>_` prefix? (still unverified) - blocks WP-4x: no for WP-42 (this spike kept `server_id` to 6 characters, `memmgr`, well under the 47-character budget the existing "MCP support" table already derived); a concrete provider-by-provider limit is only relevant once #146/#150 run real models against real providers.
- In which version was `FORWARD_USER_INFO_HEADER_JWT_SECRET` introduced? (still unverified) - blocks WP-4x: no, it is ADR-0011 Option C's mechanism, explicitly deferred to its own future ADR.
- For B with Entra: does adding a custom API scope to `MICROSOFT_OAUTH_SCOPE` break Open WebUI features that call Graph with that token? (still unverified) - blocks WP-4x: no, Option B (`system_oauth`) is deferred to ADR-0006 §8's resource-server mode, not part of WP-42 … WP-46.
- For `system_oauth`, which token does Open WebUI pick when a user has several OAuth sessions or providers? (code-level answer only: prefers the `oauth_session_id` cookie, falls back to the most recent session) - blocks WP-4x: no, same reason as the previous line.
- M3 (refresh-token replay vs. Open WebUI's lock-free early refresh) and M4's shared-egress-IP rate-limit bucket - both code findings only (see "Predicted AS mismatches" above); not re-opened as questions here, but #143/#144 must read that table before changing `provider.py`/`http.py`.

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
- https://github.com/open-webui/open-webui/blob/v0.11.4/backend/open_webui/routers/configs.py (spike, 2026-10-10: `/oauth/clients/register`, `/tool_servers`)
- https://github.com/open-webui/open-webui/blob/v0.11.4/backend/open_webui/routers/functions.py (spike, 2026-10-10: `/create`, `/id/{id}/update`, `/id/{id}/valves/user/update`)
- https://github.com/open-webui/open-webui/blob/v0.11.4/backend/open_webui/routers/auths.py (spike, 2026-10-10: `/signup`, `create_session_response`)
- memory-manager's own `.venv` at `mcp==2.3.0` (`uv.lock`): `mcp/server/auth/routes.py` `validate_issuer_url` (spike, 2026-10-10)
- `podman_compose.py` (Debian package `podman-compose` 1.3.0, `/usr/lib/python3/dist-packages/podman_compose.py`): `get_net_args_from_network_mode` (spike, 2026-10-10)
- https://github.com/open-webui/mcpo
- https://github.com/open-webui/helm-charts/blob/main/charts/open-webui/Chart.yaml
