# Ory Hydra as the authorization server for a self-hosted MCP server (claude.ai + Claude Code)

Retrieved: 2026-10-06

Method:
- Hydra behaviour was checked against the **v26.2.0 source**, including the vendored `fosite/`, and its `spec/config.json` schema. The `ory/docs`, `ory/k8s`, `ory/hydra-maester` and `ory/mcp` repos were read from clones on 2026-10-06.
- ory.sh, ory.com, dev.to and getlarge.eu were blocked by the egress proxy. Doc URLs below are the canonical ory.sh paths of the files read from the `ory/docs` repo.

## 0. Versions

| Component | Latest | Date | Source |
|---|---|---|---|
| Ory Hydra | **v26.2.0** (`oryd/hydra:v26.2.0`, also `-distroless`) | 2026-03-20 | [H1], [H2] |
| Previous Hydra | v25.4.0 | 2025-11-07 | [H1] |
| Helm charts `ory/k8s` (hydra, hydra-maester, kratos, kratos-selfservice-ui-node, oathkeeper, keto) | **chart v0.64.0**. The hydra chart has appVersion v26.2.0; kratos-selfservice-ui-node has appVersion v0.13.0-4 | repo HEAD 2026-09-30 | [K1] |
| Hydra Maester | v0.0.42 (image published 2026-05-25) | — | [K1], [M1] |

## 1. Dynamic Client Registration (RFC 7591 / RFC 7592)

**How it is enabled:**
- Config key `oidc.dynamic_client_registration.enabled: true`. The default is **false**. [H3]
- Optional key `oidc.dynamic_client_registration.default_scope` (array). [H3]

**Endpoints (public port 4444):**
- `POST /oauth2/register` registers a client.
- `GET`, `PUT` and `DELETE /oauth2/register/{id}` implement RFC 7592 management.
- The response includes a `registration_access_token` and a `registration_client_uri`. RFC 7592 calls must present that token.

Source: [H4], [D1]

**Authentication:** none. Registration is open to anyone who can reach the public endpoint once DCR is enabled. The only gate in the handler is the config flag check, which returns `404 "Dynamic registration is not enabled."` when DCR is off. The endpoint carries the rate-limit bucket `hydra-public-high`. [H4]

**Restrictions on fields a DCR client can register:**
- The client cannot choose its own `client_id`; Hydra assigns a UUIDv4. [H4]
- The client cannot choose its own `client_secret`. [H4]
- A secret is returned only for confidential clients. [H4]
- The validator rejects these fields: `metadata`, `access_token_strategy`, `skip_consent` and `skip_logout_consent`. [H5]

**Scope caveat (from the schema):** "The OpenID Connect Dynamic Client Registration specification has no concept of whitelisting OAuth 2.0 Scope… users can overwrite this default by setting the `scope` key in the registration payload, effectively disabling the concept of whitelisted scopes." The same applies to the client's `audience` allow-list. [H3]

**Security caveats:**
- Registration is open and unauthenticated, so a client can request any scope or audience. This makes the consent/login app the real policy enforcement point.
- Claude's DCR "registers a new client on every fresh connection", so the client table grows without bound. Plan for periodic cleanup. [C2]
- Ory's doc warns that the `registration_access_token` is sensitive. [D1]

## 2. Client ID Metadata Documents (CIMD)

**Not supported** in v26.2.0:
- There is no `client_id_metadata_document_supported` field in the discovery code. [H6]
- The open feature request is **ory/hydra#4061 "Support 'Client ID Metadata Document' (CIMD)"**, opened 2026-01-17 with label `feat`. No maintainer plan was visible. [I1]

**Consequence:** claude.ai falls back to **DCR**. Claude selects CIMD only if the AS metadata contains both `client_id_metadata_document_supported: true` and `none` in `token_endpoint_auth_methods_supported`. [C2]

## 3. RFC 8707 `resource` vs Hydra `audience`

**Hydra does not read `resource`:**
- fosite's `GetAudiences` reads only the form field **`audience`** (space-separated or repeated).
- That audience is checked by the AudienceStrategy against the client's registered `audience` allow-list.
- There is no `resource` handling anywhere in the Hydra or fosite v26.2.0 source.

Source: [H7]. The canonical/identity-platform-login-ui issue #966 (2026-09-08, closed) confirms this: "upstream Ory Hydra uses the non-standard `audience` parameter… does not support RFC 8707 `resource`". [I2]

**What Claude sends:** `resource=<canonical MCP URL>` on the authorize and token requests. Claude never sends `audience`. [C3]

**Workaround that works with Hydra's design:**
- The **consent app** sets `grant_access_token_audience` when it accepts the consent request. Hydra applies the granted audience to the access token without condition (`request.GrantAudience(...)` in `updateSessionWithRequest`). The field comment says it "Should be a subset of `requested_access_token_audience`", but **no server-side subset check** was found in v26.2.0. [H8]
- So the consent app can parse `resource` from the consent request's original `request_url` (the authorization URL, which Hydra stores in the flow), validate it against an allow-list containing your MCP URL, and grant it as the audience.
- This is the approach issue #966 describes ("map the parsed value(s) to the audience list when accepting the consent request"). [I2]
- The token-endpoint `resource` (sent again on refresh) is ignored, but the granted audience persists across refresh (`flow_refresh` copies the original audience). [H7]

**Alternatives:**
- Pre-registered static client: set `audience: [<mcp-url>]` on the client. The client still has to send `audience=`, and Claude won't, so the consent-app mapping is still needed.
- Validate tokens by **introspection plus a client_id or scope check** in the MCP server instead of `aud`. This is weaker and violates the spec's MUST.

## 4. RFC 9728 Protected Resource Metadata

Hydra is only the authorization server and does not serve PRM. No `oauth-protected-resource` handler exists in the source. [H6]

**The MCP server must serve PRM itself:**
- Serve `/.well-known/oauth-protected-resource` (and/or the path-suffixed variant) with `resource` equal to the exact MCP URL and `authorization_servers` equal to the Hydra issuer URL.
- Answer unauthenticated requests with `401` and `WWW-Authenticate: Bearer resource_metadata="…"`.
- Claude uses only the first AS entry.

Source: [S1], [C2]

## 5. RFC 8414 vs OIDC discovery

**Both are served:**
- `/.well-known/oauth-authorization-server` (constant `OauthAuthorizationServerPath` in `oauth2/handler.go`).
- `/.well-known/openid-configuration`.

Source: [H6], [D2]

**Advertised values** (from the metadata code) [H6]:
- `code_challenge_methods_supported: ["plain","S256"]`.
- `token_endpoint_auth_methods_supported: ["client_secret_post","client_secret_basic","private_key_jwt","none"]`.
- `registration_endpoint`, `revocation_endpoint` and `end_session_endpoint`.
- grant types: `authorization_code`, `implicit`, `client_credentials`, `refresh_token` and `device_code`.

**`authorization_response_iss_parameter_supported`** (RFC 9207) does **not** appear in the v26.2.0 metadata struct. Under the 2026-07-28 spec this is only a SHOULD for the AS. [H6]

**Issuer:** the issuer must match token `iss`. Claude flags issuer mismatch as a common failure. [C3]

## 6. PKCE, refresh tokens, revocation, introspection, token format

**PKCE:**
- `oauth2.pkce.enforced` applies to all clients. `oauth2.pkce.enforced_for_public_clients` applies to public clients only. [H3]
- Hydra still advertises `plain`, so S256 cannot be made the only method by config.
- Claude always sends S256. [C2]

**Refresh tokens:**
- Refresh tokens are **single-use and rotated**: "a new refresh token is issued, and the previous token is invalidated". [D3]
- `oauth2.grant.refresh_token.rotation_grace_period` defaults to 0s, with a maximum of 5 min unless a reuse count is set. `rotation_grace_reuse_count` is also available. [H3]
- `ttl.refresh_token` defaults to 720h. [H3]
- `ttl.access_token` defaults to 1h. [H3]
- A refresh token is issued only if the client requests `offline` or `offline_access`. Claude adds `offline_access` when the AS lists it, and Hydra always includes it in `scopes_supported`. [H3], [C2]

**Revocation and introspection:**
- `POST /oauth2/revoke` (public). [H6]
- **Introspection (RFC 7662):** `POST /admin/oauth2/introspect` on the **admin** port. [H6]

**Token format:**
- `strategies.access_token` is `opaque` by default or `jwt`. The schema says "jwt is a bad idea". [H3]
- With opaque tokens, the MCP server must introspect each token (with caching) via the admin API. JWTs can be verified via JWKS, but cannot be revoked before they expire. [D4]
- The strategy can be set per client by an admin, not via DCR. [H5]
- `oauth2.allowed_top_level_claims` and `mirror_top_level_claims` control custom claims. [H3]

## 7. Login/consent app, skip consent, and DCR clients

**A login/consent app is required:**
- Hydra delegates both login and consent through `urls.login` and `urls.consent`. In Helm these are `hydra.config.urls.login` and `hydra.config.urls.consent`. [D5]
- **Ory Account Experience** is the default UI only on **Ory Network**. Self-hosted deployments must run their own login/consent UI. [D6]

**kratos-selfservice-ui-node implements Hydra login/consent:**
- `HYDRA_ADMIN_URL` / `ORY_SDK_URL` point it at the Hydra admin API.
- `CSRF_COOKIE_SECRET` is needed for `/consent`.
- `REMEMBER_CONSENT_SESSION_FOR_SECONDS` sets how long consent is remembered.
- `TRUSTED_CLIENT_IDS` lists clients that skip the consent screen.
- `SESSION_EXTRA_TRAITS_ACCESS_TOKEN` copies identity traits into the access token.
- It does **not** map RFC 8707 `resource` into an audience, so for that you need a fork or a small custom consent service.

Source: [U1]

**Skip consent:**
- Set the client's `skip_consent` flag. It can **only be set via the admin API**, and DCR rejects it. [H5], [D7]
- **DCR clients such as claude.ai's always go through the consent app.**
- Since client IDs are random UUIDs, they cannot be put in `TRUSTED_CLIENT_IDS` ahead of time. Either show a consent screen, or have the custom consent app auto-accept based on `client.redirect_uris` (for example, exactly `https://claude.ai/api/mcp/auth_callback`). The spec says the consent screen MUST display the redirect hostname. [S2]

**Claude Code loopback redirects:**
- fosite's RFC 8252 port-agnostic match (`isMatchingAsLoopback`) only applies to **IP-literal** loopback hosts, because it uses `net.ParseIP(host).IsLoopback()`. **`http://localhost:<port>/callback` does not match port-agnostically.** [H9]
- With DCR this is fine, because Claude Code registers its exact port at registration time.
- For a pre-registered static client, use `claude mcp add … --client-id … --callback-port <fixed>` and register exactly `http://localhost:<fixed>/callback`. [C4]

## 8. Kubernetes: Helm charts and Hydra Maester

**Helm:**
- `helm install ory/hydra` (chart 0.64.0) bundles **Hydra Maester**. Disable it with `maester.enabled=false`. [D5], [K1]
- Other charts in the repo: kratos, kratos-selfservice-ui-node, oathkeeper (+ maester), keto and example-idp. [K1]

**Hydra Maester:**
- It manages the CRD **`oauth2clients.hydra.ory.sh`** (`OAuth2Client`). The API group in code is `hydra.ory.sh/v1alpha1`; the Helm doc's spelling `hydra.ory.com` is a typo. [D5], [M2]
- Spec fields include `grantTypes`, `responseTypes`, `redirectUris`, `scope`/`scopeArray`, `audience`, `tokenEndpointAuthMethod`, **`skipConsent`**, `accessTokenStrategy`, `metadata`, the per-grant token lifespans, and `secretName`.
- The client ID and secret come from a K8s Secret, using the keys `CLIENT_ID` and `CLIENT_SECRET` by default. [M2], [M1]
- This is the GitOps way to **pre-register a static client**, for example a public client with `tokenEndpointAuthMethod: none` and `skipConsent: true`.
- claude.ai can use such a client through the custom connector's **"Use your own OAuth client"** option. Register the redirect `https://claude.ai/api/mcp/auth_callback`, and leave the secret blank for a public client. [C1]
- This avoids enabling open DCR. The same client, or a second one, can serve Claude Code via `--client-id/--callback-port`.

## 9. Known reports of Hydra with MCP / Claude

- **Ory's own packages:**
  - `ory/mcp` (renamed from `mcp-oauth-provider`; HEAD 2026-09-30) publishes **`@ory/mcp-oauth-provider`**: "supports both Ory Network and Ory Hydra as backend providers… PKCE… client registration… token introspection".
  - It also publishes `@ory/mcp-access-control`, which does JWT validation via JWKS with a configurable audience.
  - Both are TypeScript only.

  Source: [O1]
- **Ory blog:** "Securing AI agents with Ory Hydra and MCP: A complete integration guide" [O2]. This was found by search but **not fetched**, because ory.com was blocked.
- **Community blog:** "Securing MCP Servers with OAuth2: Ory Hydra + Claude Code + ChatGPT" (getlarge.eu, mirrored on dev.to) [O3]. It was **not fetched** (blocked). It is reported in search results as a working Hydra-plus-Claude-Code setup.
- **Community PR:** JalapenoLabs/Elysium PR #24 "MCP server at /api/mcp, with OAuth through Ory Hydra" [O4]. Found by search, not reviewed.
- **Canonical identity-platform-login-ui #966:** the RFC 8707 to `audience` mapping in the login/consent UI on top of Hydra, motivated by MCP. [I2]
- **ory/hydra#4061:** CIMD request motivated by MCP. [I1]

## 10. Bottom line for the design

The Hydra (+ Kratos) setup works for claude.ai and Claude Code if all of the following hold:
1. The MCP server serves **PRM** and a **401 challenge** itself.
2. Clients come either from **DCR** (`oidc.dynamic_client_registration.enabled=true`, open endpoint, cleanup job) or from a **Maester-managed static public client** entered in the claude.ai dialog.
3. A **custom consent step maps `resource` into `grant_access_token_audience`**. Without it, tokens carry no MCP audience and the server cannot satisfy the spec's audience-binding MUST.
4. The MCP server validates tokens via **admin-port introspection** (opaque, the default) or **JWKS** (JWT strategy). It checks `active`, `aud` (the canonical MCP URL), `iss` and scope.
5. **PKCE is enforced** for public clients.
6. The Hydra **public endpoints are reachable from 160.79.104.0/21** and respond in under 10 s.

---

## Sources
- [H1] Hydra tags — https://github.com/ory/hydra/tags (v26.2.0 commit dated 2026-03-20; v25.4.0 2025-11-07)
- [H2] Docker Hub oryd/hydra tags — https://hub.docker.com/r/oryd/hydra/tags
- [H3] Hydra config schema v26.2.0 — https://github.com/ory/hydra/blob/v26.2.0/spec/config.json
- [H4] DCR handler — https://github.com/ory/hydra/blob/v26.2.0/client/handler.go
- [H5] DCR validator / client fields — https://github.com/ory/hydra/blob/v26.2.0/client/validator.go ; https://github.com/ory/hydra/blob/v26.2.0/client/client.go
- [H6] Discovery, introspect, and revoke handlers — https://github.com/ory/hydra/blob/v26.2.0/oauth2/handler.go
- [H7] fosite audience handling — https://github.com/ory/hydra/blob/v26.2.0/fosite/audience_strategy.go ; fosite/handler/oauth2/flow_refresh.go
- [H8] Consent granted audience — https://github.com/ory/hydra/blob/v26.2.0/flow/consent_types.go ; flow/flow.go ; oauth2/handler.go (`updateSessionWithRequest`)
- [H9] Loopback redirect matching — https://github.com/ory/hydra/blob/v26.2.0/fosite/authorize_helper.go
- [D1] Ory docs, OAuth2 clients / OpenID DCR — https://www.ory.sh/docs/hydra/guides/oauth2-clients (repo: ory/docs docs/hydra/guides/oauth2-clients.mdx)
- [D2] Well-known endpoint discovery — https://www.ory.sh/docs/oauth2-oidc/wellknown-endpoint-discovery
- [D3] Refresh token grant / rotation — https://www.ory.sh/docs/oauth2-oidc/refresh-token-grant
- [D4] JWT access tokens — https://www.ory.sh/docs/oauth2-oidc/jwt-access-token
- [D5] Hydra Kubernetes Helm chart — https://www.ory.sh/docs/hydra/self-hosted/kubernetes-helm-chart
- [D6] Custom login/consent flow — https://www.ory.sh/docs/oauth2-oidc/custom-login-consent/flow ; https://www.ory.sh/docs/hydra/guides/custom-ui-oauth2
- [D7] Skip consent — https://www.ory.sh/docs/oauth2-oidc/skip-consent
- [K1] ory/k8s Helm charts — https://github.com/ory/k8s/tree/master/helm/charts
- [M1] Hydra Maester README — https://github.com/ory/hydra-maester
- [M2] OAuth2Client CRD types — https://github.com/ory/hydra-maester/blob/master/api/v1alpha1/oauth2client_types.go
- [U1] kratos-selfservice-ui-node README — https://github.com/ory/kratos-selfservice-ui-node
- [I1] ory/hydra#4061 CIMD — https://github.com/ory/hydra/issues/4061
- [I2] canonical/identity-platform-login-ui#966 RFC 8707 — https://github.com/canonical/identity-platform-login-ui/issues/966
- [O1] ory/mcp — https://github.com/ory/mcp
- [O2] Ory blog (not fetched) — https://www.ory.com/blog/mcp-server-oauth-with-ory-hydra-authentication-ai-agent-integration-guide
- [O3] Community blog (not fetched) — https://getlarge.eu/blog/securing-mcp-servers-with-oauth2-ory-hydra-claude-code-chatgpt/
- [O4] Elysium PR (not reviewed) — https://github.com/JalapenoLabs/Elysium/pull/24
- [S1] MCP AS discovery 2026-07-28 — https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/authorization-server-discovery
- [S2] MCP security considerations 2026-07-28 — https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations
- [C1] Claude: add custom connector — https://claude.com/docs/connectors/custom/add-unlisted
- [C2] Claude: connector authentication — https://claude.com/docs/connectors/building/authentication
- [C3] Claude: connector troubleshooting — https://claude.com/docs/connectors/building/troubleshooting
- [C4] Claude Code MCP — https://code.claude.com/docs/en/mcp
