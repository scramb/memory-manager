# Enterprise scale: Entra ID, MCP sessions under load balancing, Postgres at scale

Retrieved: 2026-10-07 · Feeds: [ADR-0006](../adr/0006-enterprise-auth-entra.md), [ADR-0007](../adr/0007-storage-backend.md), [ADR-0008](../adr/0008-namespace-permissions.md), [ADR-0009](../adr/0009-stateless-replicas.md)

MCP authorization spec and claude.ai / Claude Code OAuth behaviour are in [`mcp-auth-and-connectors.md`](./mcp-auth-and-connectors.md) (retrieved 2026-10-06) and are not repeated here. Each part has its own source list; tags `[lab]` mark measurements taken on 2026-10-07, `[unverified]` marks claims without a primary source.

- §1 Microsoft Entra ID
- §2 MCP Streamable HTTP sessions, shutdown and shared state across replicas
- §3 PostgreSQL as source of truth at 1M notes / 5M chunks

---

## §1 Microsoft Entra ID as identity provider for a remote MCP server — fact sheet

Retrieved 2026-10-07. Scope: Entra ID (workforce tenants), v2.0 endpoints. Facts marked **[live probe]** were checked by direct HTTP requests to `login.microsoftonline.com` on 2026-10-07 (commands in the appendix). Facts marked **[community]** come from GitHub issues, not Microsoft documentation. **[unverified]** means I could not confirm it.

### 1. v2 endpoints, discovery, DCR/CIMD, Microsoft MCP guidance

- **Issuer format:** `https://login.microsoftonline.com/{tenantid}/v2.0`. On tenant-specific metadata it is the concrete GUID. On `common`/`organizations` it is the literal template `{tenantid}`, which validators have to substitute with the token's `tid` ([access-tokens](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens); **[live probe]**).
- **openid-configuration** (`https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration`) contains `authorization_endpoint`, `token_endpoint`, `device_authorization_endpoint`, `jwks_uri` (`…/discovery/v2.0/keys`), `end_session_endpoint`, `userinfo_endpoint` (Graph), `response_types_supported`, `response_modes_supported`, `scopes_supported` (`openid profile email offline_access`), `subject_types_supported: ["pairwise"]`, `id_token_signing_alg_values_supported: ["RS256"]`, `token_endpoint_auth_methods_supported` (`client_secret_post`, `private_key_jwt`, `client_secret_basic`, `self_signed_tls_client_auth`), `request_uri_parameter_supported: false`, `mtls_endpoint_aliases`, `tls_client_certificate_bound_access_tokens: true`, plus Microsoft-specific keys **[live probe]**.
- **`code_challenge_methods_supported` is absent**, in both the `common` and the tenant-specific document. `registration_endpoint` and `client_id_metadata_document_supported` are absent too **[live probe]**. PKCE itself works: Entra supports `S256` and `plain`, recommends PKCE for every client type and requires it for SPA ([auth code flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow)).
- **No RFC 8414 document.** HTTP 404 for `/{tenant}/v2.0/.well-known/oauth-authorization-server`, for `/.well-known/oauth-authorization-server/{tenant}/v2.0` (path insertion) and for `/{tenant}/.well-known/oauth-authorization-server`. The OIDC path-insertion variant `/.well-known/openid-configuration/{tenant}/v2.0` also returns 404. Only the OIDC "appended" form returns 200 **[live probe]**.
- **No DCR (RFC 7591).** Microsoft says so itself: "Some providers support Dynamic Client Registration (DCR), but many don't, including Microsoft Entra ID. When DCR isn't available, the client needs to be preconfigured with a client ID" ([App Service MCP auth](https://learn.microsoft.com/en-us/azure/app-service/configure-authentication-mcp)). `POST /{tenant}/oauth2/v2.0/register` returns 404 **[live probe]**.
- **No CIMD.** Azure DevOps Remote MCP Server GA post (2026-08-05): "clients such as Claude Desktop, Claude Code, ChatGPT, Cursor, and similar tools require support for dynamic OAuth client registration or client ID Metadata documents in Microsoft Entra before they can connect … We are working closely with the Microsoft Entra team to enable this capability." Microsoft gives no timeline ([devblogs](https://devblogs.microsoft.com/devops/azure-devops-remote-mcp-server-ga/)). The Entra what's-new page does not mention CIMD or DCR ([whats-new source](https://github.com/MicrosoftDocs/entra-docs/blob/main/docs/fundamentals/whats-new.md)).
- **Microsoft guidance for MCP with Entra:**
  - *App Service / Functions built-in auth* serves PRM (preview, `WEBSITE_AUTH_PRM_DEFAULT_WITH_SCOPES`, default scope `api://<client-id>/user_impersonation`). It recommends pre-authorizing known clients and requires a pre-configured client ID ([App Service MCP auth](https://learn.microsoft.com/en-us/azure/app-service/configure-authentication-mcp)).
  - *Azure API Management* validates Entra tokens inbound with `validate-azure-ad-token` and links PRM samples ([APIM secure MCP](https://learn.microsoft.com/en-us/azure/api-management/secure-mcp-servers), page date 2026-09-11).
  - The Azure-Samples "Secure Remote MCP Servers using APIM (Experimental)" builds an **AS facade in APIM policies**: `/authorize`, `/.well-known/oauth-authorization-server`, `POST /register` (DCR) and `/token`. It exchanges the Entra code itself and hands an encrypted session token to the MCP client. Entra tokens are never exposed to the client ([sample README](https://github.com/Azure-Samples/remote-mcp-apim-functions-python), last push 2026-09-23). Microsoft's May 2025 blog describes this pattern for Claude ([developer.microsoft.com](https://developer.microsoft.com/blog/claude-ready-secure-mcp-apim/)).
  - Clients Microsoft lists as working with Entra-protected remote MCP: VS Code + GitHub Copilot, Visual Studio, GitHub Copilot CLI/app, Foundry, Copilot Studio ([devblogs](https://devblogs.microsoft.com/devops/azure-devops-remote-mcp-server-ga/)).

### 2. RFC 8707 `resource` on v2 endpoints

- **Mismatch is rejected.** `/authorize` with `scope=https://graph.microsoft.com/User.Read&resource=https://mcp.example.com/mcp` redirects with `error=invalid_target` and `AADSTS9010010: The resource parameter provided in the request doesn't match with the requested scopes` **[live probe]**.
- **Match is accepted.** With `resource=https://graph.microsoft.com` (with or without a trailing slash) and the same Graph scope, the normal sign-in page appears (no error) **[live probe]**. So `resource` is accepted only when it equals the identifier URI of the API that owns the scopes.
- Community reports say the enforcement became strict around March 2026; before that `resource` was silently ignored **[community]**: [claude-code#73460](https://github.com/anthropics/claude-code/issues/73460), [python-sdk#2578](https://github.com/modelcontextprotocol/python-sdk/issues/2578).
- **Known MCP-client failures against Entra directly:**
  - Claude Code adds a trailing slash to `resource` and breaks even matching configurations, e.g. Microsoft's Business Central MCP, whose identifier URI is `https://mcp.businesscentral.dynamics.com` ([claude-code#52871](https://github.com/anthropics/claude-code/issues/52871), open).
  - The server URL ≠ `api://<appid>` mismatch: [#73460](https://github.com/anthropics/claude-code/issues/73460) (closed as stale) and [#89438](https://github.com/anthropics/claude-code/issues/89438) (open, 2026-08-25). These also report `AADSTS650053` on reconnect in Claude Code and Claude Desktop (scope resolved against Graph).
  - VS Code fails the same way ([vscode#321249](https://github.com/microsoft/vscode/issues/321249), open).
  - The MCP Python SDK sends `resource` on refresh, which Entra rejects. Sessions die after about 1 h ([python-sdk#2578](https://github.com/modelcontextprotocol/python-sdk/issues/2578), open).
  - The same class of failure is reported for LibreChat, FastMCP, IBM context-forge and Tracecat.
- **Identifier URI = MCP URL:** possible in principle. Allowed secure patterns include `https://<verifiedCustomDomain>/<string>`, `https://<string>.<verifiedCustomDomain>` and `https://<string>.<verifiedCustomDomain>/<string>`. The value must not end in `/` and must be unique in the tenant. `https://` always requires a verified (or initial `*.onmicrosoft.com`) domain, even when the policy is disabled. v2-token apps (`requestedAccessTokenVersion=2`) are exempt from the stricter default policy ([identifier-uri-restrictions](https://learn.microsoft.com/en-us/entra/identity-platform/identifier-uri-restrictions)).
  - Consequence: the MCP host must sit under a domain verified in the operator's Entra tenant. Cloud hostnames such as `*.azurewebsites.net` or `*.amazonaws.com` cannot be used **[community, consistent with docs]**.
  - **[unverified]** That a path-bearing identifier URI (`https://mcp.example.com/mcp`) plus scope `https://mcp.example.com/mcp/<scope>` makes `resource=https://mcp.example.com/mcp` pass. Inferred from the Graph probe and the Business Central case; not tested on a tenant.
- Even when the identifier URI is https, **v2 `aud` is the API's client ID GUID**, not the URI. "In v2.0 tokens, this value is always the client ID of the API" ([claims reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference)).

### 3. Access token v2 claims and validation

Source: [claims reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference) and [access-tokens](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens).

- `iss`: `https://login.microsoftonline.com/{tid}/v2.0`. `ver`: `"2.0"`.
- `aud`: the API client ID (GUID) in v2. In v1 it is the GUID or the resource URI as requested.
- `tid`: tenant GUID. `oid`: immutable, the same across apps within a tenant (the Graph `id`).
- `sub`: **pairwise per application ID**, immutable.
- `scp`: space-separated delegated scopes (user tokens only). `roles`: app roles assigned to the user (or to the app for client-credentials tokens).
- `groups`: object IDs, subject to overage (see §5). `wids`: directory role template IDs.
- `azp` (v2) / `appid` (v1): client app ID. `azpacr`: 0 = public, 1 = secret, 2 = certificate.
- `idtyp`: optional claim, app vs user token. `xms_cc`: client capabilities (CAE `cp1`).
- `preferred_username` / `name`: mutable, must not be used for authorization. `uti`: token ID.
- Microsoft states "Applications should not take hard dependency on claims being present".
- `requestedAccessTokenVersion` in the manifest: `null`/`1` → v1 tokens, `2` → v2. The resource decides the version regardless of which endpoint the client uses.
- **Validation guidance:** web APIs "must only accept tokens containing one of their AppId URIs as the `aud` claim" (in practice the GUID for v2). Validate signature and issuer against the OIDC metadata.
  - Multi-tenant: substitute `{tenantid}` with `tid`, check that `tid` is a GUID, and check the key's own `issuer` property from the JWKS.
  - Use `kid` (v2 has no `x5t`). Refresh the keys roughly every 24 h; keys rotate periodically and code must handle that automatically.
  - Apps with custom signing keys (claims mapping) must append `?appid=<client-id>` to the metadata/JWKS URL. `/discovery/v2.0/keys?appid=…` returns 200 **[live probe]**.

### 4. App roles

Source: [howto-add-app-roles](https://learn.microsoft.com/en-us/entra/identity-platform/howto-add-app-roles-in-apps), page date 2026-09-25.

- **Definition:** `appRoles` on the app registration. Each role has `allowedMemberTypes` (User/Group and/or Application), `value` (the claim value, no spaces), `displayName`, `description` and `isEnabled`. There is a limit of 700 role and scope definitions per app.
- **Assignment:** roles are assigned to users or groups under Enterprise apps → Users and groups, or via Graph `appRoleAssignedTo`. Assigned roles appear in the `roles` claim of the access token issued for the API.
  - Disabling a role does not remove it from tokens; existing assignments still emit it.
  - Groups assigned to a role propagate it to members. Service principals in such a group do not get the claim.
- **Group-based assignment requires Entra ID P1/P2, and nested groups are not supported** ([assign users/groups](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/assign-user-or-group-access-portal), page date 2026-04-01).
- **"Assignment required"** (`appRoleAssignmentRequired` on the service principal): "When user assignment is required, only those users you assign to the application (either through direct user assignment or based on group membership) are able to sign in". It applies to OAuth2/OIDC apps. It disables user consent, so admin consent is needed. If it is off, unassigned users can still sign in ([access management](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/what-is-access-management)).
- The Terraform docs describe the same setting as "requires an app role assignment … before Azure AD will issue a user or access token" ([azuread_service_principal](https://registry.terraform.io/providers/hashicorp/azuread/latest/docs/resources/service_principal)).

### 5. Group claims and overage

- **Limits:** 200 groups for JWT, 150 for SAML, and 5 for the implicit flow (which uses `hasgroups`). Nested groups count toward the limit. Above the limit the `groups` claim is omitted entirely.
- **Overage signal:** a JWT gets `"_claim_names":{"groups":"src1"}` and `"_claim_sources":{"src1":{"endpoint":…}}`. The endpoint URL is legacy Azure AD Graph; Microsoft says to build a Microsoft Graph URL instead. `hasgroups: true` points to `/users/{id}/getMemberObjects` ([claims reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference); [group claims](https://learn.microsoft.com/en-us/entra/identity/hybrid/connect/how-to-connect-fed-group-claims)).
- **`groupMembershipClaims` values:** `None`, `SecurityGroup` (security groups plus directory roles), `DirectoryRole` (emitted as `wids`), `ApplicationGroup` and `All`.
  - `ApplicationGroup` ("Groups assigned to the application") emits only groups explicitly assigned to the app. Nested groups are not included; the user must be a direct member.
  - Microsoft recommends it for large organizations because of the limit. Group assignment itself needs P1/P2.
  - Group filtering only works for users in ≤ 1,000 groups.
  - Microsoft recommends app roles over groups for new apps when nesting is not needed.
- **Graph fallbacks:**
  - `POST /users/{id}/getMemberObjects` (transitive). Application permission: least privileged `User.ReadBasic.All` + `GroupMember.Read.All`; `Directory.Read.All` covers it. ([getMemberObjects](https://learn.microsoft.com/en-us/graph/api/directoryobject-getmemberobjects?view=graph-rest-1.0))
  - `POST /users/{id}/checkMemberGroups` (transitive, up to 20 group IDs). Same permissions, plus `Member.Read.Hidden` for hidden-membership groups. ([checkMemberGroups](https://learn.microsoft.com/en-us/graph/api/directoryobject-checkmembergroups?view=graph-rest-1.0))
  - `getMemberGroups` exists alongside these; its permission page was not retrieved (likely analogous) **[unverified]**.
  - `GET /users/{id}/transitiveMemberOf`: application least privileged `User.Read.All`. Page size 100 by default, 999 max. ([transitiveMemberOf](https://learn.microsoft.com/en-us/graph/api/user-list-transitivememberof?view=graph-rest-1.0))
  - For the signed-in user (`/me/...`), delegated `User.Read` is enough. Application permissions need admin consent.
- **Graph throttling (identity and access):** limits are per app+tenant per 10 s: 3,500 ResourceUnits (S, < 50 users), 5,000 (M, 50–500) and 8,000 (L, > 500). There is also 150,000 RU per 20 s per app across tenants. Most requests cost 1 RU; `$select` −1, `isMemberOf` 4, `groups/{id}/transitiveMembers` 5. **There is no `Retry-After` on 429 for identity limits**, so clients must back off exponentially with jitter ([throttling limits](https://learn.microsoft.com/en-us/graph/throttling-limits)).

### 6. Token lifetimes

- **Access token:** a random 60–90 min (75 average). Tenants without Conditional Access give 2 h to Teams and M365-type clients ([access-tokens](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens)).
- **CAE long-lived tokens:** 20–28 h, only "when both the client and the resource support CAE" ([CTL](https://learn.microsoft.com/en-us/entra/identity-platform/configurable-token-lifetimes)).
  - "The initial implementation of continuous access evaluation focuses on Exchange, Teams, and SharePoint Online" (plus Graph for CA policy sync).
  - Critical events: user deleted or disabled, password change, MFA enabled, refresh tokens revoked, high user risk. Latency is up to 15 min ([CAE concept](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-continuous-access-evaluation), page date 2026-04-08).
  - **No documented mechanism lets a third-party/custom resource API subscribe to CAE events.** Docs say "both your app and the resource API it's accessing must be CAE-enabled" but give no way to make a custom API one ([app-resilience CAE](https://learn.microsoft.com/en-us/entra/identity-platform/app-resilience-continuous-access-evaluation)). Treat custom APIs as non-CAE: they get plain 60–90 min tokens **[inference; not stated verbatim]**.
- **Refresh token:** 24 h for `spa` redirect URIs (non-extendable chain) and 90 days for everything else (sliding: Max Inactive Time 90 days, max age until revoked). Refresh and session token lifetimes are **not configurable** since 2021-01-30; Microsoft points to CA sign-in frequency instead ([refresh tokens](https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens); [CTL](https://learn.microsoft.com/en-us/entra/identity-platform/configurable-token-lifetimes)).
- **Configurable token lifetime policies:** still supported for access, ID and SAML tokens. Range 10 min to 23:59:59. Graph/PowerShell only, no portal UI. An org-level policy beats an app-level one. Not honored for CAE sessions ([CTL](https://learn.microsoft.com/en-us/entra/identity-platform/configurable-token-lifetimes), page date 2026-04-08).

### 7. Conditional Access

- CA targets **resources**, and any Entra-registered app (gallery or custom) can be targeted. "Conditional Access applies to resources not clients, except when the client is a confidential client requesting an ID token." Public clients cannot be targeted; policies apply to the resources they request ([CA target resources](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-conditional-access-cloud-apps)).
- **Implications:**
  - (a) **Direct model:** CA is evaluated when the MCP client gets a token for the MCP API's resource. Policies target the API app.
  - (b) **Facade as a confidential OIDC client:** CA is evaluated when the facade signs the user in (it requests an ID token, plus any resource it asks for). It is not re-evaluated on each MCP call. Re-evaluation happens only when the facade goes back to Entra (refresh or re-login). Session length is governed by the facade's own token and refresh lifetime, not by Entra's.
  - Since the March 2026 rollout, "All resources" policies with exclusions also enforce low-privilege scopes (`openid`, `profile`, `User.Read`, `GroupMember.Read.All`…). A facade requesting only those scopes is still in scope of baseline policies (same source).
- Authentication context (`acrs`, c1–c99) is available for step-up in custom apps (same source).

### 8. Deprovisioning

- **Plain JWT validation:** a disabled or deleted user keeps a valid access token until `exp` (≤ 90 min, or longer with CTL). Refresh tokens are revoked on "Admin revokes all refresh tokens" for every client class. For confidential clients a password change by the user does **not** revoke refresh tokens ([refresh tokens](https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens)). Without CAE nothing pushes revocation to a custom API (§6).
- **`POST /users/{id}/revokeSignInSessions`:** invalidates refresh tokens and browser session cookies by resetting `signInSessionsValidFromDateTime`. There is "a small delay of a few minutes". Access tokens already issued are not mentioned and stay valid until expiry. Application permission: `User.RevokeSessions.All`. Does not work for external (B2B) users ([revokeSignInSessions](https://learn.microsoft.com/en-us/graph/api/user-revokesigninsessions?view=graph-rest-1.0)).
- **Delta query `GET /users/delta`:** application permission `User.Read.All`. `$select` limits tracking to the chosen properties (e.g. `accountEnabled`). `$deltatoken=latest` gives "sync from now".
  - Deleted users appear as `@removed` with `reason: changed` (soft-deleted, restorable) or `deleted` (hard-deleted).
  - Delta tokens for directory objects last **7 days**. `410 Gone` means a full resync is needed. Replication delays and replays occur ([user delta](https://learn.microsoft.com/en-us/graph/api/user-delta?view=graph-rest-1.0); [delta overview](https://learn.microsoft.com/en-us/graph/delta-query-overview)).
- **Change notifications:** `/users` and `/users/{id}` are supported. Maximum subscription lifetime is 41,760 min (< 29 days), so subscriptions need renewal. Quotas: 100 per app+tenant, 1,000 per tenant. **Latency for `user` is listed as "Unknown"**. Delivery is by webhook (public HTTPS endpoint), Event Hubs or Event Grid. Microsoft recommends combining notifications with delta query ([change notifications](https://learn.microsoft.com/en-us/graph/change-notifications-overview)).
- **SCIM provisioning (Entra → custom app):**
  - Uses the non-gallery app feature. The app needs SCIM 2.0 `/Users` (POST, GET by id, `filter=userName eq …`, PATCH, optional DELETE) and optionally `/Groups`.
  - Disable is signalled as PATCH `active=false`. Hard-delete follows 30 days after soft-delete, or on permanent deletion.
  - Sync cycles run "approximately every 40 minutes". In quarantine they slow to daily.
  - Auth options for the endpoint: a long-lived bearer token (still supported for non-gallery apps), an Entra-issued bearer token when the Secret Token field is blank, or OAuth2 client credentials. The auth code grant is retired.
  - Scope is assigned users and groups. Group scoping requires P1/P2, nested groups are not supported, and the user must be active in Entra before provisioning.
  - Sources: [SCIM tutorial](https://learn.microsoft.com/en-us/entra/identity/app-provisioning/use-scim-to-provision-users-and-groups) (page date 2026-09-15); [how provisioning works](https://learn.microsoft.com/en-us/entra/identity/app-provisioning/how-provisioning-works).

### 9. Redirect URIs and client types

Source: [reply-url](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url).

- `https` is required except for localhost. `http://localhost` is valid.
- **The port is ignored when matching localhost redirect URIs** (RFC 8252 §7.3/8.3). `http://localhost/callback` matches `http://localhost:<any>/callback`. The path still has to match exactly (case-sensitive). Do not register several localhost URIs that differ only by port, because Entra picks one arbitrarily. IPv6 `[::1]` is not supported.
- `http://127.0.0.1` can only be added through the manifest (`replyUrlsWithType`), not the portal text box. Microsoft nevertheless recommends `127.0.0.1` over `localhost`.
- **Platform types:**
  - Desktop/CLI with system browser: "Mobile and desktop applications" (public client, no secret).
  - Server-side: "Web" (confidential; `client_secret`/certificate required at the token endpoint, PKCE optional but recommended).
  - SPA: requires PKCE, needs a CORS `Origin` header, and gets 24 h refresh tokens. A `spa` redirect cannot be used for non-SPA flows, and Entra refuses client credentials when an `Origin` header is present ([auth code flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow)).
- `https://claude.ai/api/mcp/auth_callback` fits the Web platform (claude.ai redeems the code server-side with an optional secret). Registering it in a customer tenant is an ordinary https redirect; there is no domain-verification requirement for redirect URIs (none is stated in [reply-url](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url)).
- Limits: 256 redirect URIs (org-only apps) or 100 (apps that include MSA); 256 characters each. Query parameters are allowed only for org-only apps. Wildcards work only via the manifest, for org-only apps, and are discouraged.
- **[unverified]** Whether claude.ai in "own OAuth client" mode without a secret works against a Web-platform registration. A Web redirect requires a secret at the token endpoint, so without a secret the client would have to be registered as a public "Mobile and desktop" redirect; https URIs are allowed there.

### 10. Terraform `azuread` provider

- **Current version: 3.10.0** (2026-09-24), major line 3.x. Releases: 3.8.0 (2026-02), 3.9.0 (2026-06). **License MPL-2.0**, repo active ([registry](https://registry.terraform.io/providers/hashicorp/azuread/latest); [GitHub](https://github.com/hashicorp/terraform-provider-azuread)).
- **OpenTofu registry mirrors it** (`registry.opentofu.org/v1/providers/hashicorp/azuread/versions` lists 105 versions up to 3.10.0) **[live probe]**.
- **`azuread_application`:**
  - `identifier_uris`, `sign_in_audience`, `group_membership_claims` (`None|SecurityGroup|DirectoryRole|ApplicationGroup|All`).
  - `app_role` blocks.
  - `api { requested_access_token_version, oauth2_permission_scope {…}, known_client_applications, mapped_claims_enabled }`.
  - `optional_claims { access_token / id_token / saml2_token }`.
  - `web { redirect_uris, implicit_grant, … }`, `public_client { redirect_uris }`, `single_page_application { redirect_uris }`, `fallback_public_client_enabled`, `required_resource_access`.
- **Split resources** for modular management: `azuread_application_identifier_uri`, `azuread_application_redirect_uris`, `azuread_application_app_role`, `azuread_application_permission_scope`, `azuread_application_optional_claims`, `azuread_application_pre_authorized`.
- **`azuread_service_principal`:** `app_role_assignment_required`. **`azuread_app_role_assignment`** assigns roles to users, groups or SPs (needs `AppRoleAssignment.ReadWrite.All` + `Application.Read.All`/`Directory.Read.All`).
  - Docs: [application](https://registry.terraform.io/providers/hashicorp/azuread/latest/docs/resources/application), [service_principal](https://registry.terraform.io/providers/hashicorp/azuread/latest/docs/resources/service_principal), [app_role_assignment](https://registry.terraform.io/providers/hashicorp/azuread/latest/docs/resources/app_role_assignment).
- **Doc quirk:** the v3.10.0 docs claim `public_client.redirect_uris` must be `https`/`ms-appx-web` and `web.redirect_uris` must be "http URL or URN". The source validator (`internal/services/applications/application_resource.go`) allows **all schemes** for `public_client` (`IsRedirectUriFunc(true, true)`) and `http/https/ms-appx-web/brk-multihub` for `web`. `http://localhost` is therefore accepted on `public_client` in code.

### Sources (all retrieved 2026-10-07)

- https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration and tenant-specific variant (live)
- https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens
- https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference
- https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow
- https://learn.microsoft.com/en-us/entra/identity-platform/reply-url
- https://learn.microsoft.com/en-us/entra/identity-platform/identifier-uri-restrictions
- https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens
- https://learn.microsoft.com/en-us/entra/identity-platform/configurable-token-lifetimes
- https://learn.microsoft.com/en-us/entra/identity-platform/app-resilience-continuous-access-evaluation
- https://learn.microsoft.com/en-us/entra/identity-platform/howto-add-app-roles-in-apps
- https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-continuous-access-evaluation
- https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-conditional-access-cloud-apps
- https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/assign-user-or-group-access-portal
- https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/what-is-access-management
- https://learn.microsoft.com/en-us/entra/identity/hybrid/connect/how-to-connect-fed-group-claims
- https://learn.microsoft.com/en-us/entra/identity/app-provisioning/use-scim-to-provision-users-and-groups
- https://learn.microsoft.com/en-us/entra/identity/app-provisioning/how-provisioning-works
- https://learn.microsoft.com/en-us/graph/api/directoryobject-getmemberobjects?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/api/directoryobject-checkmembergroups?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/api/user-list-transitivememberof?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/throttling-limits
- https://learn.microsoft.com/en-us/graph/api/user-revokesigninsessions?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/api/user-delta?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/delta-query-overview
- https://learn.microsoft.com/en-us/graph/change-notifications-overview
- https://learn.microsoft.com/en-us/azure/app-service/configure-authentication-mcp
- https://learn.microsoft.com/en-us/azure/api-management/secure-mcp-servers
- https://github.com/Azure-Samples/remote-mcp-apim-functions-python
- https://developer.microsoft.com/blog/claude-ready-secure-mcp-apim/
- https://devblogs.microsoft.com/devops/azure-devops-remote-mcp-server-ga/
- https://github.com/MicrosoftDocs/entra-docs/blob/main/docs/fundamentals/whats-new.md
- https://github.com/anthropics/claude-code/issues/52871
- https://github.com/anthropics/claude-code/issues/73460
- https://github.com/anthropics/claude-code/issues/89438
- https://github.com/microsoft/vscode/issues/321249
- https://github.com/modelcontextprotocol/python-sdk/issues/2578
- https://registry.terraform.io/providers/hashicorp/azuread/latest (+ resource docs listed in §10)
- https://registry.opentofu.org/v1/providers/hashicorp/azuread/versions
- https://github.com/hashicorp/terraform-provider-azuread (tag v3.10.0)

### Appendix — live probes

```
curl https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration          # 200, no code_challenge_methods_supported
curl https://login.microsoftonline.com/{tenant}/v2.0/.well-known/oauth-authorization-server    # 404
curl https://login.microsoftonline.com/.well-known/oauth-authorization-server/{tenant}/v2.0    # 404
curl -X POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/register                   # 404
GET /organizations/oauth2/v2.0/authorize?client_id=<Azure CLI public client>&scope=https://graph.microsoft.com/User.Read
    &resource=https://mcp.example.com/mcp&code_challenge_method=S256…  -> 302 error=invalid_target AADSTS9010010
    &resource=https://graph.microsoft.com                               -> 200 sign-in page (no error)
```

### Decision impact

Options: (a) clients talk to Entra directly with a pre-registered client · (b) AS facade in our server (DCR/CIMD/PKCE to MCP clients, login delegated to Entra as an OIDC client) · (c) upstream gateway such as APIM.

1. **Entra's metadata lacks `code_challenge_methods_supported` and there is no RFC 8414 document** (live-verified). claude.ai refuses an AS without that field and uses only the first `authorization_servers` entry, so **(a) cannot work for claude.ai** however Entra is configured. This is the most constraining fact.
2. **Entra has no DCR and no CIMD**, with no timeline (Microsoft: App Service docs, Azure DevOps GA post 2026-08-05). Under (a) every client needs a pre-registered client ID per tenant. Claude Code works only with `--client-id`; zero-config onboarding is impossible. (b) or (c) restore DCR/CIMD for clients.
3. **RFC 8707 `resource` must byte-equal the identifier URI that owns the requested scope, otherwise AADSTS9010010** (live-verified). Claude Code, VS Code and the MCP Python SDK all have open bugs here: trailing slash, refresh path, reconnect via Graph. (a) is fragile even for Claude Code. (b) avoids the issue entirely because our AS honours `resource` and talks plain v2 scope requests to Entra.
4. **Matching `resource` needs an https identifier URI on a domain verified in the operator's tenant.** That is an operator burden and impossible on cloud default hostnames. And v2 `aud` is still the client ID GUID, not the URL. Audience binding to the MCP URL is only fully under our control in (b).
5. **CAE does not reach custom APIs.** With plain JWT validation, a disabled user keeps access for up to 60–90 min (longer with CTL), and `revokeSignInSessions` kills only refresh tokens, within minutes. Under (b) revocation latency is set by our own token lifetime plus how often we re-check Entra (refresh, delta query or webhooks; SCIM is about 40 min). Under (a) it is Entra's lifetime.
6. **Conditional Access is evaluated at token issuance.** Under (b) that is the facade's confidential-client sign-in (ID token), and later only on the facade's refresh or re-login. Our facade's session and refresh lifetime then determines how often CA and sign-in frequency are re-applied. That is a policy decision the facade must expose (short facade refresh lifetime, or a forced upstream refresh).
7. **Authorization data:** app roles in `roles` (group assignment needs P1/P2, no nesting) avoid group overage. Using `groups` means handling the 200-group overage with Graph calls. Those calls need admin-consented `GroupMember.Read.All` + `User.ReadBasic.All` (or `User.Read.All`), and Graph sends no `Retry-After` on identity throttling. Equal cost in (a)/(b)/(c); (b) can resolve it once at login and cache it.
8. **Redirects:** Entra ignores the port for localhost (Claude Code's random port works with one `http://localhost/callback` registration, public "Mobile and desktop" platform). `https://claude.ai/api/mcp/auth_callback` is a normal Web redirect. Under (b) only the facade's own callback is registered in Entra, a single static confidential client per tenant.
9. **(c) APIM:** Microsoft's own Claude-ready sample implements exactly (b) inside APIM policies (`/register`, `/authorize`, `/token`, PRM, encrypted session tokens; marked "Experimental"). APIM does not supply DCR/CIMD itself. (c) is therefore (b) moved into a proprietary, Azure-only, non-OSS gateway, which conflicts with this project's OSS-first, container, generic-deployment guardrails.
10. **Terraform `azuread` 3.10.0 (MPL-2.0, mirrored on the OpenTofu registry)** covers every object needed for (a) or (b): app, roles, scopes, identifier URI, redirect URIs, group claims, SP with `app_role_assignment_required`, role assignments. Operator setup is fully scriptable in either option.

### Unverifiable or not verified

- Whether a **path-bearing https identifier URI** (e.g. `https://mcp.example.com/mcp`) together with scope `https://mcp.example.com/mcp/<scope>` makes `resource=https://mcp.example.com/mcp` pass. Inferred from the Graph probe and the Business Central case, not tested on a tenant with a verified domain.
- Whether Entra rejects a **matching** `resource` on `refresh_token` grants. The python-sdk#2578 report may involve only the trailing-slash mismatch. Not probed; probing needs a real refresh token.
- That strict `resource` enforcement started around **March 2026**. Community reports only; I found no Microsoft announcement.
- **CAE for custom APIs.** No Microsoft doc says verbatim "not supported". The conclusion rests on CAE docs listing only Microsoft services and the absence of any registration mechanism.
- Exact permission table for **`getMemberGroups`** (not fetched; assumed analogous to `getMemberObjects`).
- **Change-notification latency for `user`**: Microsoft lists it as "Unknown".
- Whether claude.ai **"own OAuth client" without a secret** works against Entra; that depends on how claude.ai authenticates at the token endpoint.
- Any **Microsoft roadmap date for CIMD/DCR** in Entra: none published. A third-party blog (bighatgroup.com) claims related details, including a CVE, that I could not confirm and did not use.

---

## §2 Horizontal scaling of the MCP server: sessions, shutdown, shared state

Retrieved: 2026-10-07 · SDK under test: `mcp` 2.3.0 / `mcp_types` 2.3.0 (installed in `.venv`), `uvicorn` 0.54.0, `starlette` 1.7.0, `sse_starlette` 3.5.0.

Method: read the installed SDK source; ran a minimal `MCPServer` with `stateless_http=True, json_response=True` under uvicorn and probed it with httpx/curl ([E1]); SIGTERM test with an open GET stream ([E2]); pgbench run against `pgvector/pgvector:pg16` in podman ([E3]). Does not repeat `mcp-sdks.md` / `mcp-auth-and-connectors.md` (spec transport rules, Claude auth behaviour are there).

### 1. What `stateless_http=True` does in `mcp` 2.3.0

**Era routing comes first.** `StreamableHTTPSessionManager._handle_request` reads `MCP-Protocol-Version`. If present and not in `HANDSHAKE_PROTOCOL_VERSIONS`, the request goes to `handle_modern_request` and returns. `stateless_http` is only read after that, so it affects **legacy requests only** ([P1] `streamable_http_manager.py:192-204`; same statement in SDK docs [D1] "Workers, and who has to be sticky", [D2] "The one knob").

#### Legacy clients (2025-11-25 / 2025-06-18 / no header) with `stateless_http=True`

| Aspect | Behaviour | Source |
|---|---|---|
| `Mcp-Session-Id` | Never issued. The transport is built with `mcp_session_id=None`. A bogus client-sent id is ignored (request served, 200) | [P1] :212-217; [P2] `_validate_session` :901-905; [E1] |
| `initialize` | Answered inline per POST (`inline_methods={"initialize"}`). The connection is "born-ready" from the header (default `DEFAULT_NEGOTIATED_VERSION`), so `tools/call` **without a prior `initialize`** works | [P1] :224-243; [E1] `call-no-init 200` |
| GET (standalone SSE) | **Not 405.** Returns `200 text/event-stream` and holds an empty stream open until the client disconnects or the server shuts down. Nothing is ever written to it (no producer exists in a per-request transport). The SDK docs say stateless has "no standalone stream", which is true in effect (no events) but not on the wire | [P2] `_handle_get_request` :728-823; [E1] `get: ReadTimeout (stream held open)`; [D2] |
| DELETE | `405` with JSON-RPC body "Session termination not supported" | [P2] :825-835; [E1] |
| Server→client requests (elicitation, sampling, roots) | Raise `NoBackChannelError` (`TransportContext(can_send_request=False)`) | [P1] :228-233; [D2] "It costs both server-to-client channels" |
| Progress / log notifications | With `json_response=True` (this repo's default, `config.py:263,320`) request-related notifications are **dropped** before queueing. With SSE responses they would go out on that POST's stream | [P2] :1072-1083, :178-183 |
| Resumability / EventStore | `event_store=None` is hard-coded in the stateless path; `Last-Event-ID` is meaningless | [P1] :215 |
| Shared state needed? | None. Each POST builds and tears down its own transport (`terminate()` in `finally`). Any replica can serve any request | [P1] :251-259; [D1] table row "with `stateless_http=True`: Nothing" |

#### Modern clients (2026-07-28)

- One self-contained POST per request; `Mcp-Session-Id` never set; GET/DELETE → `405 Allow: POST`. Verified: `get-modern 405 POST`, `modern call 200 None` (no session header) ([P3] `_streamable_http_modern.py:375-403`; [E1]).
- Server→client requests: `send_raw_request` always raises `NoBackChannelError` (MRTR instead) ([P3] :101-107).
- Progress: sent only on the SSE path; with `json_response=True` the request is served via `serve_one` → single JSON body, so progress is effectively not delivered ([P3] :109-126, :465-476).
- `subscriptions/listen` is a long-lived SSE response pinned to one replica; cross-replica change notifications need a custom `SubscriptionBus` (SDK ships only in-process) ([P3] :429-433; [D1] "Change notifications across replicas").
- MRTR `requestState` is sealed with a per-process random key by default; multi-replica needs shared `RequestStateSecurity(keys=[...])` and identical server name ([D1] "`requestState` across workers"). **Not used here**: no `Resolve(...)`, `InputRequiredResult` or `request_state` in `src/memory_manager` (grep).

#### Features lost in stateless mode, and whether this server needs them

Lost on the legacy path: server-initiated requests (elicitation, sampling, roots), standalone notification stream (`list_changed`, resource updates, unsolicited logs), resumability, session-scoped state. Also progress, but only because `json_response=True`.

This server needs none of them. `mcp/server.py` registers 7 tools and 1 prompt (`memory_guide`); grep finds no `elicit`, `report_progress`, `create_message`, `ctx.log/info`, `list_roots`, `notify_*` in `src/memory_manager/mcp/`. Capabilities advertise `listChanged:false` ([E1] initialize result). Claude does not support sampling or resource subscriptions anyway ([C1] "Claude doesn't yet support").

### 2. If stateful were chosen

- The legacy session record is "a plain in-process `dict`. There is no distributed session store and no way to plug one in" ([D2] "The routing is free. The session is not."; code: `_server_instances`, `_session_owners` dicts, [P1] :118-121).
- A request with a foreign `Mcp-Session-Id` → `404 Session not found`, so **sticky sessions are mandatory** for legacy clients across replicas ([P1] :301-306; [D1] table; [D2]).
- `EventStore` is only an ABC (`store_event`, `replay_events_after`) ([P2] :116-145). The SDK ships **no** implementation in the package. The repo only has an in-memory example (`examples/stories/sse_polling/event_store.py`) and no Redis EventStore. The docs say plainly: "No shipped `EventStore`", and "`event_store=` looks like the fix and is not": it replays events within the same session, it does not make a session reachable from another process ([D1], [D2], [G1] code search 2026-10-07).
- The only Redis code in the SDK docs is a `SubscriptionBus` sketch for modern `subscriptions/listen` ([D3]).
- Stateful also brings `session_idle_timeout` (1800 s) and `max_sessions` (10 000), both per process. An open GET keeps a session alive indefinitely ([P1] :38-42; [D2] "Session lifetime and limits").
- Conclusion: stateful + N replicas = sticky routing (cookie/header affinity on `Mcp-Session-Id` at the ingress) plus loss of every session on pod restart. Nothing in the SDK mitigates this.

### 3. claude.ai and Claude Code against a stateless legacy-shape server

- claude.com connector docs ([C1], [C2]) contain **no** statement about sessions, stateless servers, the GET stream or sticky routing. Unverifiable from docs; the facts below come from issue trackers.
- [C2] diagnostic checklist: `curl -i …/mcp` "A `401` or `405` is fine".
- **Claude Code**: issue anthropics/claude-code#39790 (2026-03-27): from v2.1.84, servers that answer GET `/mcp` with **405** were marked failed (Spring AI stateless, FastMCP). It was closed for inactivity on 2026-05-11 with no fix confirmed ([I1]). Current status: **unverified**.
  - This SDK's stateless legacy path answers GET with `200 text/event-stream` (empty, held open), not 405, so it is **not exposed to that bug** ([E1]).
  - A modern-header GET does get 405, but a modern client is not expected to send GET.
- **claude.ai hosted**: anthropics/claude-code#78193 (closed 2026-08-24) and anthropics/claude-ai-mcp#636 (**open**, last comment 2026-10-04) report that Claude's own proxy (`claude.ai/v1/toolbox/shttp/mcp/<id>`) answers the client's GET with 405 and the origin "never received a single GET".
  - The symptom is the "Client server capabilities not available" toast. It mainly breaks MCP Apps rendering, and tool calls still reach the origin with 200 ([I2], [I3]).
  - So the origin's GET behaviour is apparently irrelevant for claude.ai web. The bug is client-side, and this server uses no MCP Apps.
  - One reporter on #636 runs a "stateless Streamable HTTP" server answering both eras; their problem is MCP Apps only ([I3], comment 2026-10-04).
- Project fact: v0.1.x of this server (stateless, `json_response=True`) is deployed and documented for claude.ai and Claude Code (commit `50bdaf0`, auto-memory "v0.1.0 live"). Whether claude.ai connected successfully is not verifiable from this environment.
- Claude only speaks the 2025 authorization specs ([C1]), so traffic from Claude takes the **legacy** path, and `stateless_http=True` is what makes it replica-agnostic.

### 4. Graceful shutdown (uvicorn + SDK session manager)

uvicorn 0.54.0 `Server.shutdown` ([U1] `uvicorn/server.py:281-311`):
1. Close the listening sockets, so no new connections.
2. `connection.shutdown()` closes idle keep-alive connections and sets `keep_alive=False` on busy ones ([U2] `protocols/http/h11_impl.py:336-345`).
3. Wait for all connections and tasks, bounded by `timeout_graceful_shutdown` (default `None` = wait forever, [U3] `config.py:236`). After the timeout the remaining tasks are cancelled.
4. Only then send `lifespan.shutdown`. Here that runs `StreamableHTTPSessionManager.run()`'s `finally` (cancels its task group, [P1] :163-173), then `create_app`'s lifespan `finally` (`http.py:254-264`), then `open_services`' cleanup (poll task, `queue.stop()` cancels the consumer, pool close; `app.py:134-142`).

- `cli.py:847-864` sets no `timeout_graceful_shutdown`, so it waits unbounded and only Kubernetes' SIGKILL bounds it. If SIGKILL fires first, lifespan shutdown never runs.
- **SSE streams do not block shutdown**: `sse_starlette` 3.5.0 detects uvicorn's `should_exit` (signal-handler introspection) and ends every `EventSourceResponse` ("automatic graceful drain", [S1] `sse_starlette/sse.py:110-160,186-213`).
  - Verified: with an open legacy GET stream and no grace timeout, SIGTERM led to a clean exit within about 1 s ("Application shutdown complete") ([E2]).
  - Flip side: in SSE response mode an **in-flight legacy tool call answered via `EventSourceResponse` would also be cut** on SIGTERM. With `json_response=True` POSTs are plain JSON responses and drain normally.
- Kubernetes:
  - SIGTERM and EndpointSlice removal happen **concurrently**, and the default grace period is 30 s ([K1]).
  - preStop runs **before** SIGTERM and counts against `terminationGracePeriodSeconds` ([K2]).
  - The native `lifecycle.preStop.sleep` action is GA since v1.34 (beta since v1.30) ([K3]).
- Recommended shape:
  - `preStop: sleep: {seconds: 5–10}`, so ingress and kube-proxy stop routing before the socket closes.
  - Then uvicorn `timeout_graceful_shutdown` ≈ the longest legitimate request (a write = sync + commit + push; claude.ai caps a tool call at 240 s, [C1]). Something like 30–60 s.
  - `terminationGracePeriodSeconds` ≥ preStop + graceful timeout + lifespan cleanup + margin.
  - Writes in the queue whose HTTP request was cancelled at the timeout would be cancelled by `queue.stop()`. Git remains consistent because commit+push is atomic per job, but the caller sees an error.

### 5. Rate limiting and caches across replicas

| Option | Assessment | Source |
|---|---|---|
| Local token buckets with limit/N | Zero dependencies, but only correct with even, per-request balancing. HTTP keep-alive from Anthropic's egress (`160.79.104.0/21`) and connection reuse pin a client to one pod, so the effective limit drifts between limit/N and limit×N. Acceptable as a coarse abuse guard, not as an exact quota | reasoning; egress range from mcp-auth-and-connectors.md |
| Postgres fixed-window counter (`UNLOGGED` table, `INSERT … ON CONFLICT (key, window) DO UPDATE SET n = n+1 RETURNING n`) | No new service, since Postgres is already required for auth. Measured on 8 cores, pg16, 50 clients: **~28 600 upserts/s, 1.75 ms avg** over 200 keys; **~11 500/s, 4.4 ms** with all traffic on one hot key. A few hundred RPS is ~1–3 % of that. Costs one extra round-trip per request; needs a sweep of old windows (fits the existing hourly `_oauth_cleanup_loop`). UNLOGGED counters are lost on crash, which is fine for rate limits | [E3]; [PG1], [PG2] |
| Valkey / Redis | Lowest latency, true sliding window/GCRA via Lua. But it is a new service, which CLAUDE.md "When to ask" and "few dependencies" gate. Only pays off with cross-replica pub/sub needs (e.g. `SubscriptionBus`), which this server does not have | CLAUDE.md |

Licences:
- **Redis** ≤7.2: BSD-3. 7.4 (2024): RSALv2/SSPLv1. **Redis 8+: tri-licence RSALv2 / SSPLv1 / AGPLv3**, user's choice ([L1] `LICENSE.txt`; announced 2025-05-01 [L2]). Latest release 8.10.2 (2026-09-17) ([L3]).
- **Valkey**: BSD-3-Clause ([L4] `COPYING`). Latest 9.1.2 (2026-09-01) ([L5]).
- Python clients:
  - `redis` (redis-py) 8.1.0: **MIT** ([L6]). It speaks RESP, so it works against Valkey; Valkey is a fork of Redis 7.2.4 with protocol compatibility. Valkey compatibility is not documented as guaranteed by redis-py.
  - `valkey` (valkey-py) 6.1.1: MIT.
  - `valkey-glide` 2.5.3: Apache-2.0 ([L6]).
  - All three are AGPL-compatible.

**Is Postgres-only realistic at ~200 concurrent users / few hundred RPS?** Yes. Measured headroom is roughly 50–100× (fixed window), and per-key contention is negligible because each token has its own row.

Caches:
- CIMD documents (`auth/cimd.py:169`, 5 min–24 h TTL) and OIDC discovery (`login_oidc.py:153`, 1 h) can stay per-replica. They are pure caches of external, idempotent data; N replicas just mean N fetches.

### 6. In-process state in `src/memory_manager` that matters for several replicas

Listed in the reply; see "In-process state" below.

### In-process state

**Breaks correctness or security with >1 replica:**

1. `auth/ratelimit.py:89`: `RateLimiter._buckets` (in-memory token buckets), instantiated in `http.py:179-185` (`mcp`, `write`, `oauth`, `webhook` limiters). Each replica limits separately. The module docstring (`ratelimit.py:2-8`) states single-replica by design.
2. `auth/login_password.py:132-133`: `PasswordAuthenticator._by_ip` / `_global` failed-login windows. Brute-force protection becomes N× weaker.
3. `auth/login_oidc.py:155` (written :252, read :287/:350): `OidcAuthenticator._state`, the pending OIDC `state` → `code_verifier`/`pending_id` map, 600 s TTL (:97). **The IdP callback must hit the same replica that started `/login`, or login fails.** The docstring (:17) says "in-memory, single-replica". It needs to move into Postgres (`oauth_pending` already exists, `db/migrations/0003_oauth.sql:20`) or a sealed cookie/state.
4. `queue.py:331,371` + `app.py:101-104`: `WriteQueue` with its own `asyncio.Queue` and consumer task over **a per-replica git clone** (`repo.ensure_clone`/`repo.sync`).
   - Single-writer serialization only holds within one process.
   - Cross-replica writes race at `git push`. They are handled by `PushRejected` → rebase/retry (`queue.py:15-38`, `vault/git.py:70,121`), and `if_version` is checked after the pre-write sync. So it is probably safe but untested for N writers.
   - The deployment comment (`deploy/deployment.yaml:3-9`) and the chart schema pin `replicas: 1` with `Recreate`.
5. `app.py:121-123`: `poll_loop` per replica. Each replica syncs its own clone on its own timer. Reads (`memory_read` from the working copy) can be stale on other replicas until their next poll.
6. `http.py` `_vault_webhook` → `services.trigger_sync` (`http.py:869`). The webhook syncs **only the replica that received it**; the others wait for the poll.
7. Shared-index races from per-replica clones:
   - `app.py:174` startup `indexer.reindex()` and the queue's index/sync hooks (`app.py:116-118`) all write the **shared** Postgres index from different clone states. A replica behind on `HEAD` can re-index older content over newer.
   - Indexer upserts are idempotent (`index/indexer.py:232`), but not ordered by commit.
   - Migrations are safe: there is an advisory lock in `db/migrate.py:72`.

**Duplicated but harmless (or needs only a guard):**

8. `http.py:248`: `_oauth_cleanup_loop` hourly on every replica. Idempotent DELETEs, so this is just redundant work.
9. `auth/cimd.py:169`: `ClientMetadataFetcher._cache` (CIMD documents plus 60 s negative cache). Per-replica cache, OK.
10. `auth/login_oidc.py:153-154`: OIDC discovery cache (1 h). OK.
11. `vault/secrets.py:136`: `@lru_cache` rules load. OK.
12. `observability/metrics.py:71-90`: Prometheus counters/gauges per process (`QUEUE_DEPTH` is per replica). OK if scraped per pod.
13. `http.py:153`: `_OAuthProviderCell`, plus `app.state.*`. Per-process wiring, no shared data.

**Already shared (Postgres), fine:** OAuth clients, pending authorizations, codes, tokens (`oauth_*` tables), static tokens, audit log, index.

**SDK-internal:** `StreamableHTTPSessionManager._server_instances`/`_session_owners` are unused with `stateless_http=True`. MRTR `requestState` key is unused here.

### Decision impact

- **Keep `stateless_http=True` (and `json_response=True`).** In `mcp` 2.3.0 it makes both legacy (Claude's era) and 2026-07-28 traffic replica-agnostic: no session id, no session dict, no EventStore. The server uses none of the features this loses (elicitation, sampling, notifications, resumability, progress). Stateful would require sticky routing on `Mcp-Session-Id`, and the SDK offers no shared session store or EventStore implementation. Reject.
- **Client compatibility is acceptable but not documented.** A legacy GET gets an empty `200` SSE stream, not 405, which sidesteps the Claude Code GET-405 bug (#39790, status unverified). claude.ai's proxy never forwards GET at all (claude-ai-mcp#636, open; affects MCP Apps, not plain tools). No Anthropic doc mentions sessions or stickiness. Flag this as a residual risk, and add an integration check (GET → 200/held, DELETE → 405, `tools/call` without `initialize`) to CI.
- **The blockers for >1 replica are in this repo's code, not the SDK.**
  - In order: (a) OIDC pending `state` map (login breaks across replicas), (b) the single-writer git working copy, write queue, per-replica poll/webhook sync and index writes, (c) in-memory rate limiters and the password brute-force window.
  - (a) and (c) can move to Postgres without a new service. A fixed-window UNLOGGED upsert has ~50–100× headroom at a few hundred RPS (measured).
  - (b) is an architecture decision (ADR): for example a leader-elected writer via Postgres advisory lock with read-only replicas, or accepting N writers relying on push-reject/rebase, plus commit-ordered indexing.
- **Valkey/Redis is not needed.** Should it ever be, Valkey (BSD-3) with redis-py (MIT) or valkey-py (MIT) is licence-clean. Redis 8 is now AGPLv3-optional, which is compatible, but it is tri-licensed. Either way it would be a new service and needs owner approval per CLAUDE.md.
- **Graceful shutdown changes are cheap and independent of scaling:**
  - Set uvicorn `timeout_graceful_shutdown` (currently unbounded).
  - Add a `preStop` sleep (5–10 s, native `sleep` action on K8s ≥1.30/GA 1.34).
  - Set `terminationGracePeriodSeconds` ≥ preStop + grace + cleanup.
  - Keep `json_response=True`, so in-flight tool calls drain instead of being cut by sse_starlette's automatic SSE drain.
  - Switching to `RollingUpdate` only becomes safe after (b) is solved.

### Sources (all retrieved 2026-10-07)

- [P1] `.venv/lib/python3.12/site-packages/mcp/server/streamable_http_manager.py` (mcp 2.3.0)
- [P2] `.venv/lib/python3.12/site-packages/mcp/server/streamable_http.py` (mcp 2.3.0)
- [P3] `.venv/lib/python3.12/site-packages/mcp/server/_streamable_http_modern.py` (mcp 2.3.0)
- [D1] python-sdk docs "Deploy & scale": https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/deploy.md (last commit 65139550, 2026-10-02)
- [D2] python-sdk docs "Serving legacy clients": https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/legacy-clients.md
- [D3] python-sdk docs "Subscriptions": https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/handlers/subscriptions.md
- [G1] GitHub code search `repo:modelcontextprotocol/python-sdk EventStore` / `redis`
- [E1] Local probe: ad-hoc script, not kept (MCPServer, `stateless_http=True`, `json_response=True`, uvicorn 0.54.0)
- [E2] Local SIGTERM test: ad-hoc app + `curl -N` GET, with and without `timeout_graceful_shutdown`
- [E3] Local pgbench: `pgvector/pgvector:pg16` in podman 5.4.2, 8 cores, `-c 50 -j 4`, UNLOGGED `(key, win)` upsert
- [U1] `.venv/.../uvicorn/server.py:281-330` (uvicorn 0.54.0); [U2] `.venv/.../uvicorn/protocols/http/h11_impl.py:336-345`; [U3] `.venv/.../uvicorn/config.py:236`
- [S1] `.venv/.../sse_starlette/sse.py` (sse_starlette 3.5.0)
- [C1] Build an MCP server for Claude: https://claude.com/docs/connectors/building
- [C2] Troubleshoot your connector: https://claude.com/docs/connectors/building/troubleshooting
- [I1] anthropics/claude-code#39790: https://github.com/anthropics/claude-code/issues/39790
- [I2] anthropics/claude-code#78193: https://github.com/anthropics/claude-code/issues/78193
- [I3] anthropics/claude-ai-mcp#636: https://github.com/anthropics/claude-ai-mcp/issues/636
- [K1] Pod lifecycle, termination: https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/
- [K2] Container lifecycle hooks: https://kubernetes.io/docs/concepts/containers/container-lifecycle-hooks/
- [K3] KEP-3960 Pod lifecycle sleep action: https://www.kubernetes.dev/resources/keps/3960/
- [PG1] PostgreSQL INSERT … ON CONFLICT: https://www.postgresql.org/docs/16/sql-insert.html
- [PG2] PostgreSQL UNLOGGED tables: https://www.postgresql.org/docs/16/sql-createtable.html
- [L1] Redis LICENSE.txt: https://github.com/redis/redis/blob/unstable/LICENSE.txt
- [L2] Redis AGPLv3 coverage: https://simonwillison.net/2025/May/1/redis-is-open-source-again/ ; https://www.heise.de/en/news/Redis-another-license-turnaround-from-now-on-the-open-source-AGPL-applies-10369781.html
- [L3] Redis releases: https://api.github.com/repos/redis/redis/releases/latest
- [L4] Valkey COPYING: https://github.com/valkey-io/valkey/blob/unstable/COPYING
- [L5] Valkey releases: https://api.github.com/repos/valkey-io/valkey/releases/latest
- [L6] PyPI JSON: https://pypi.org/pypi/redis/json , https://pypi.org/pypi/valkey/json , https://pypi.org/pypi/valkey-glide/json

---

## §3 Research: PostgreSQL as source of truth at enterprise scale

Retrieved/verified: 2026-10-07. Scope: 2,000 active users, ~1M notes (Markdown ≤16 KB), ~5M chunks with 1024-dim (opt. 1536) embeddings, hybrid search (tsvector + pgvector, RRF) filtered by readable namespaces, RLS per request, targets search p95 < 300 ms, read p95 < 100 ms, write p95 < 200 ms, ≥3 API replicas.

Legend: **[doc]** = official documentation/release, **[lab]** = measured by me on 2026-10-07 in a `pgvector/pgvector:0.8.1-pg18` container (PostgreSQL 18.2, 8 vCPU, 47 GB RAM, slow shared disk, concurrent load at times; absolute timings are indicative only), **[est]** = my arithmetic from sourced numbers, **[unverified]** = could not confirm from a primary source.

### 0. Current state of the repo (read briefly)

- `src/memory_manager/db/migrations/0001_index_schema.sql`: `notes` (text ULID PK, `namespace text`, no RLS, no revision table), `chunks` (`embedding vector` untyped, `model`, `dimension`, two generated `tsvector` columns with GIN), `links`, `audit_log`. Header comment: everything except `audit_log` is derivable from Git.
- `index/indexer.py:403-410`: one partial HNSW expression index per `(model, dimension)`: `using hnsw ((embedding::vector(<dim>)) vector_cosine_ops) where model = … and dimension = …` — default `m`/`ef_construction`, fp32.
- `search.py:227-250`: vector query filters `n.namespace = any($7)` in a `chunks JOIN notes`, ordered by `<=>`; no `hnsw.ef_search` / `hnsw.iterative_scan` set anywhere (grep) → filtered queries currently run with `ef_search=40`, no iterative scan.
- `queue.py`: in-process single-consumer `asyncio.Queue` serialising all vault writes on one Git clone — does not extend to 3+ replicas.
- `http.py`/`config.py`: MCP Streamable HTTP, `stateless_http=True`, `json_response=True` by default.
- **Governance flag:** `CLAUDE.md` states "Git is the source of truth. Postgres must be fully rebuildable from the vault". Moving the source of truth to Postgres reverses that rule and the PLAN; it needs an ADR and owner sign-off before any implementation (CLAUDE.md "When to ask": new data model, contradiction PLAN/CLAUDE.md).

### 1. pgvector: version, HNSW, memory, filtering, RLS

**Versions.** pgvector latest **0.8.7 (2026-10-01)**; 0.8.3/0.8.4 (June 2026) fixed possible HNSW index corruption and `hnsw graph not repaired` errors during vacuum → run ≥ 0.8.4 [doc: CHANGELOG]. Licence: PostgreSQL License [doc: LICENSE]. PostgreSQL current: **18.6** (EOL 2030-11-14); 17.11; 16.15 [doc: versioning].

**HNSW parameters** [doc: README]: `m` = max connections per layer, default 16; `ef_construction` = candidate list at build, default 64 ("higher … better recall at the cost of index build time / insert speed"); `hnsw.ef_search` default 40, settable per transaction with `SET LOCAL`. Index dimension limits: `vector` ≤ 2,000 dims, `halfvec` ≤ 4,000, `bit` ≤ 64,000 — so both 1024 and 1536 are indexable as `vector` or `halfvec`.

**Storage per value** [doc]: `vector` = 4·d + 8 bytes (1024 → 4,104 B; 1536 → 6,152 B); `halfvec` = 2·d + 8 (1024 → 2,056 B; 1536 → 3,080 B). Column storage is `external` (`attstorage = e`) [lab], so values > ~2 KB live in TOAST.

**Measured HNSW size, 100k × 1024, m=16, ef_construction=64** [lab]:

| | index | per row | heap+TOAST | build (4 parallel workers, maintenance_work_mem 1 GB) |
|---|---|---|---|---|
| `vector(1024)` | 781 MB | **8,192 B** (exactly one element per 8 KB page) | 533 MB | 4 min 31 s |
| `halfvec(1024)` | 260 MB | **2,730 B** (three per page) | 271 MB | 25 min 9 s ¹ |

¹ halfvec build ran partly in parallel with another test on a slow disk; the 5.6× slowdown is unexplained (possibly the image's halfvec SIMD dispatch) — **re-measure on target hardware** [unverified cause].

**Extrapolation to 5M chunks** [est, linear in rows; page packing dominates]:

| | HNSW index | heap/TOAST embeddings | total embedding footprint |
|---|---|---|---|
| vector(1024) | ~41 GB | ~27 GB | ~68 GB |
| halfvec(1024) | ~14 GB | ~14 GB | ~28 GB |
| vector(1536) | ~41 GB (still 1/page) | ~31 GB | ~72 GB |
| halfvec(1536) | ~20 GB (2/page) [est] | ~15 GB | ~35 GB |

→ fp32 1024-dim wastes ~half of every index page; `halfvec` cuts the index ~3×. Recall loss of halfvec vs fp32 on bge-m3 is not documented by pgvector [unverified] — measure on the golden set (recall@5/MRR) before committing.

**Build / maintenance** [doc: README]: "Indexes build significantly faster when the graph fits into `maintenance_work_mem`"; NOTICE `hnsw graph no longer fits into maintenance_work_mem after N tuples` otherwise (seen in lab at 67k × 64-dim with 2 GB). Parallel build via `max_parallel_maintenance_workers` (default 2, + leader), may need `max_parallel_workers` (default 8). "Create an index after loading your initial data." In containers `--shm-size` must be ≥ `maintenance_work_mem` for parallel HNSW builds → in CNPG `spec.ephemeralVolumesSizeLimit.shm` [doc: CNPG API `EphemeralVolumesSizeLimitConfiguration.Shm`]. Vacuum of HNSW is slow: "Speed it up by reindexing first" (`REINDEX INDEX CONCURRENTLY` then `VACUUM`). For 5M halfvec: plan `maintenance_work_mem` ≈ 16 GB for the build session; build time at 5M is in the hours range [est from lab, unverified].

**Filtered search problem** [doc: README §Filtering]: "With approximate indexes, filtering is applied *after* the index is scanned. If a condition matches 10% of rows, with HNSW and the default `hnsw.ef_search` of 40, only 4 rows will match on average." Options listed by pgvector: B-tree on the filter column (exact, good for low-selectivity matches), iterative index scans, partial indexes ("if filtering by only a few distinct values"), partitioning ("if filtering by many different values"); multitenancy: "sharing an approximate index between tenants means vectors from one tenant can affect recall (and speed) for other tenants … use list partitioning or separate tables".

**`hnsw.iterative_scan` (0.8.0+)** [doc]: `off` (default) | `strict_order` | `relaxed_order` ("slightly out of order by distance, but provides better recall"; restore exact order with a `MATERIALIZED` CTE + `ORDER BY distance + 0` on PG17+). Stops at `hnsw.max_scan_tuples` (default 20,000, approximate, not applied to initial scan) or `hnsw.scan_mem_multiplier` × `work_mem` (default 1).

**Lab: RLS-filtered HNSW recall** (200k × 64-dim uniform random vectors — worst case for HNSW, so compare *relative* numbers; namespace mix: org 20 %, 400 groups 50 %, 2,000 personal 30 %; recall@10 vs exact; 20 queries) [lab]:

| visible set | ef_search | iterative off | relaxed_order |
|---|---|---|---|
| unfiltered (owner) | 40 / 200 | 0.29 / 0.72 | 0.33 / 0.77 |
| personal + 20 groups (2.5 %) | 40 | **0.07, avg 1.15 rows returned of 10** | 0.69 (10 rows) |
| personal + 20 groups (2.5 %) | 200 | 0.38 | 0.77 |
| + org (22.5 %) | 40 / 200 | 0.27 / 0.63 | 0.30 / 0.66 |
| strict_order, 2.5 % / 22.5 %, ef 40 | | | 0.28 / 0.23 |

Findings: without iterative scan a selective namespace filter returns too few rows (exactly the README warning); `relaxed_order` restores recall to the unfiltered level at ~4–5× latency; `strict_order` is much worse than relaxed here.

**Partitioning options** [lab, PG18]:
- *Per-namespace partial HNSW indexes*: pgvector recommends only for "a few distinct values" [doc]; ~2,500 namespaces → thousands of indexes, and a 1+30+1-namespace query cannot use one index. Rejected.
- *Hash partitioning by namespace (8 partitions)*: the RLS predicate `ns = ANY(current_setting(...)::text[])` did **not** prune partitions; the plan was a `Merge Append` of 8 HNSW scans, and partitions with no visible rows each scanned ~20k tuples (`max_scan_tuples`) — ~10× the buffers of the single-index plan. Rejected.
- *List partitioning by namespace kind* (`personal` / `group` / `org`): with the kind in the query, the planner pruned to one partition; on `personal` it chose a **B-tree bitmap scan + exact sort** (34 rows, 100 % recall) and on `group` the HNSW index with the RLS filter. → Works as intended: exact search where the set is tiny, HNSW + iterative scan where it is medium, unfiltered HNSW on `org`. Cost: 3 vector queries per search (fused by RRF anyway).

**RLS interaction with the planner** [doc: ddl-rowsecurity]: policy expressions are "evaluated for each row prior to any conditions or functions coming from the user's query. (The only exceptions … are `leakproof` functions … the optimizer may choose to apply such functions ahead of the row-security check.)" `LEAKPROOF` can only be set by a superuser [doc: CREATE FUNCTION]. In PG18: `texteq` is leakproof; `arraycontains` (`@>` on tags), `ts_match_vq` (`@@`), pgvector `cosine_distance` and `current_setting` are **not** [lab: `pg_proc.proleakproof`]. Consequence: user filters such as `tags @> …` and `tsv @@ query` are applied after the RLS qual, and cannot be pushed into an index ahead of RLS unless the RLS qual itself is index-usable. **HNSW ordering is unaffected**: the plan is `Index Scan using hnsw … Order By: (emb <=> q) Filter: (ns = ANY(...))` — RLS becomes a post-filter on the ordered stream, i.e. the same problem and the same cure (iterative scan) as an explicit WHERE [lab EXPLAIN]. A membership-subquery policy produced `Filter: (ANY (ns = (hashed SubPlan)))` on the same HNSW scan [lab].

### 2. RLS with session variables

- `current_setting(name, missing_ok)`: with `missing_ok = true` returns NULL if the setting doesn't exist [doc: functions-admin]. `set_config(name, value, is_local)`: `is_local = true` applies "only during the current transaction" (= `SET LOCAL`) and is usable with bind parameters [doc].
- `SET LOCAL`: "takes effect for only the current transaction … Issuing this outside of a transaction block emits a warning and otherwise has no effect" [doc: SET]. Plain `SET` persists for the session after commit → leaks to the next pool borrower.
- **Pitfall [lab]:** on a fresh connection `current_setting('app.ns', true)` is NULL, but after any transaction that did `SET LOCAL app.ns` it returns `''` (empty string) on that connection. A policy `ns = ANY(current_setting('app.ns', true)::text[])` then raises `malformed array literal: ""` on the next transaction that forgot to set it (fails closed, but as an error). Use `ns = ANY(coalesce(nullif(current_setting('app.ns', true), ''), '{}')::text[])` → zero rows.
- **PgBouncer transaction mode** [doc: pgbouncer features]: `SET/RESET`, `LISTEN`, `WITH HOLD` cursors, SQL `PREPARE/DEALLOCATE`, session advisory locks: "Never"; `NOTIFY`, protocol-level prepared plans (with `max_prepared_statements` > 0, default 200): yes. `SET LOCAL` is not listed; it is transaction-scoped and the server connection is held for the whole transaction, so it is safe [inference from SET + pooling-mode semantics].
- **Owner/bypass** [doc]: "Superusers and roles with the `BYPASSRLS` attribute always bypass the row security system … Table owners normally bypass row security as well, though a table owner can choose to be subject to row security with `ALTER TABLE … FORCE ROW LEVEL SECURITY`." "Referential integrity checks … always bypass row security" (covert-channel warning). Pattern: migrations run as owner role; app connects as a separate `NOLOGIN`-free, non-owner, non-`BYPASSRLS` role; `ENABLE` + `FORCE ROW LEVEL SECURITY` on every tenant table.
- **Array GUC vs membership join** [lab]: both keep the HNSW plan. Array GUC: planner sees a stable expression, cheap per-row `texteq` check; the API must compute the readable set per request (from token/IdP groups). Membership join: one hashed subplan per query (≈ index lookup on `(user_id, ns)`), authoritative in DB, but adds a DB-held authorization model (and the policy itself reads `membership`, so `membership` needs its own RLS/grants). Both are fine for 7–32 namespaces; array GUC avoids an extra table and lets the API cap the list.
- **Views:** default views apply "the row-level security policies of the view owner"; `security_invoker = true` applies the invoking user's [doc: CREATE VIEW]. → every view over RLS tables must be `security_invoker`.
- **SECURITY DEFINER:** executes with owner privileges (an owner-owned definer function bypasses RLS unless FORCE); set `search_path` with `pg_temp` last, `REVOKE ALL … FROM PUBLIC` in the same transaction [doc: CREATE FUNCTION].

### 3. Postgres job queue

- `FOR UPDATE SKIP LOCKED`: "any selected rows that cannot be immediately locked are skipped … not suitable for general purpose work, but can be used to avoid lock contention with multiple consumers accessing a queue-like table" [doc: SELECT].
- `NOTIFY`: delivered only on commit; duplicates within a tx collapsed; 8 GB queue, `NOTIFY` fails at commit when full; payload < 8,000 bytes; a long transaction in a listening session blocks cleanup [doc: NOTIFY]. A committing transaction that called `NOTIFY` takes a database-wide lock that serialises such commits (Recall.ai 2025; DBOS 2026-07-24: PG19 commit `282b1cde…` only optimises many-channel listening, global lock remains) [blog; not in official docs]. `LISTEN` does not work through PgBouncer transaction mode [doc] → dedicated direct connection per worker.
- **Outbox**: insert the job row in the same transaction as the note/revision write → atomic, no dual-write; worker deletes/marks done.
- **Lab throughput** [lab]: `DELETE … USING (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)` with 8 clients: ~3,800 dequeues/s (2.1 ms avg), ~2,170 tx/s mixed enqueue+dequeue — orders of magnitude above this workload's need (writes of 2,000 users; tens/s at peak [est]).
- **procrastinate**: MIT, v3.10.0 (2026-09-23), uses `SKIP LOCKED` + `pg_notify` triggers; **depends on psycopg 3** (`psycopg[pool]`, plus asgiref, attrs, croniter, python-dateutil …) — a second Postgres driver next to asyncpg [doc: repo/pyproject].
- **pgmq**: PostgreSQL License, v1.13.0 (2026-09-07), PG 14–18, can be installed SQL-only into a `pgmq` schema (no compiled extension) [doc: repo]. Not in CNPG standard images nor in `postgres-extensions-containers` (pgAudit, pg_crash, pg_ivm, pgRouting, pgvector, PostGIS, TimescaleDB-Apache, wal2json) [doc].
- **Valkey streams**: BSD-3-Clause, 9.1.2 (2026-09-01) [doc]; consumer groups (`XREADGROUP`, `XACK`, `XAUTOCLAIM`) [doc: streams-intro]. Adds a service, loses transactional enqueue with the DB write (needs outbox anyway). Not justified at this volume.

### 4. Revisions, optimistic concurrency, GDPR deletion

- Append-only `note_revisions(note_id, rev int, content, …, primary key(note_id, rev))` + `notes.current_rev`; write = `UPDATE notes SET current_rev = rev+1 … WHERE id = $1 AND current_rev = $if_version` → 0 rows = conflict; PK on `(note_id, rev)` blocks double insert. Keeps the "never overwrite silently" rule with an integer instead of sha256 (API change — ADR).
- Hard delete: `DELETE` does "not immediately remove the old version of the row"; `VACUUM` "marks the space available for future reuse" (bytes remain until overwritten); `VACUUM FULL` rewrites the file [doc: routine-vacuuming]. Physical replicas carry the same pages.
- WAL: contains row images and full-page snapshots [doc: continuous-archiving]; recovery needs a base backup plus "a continuous sequence of archived WAL files"; archived WAL older than the oldest kept base backup can be deleted [doc].
- Backup retention (Barman Cloud plugin): `ObjectStore.spec.retentionPolicy: "30d"` = recovery window; "the first valid backup is the most recent backup completed before the PoR"; older backups deleted "after the next backup completes" [doc: plugin retention]. → **Effective deletion horizon ≈ retention window + base-backup interval (+ object-store versioning/soft-delete, if enabled)** [est]. Whether WAL is pruned together with backups is barman behaviour, not stated on the plugin page [unverified].
- Regulator view (UK ICO): erasure from backups may follow the backup schedule if the data is put "beyond use" and individuals are told [doc: ICO right-to-erasure]. GDPR Art. 17 [doc]. Required: an erasure log (IDs only) re-applied after any restore/PITR.
- **Crypto-shredding** (per-namespace DEK wrapped by a KMS key, delete DEK = data unreadable in every backup): works for `content`/revisions, but tsvectors and embeddings must stay plaintext to be indexable, and embeddings are invertible ("recover 92 % of 32-token text inputs exactly", Morris et al., EMNLP 2023). Cost: app-side AES-GCM (new crypto dependency), KMS/key-store service, no SQL-side search over content, key-rotation tooling. Partial protection only.

### 5. CloudNativePG

- Latest **v1.30.1 (2026-09-23)**; 1.29.3, 1.28.4 maintained [doc: releases].
- In-tree `barmanObjectStore`: deprecated since 1.26; removal postponed from 1.30.0 to **1.31.0**; `spec.backup.retentionPolicy` deprecated [doc]. Replacement: **Barman Cloud Plugin** v0.15.1 (2026-09-30), `ObjectStore` CRD [doc].
- Object stores: S3 (+ S3-compatible), Azure Blob, GCS. Azure auth: connection string, account key, SAS, `inheritFromAzureAD`, `useDefaultAzureCredentials`; S3: access keys or IRSA [doc: plugin object stores]. PITR from "the first available base backup" [doc].
- Images [doc: postgres-containers]: `minimal` (no JIT from PG18), `standard` (= minimal + PGAudit, failover slots, **pgvector**, locales, JIT), `system` (deprecated, only one with Barman binaries; removed with in-tree barman). Debian trixie/bookworm, PG 13–18.
- ImageVolume extensions: PG ≥ 18 (`extension_control_path`), CNPG ≥ 1.27, Kubernetes 1.35 (feature on by default; 1.33–1.34 need the gate), containerd ≥ 2.1 or CRI-O ≥ 1.31; `ghcr.io/cloudnative-pg/pgvector:<ext>-<ts>-<pg>-<distro>` via `.spec.postgresql.extensions` [doc]. pgvector image tag observed `0.8.1-…` in docs; whether 0.8.7 is published yet [unverified].
- `Pooler` CRD: `instances`, `type: rw|ro`, `pgbouncer.poolMode` enum `session|transaction`, **default `session`** [doc: `api/v1/pooler_types.go`]; auth via `cnpg_pooler_pgbouncer` + `auth_query` + TLS cert; metrics on 9127; PgBouncer ≥ 1.19; latest PgBouncer **1.26.0 (2026-09-23)** [doc].
- Resources: Guaranteed QoS (requests = limits); `shared_buffers` ~25 % of memory [doc: resource_management].
- **Sizing for this workload** [est]: hot set ≈ halfvec HNSW 14 GB + 2 GIN tsvector indexes (several GB) + B-trees + hot note rows. Recommendation: 3 instances (1 primary, 2 replicas, sync quorum optional), each 8 vCPU / 64 GB (shared_buffers 16 GB, effective_cache_size ~48 GB), data PVC 500 GB SSD + separate WAL volume (`walStorage`), `ephemeralVolumesSizeLimit.shm` ≥ build `maintenance_work_mem`. With fp32 vectors: 128 GB RAM class. Validate with the load test.

### 6. Connection pool sizing

- PostgreSQL wiki: optimal active connections ≈ `(core_count × 2) + effective_spindle_count` (spindles = 0 when cached; untested on SSD) [doc: wiki].
- `max_connections` default 100, sizes shared memory; `superuser_reserved_connections` default 3; `reserved_connections` default 0 [doc]. Replication connections are governed by `max_wal_senders` (default 10) [doc].
- asyncpg (v0.32.0, 2026-10-06) with PgBouncer transaction mode: FAQ says use asyncpg's own pool, `statement_cache_size=0`, or session mode [doc: asyncpg FAQ]; PgBouncer ≥ 1.21-style `max_prepared_statements` (default 200) transparently re-prepares protocol-level named statements [doc: pgbouncer config] — asyncpg FAQ doesn't mention it [unverified compatibility → test].
- Formula (direct asyncpg pools): `max_connections ≥ N_api·P_api + M_worker·(P_worker + 1 LISTEN) + R_admin/migrations (~5) + superuser_reserved (3) + headroom`, while keeping **concurrently active** queries near `2 × cores` of the primary (8 cores → ~16–20).
  - Example N=3, P_api=10, M=2, P_worker=4: 30 + 10 + 5 + 3 = 48 → `max_connections = 100` (default) is enough.
- With `Pooler` (transaction mode, `type: rw`): `default_pool_size ≈ 2 × cores` (16–20) server connections, `max_client_conn ≥ N·P_api + M·P_worker`; workers' `LISTEN` connections bypass the pooler. Worth it once replicas autoscale (N ≥ ~10) or idle connections dominate.

### 7. Load testing and dataset

- **k6**: AGPL-3.0, v2.3.0 (2026-09-21), Go binary, JavaScript scripts [doc: repo]. Plain POST with JSON body + `Authorization: Bearer` is native [doc: k6 HTTP]; thresholds on `http_req_duration` p(95) fail the run (CI gate). No native SSE; extension `xk6-sse` or experimental streams [secondary sources].
- **Locust**: MIT, 2.46.7 (2026-10-04), Python; gevent-based, `FastHttpUser` for throughput; one process per core, `--processes`/distributed workers for more load [doc: increase-performance]. Pulls flask, gevent, pyzmq, geventhttpclient, … (dev-only).
- **MCP Streamable HTTP** [doc: MCP spec 2025-11-25]: every message is a POST; client `Accept` must list `application/json` and `text/event-stream`; server returns either; `MCP-Protocol-Version` header on subsequent requests; `MCP-Session-Id` only if the server assigns one. This server runs stateless with `json_response=True` → each `tools/call` is one independent POST returning JSON: both tools fit without SSE handling.
- **Synthetic 1M notes**: seeded generator (Python, in repo) producing valid frontmatter + Markdown, namespace sizes Zipf-distributed (2,000 personal, ~500 group, 1 org), lognormal body length capped at 16 KB, text from a seeded word model (no real personal data — CLAUDE.md). Embeddings: clustered, unit-normalised synthetic vectors (Gaussian mixture) for latency/capacity runs; real bge-m3 embeddings only for a recall subset (embedding 5M chunks is expensive [unverified throughput]). Load with `COPY`, create indexes after load, raise `maintenance_work_mem`/`max_wal_size`, disable archiving during bulk load [doc: populate].

### Sources (all retrieved 2026-10-07)

- pgvector README / CHANGELOG / LICENSE — https://github.com/pgvector/pgvector · https://raw.githubusercontent.com/pgvector/pgvector/master/CHANGELOG.md
- PostgreSQL versioning — https://www.postgresql.org/support/versioning/
- PG18 Row Security Policies — https://www.postgresql.org/docs/current/ddl-rowsecurity.html
- PG18 SET — https://www.postgresql.org/docs/current/sql-set.html
- PG18 config functions — https://www.postgresql.org/docs/current/functions-admin.html
- PG18 CREATE VIEW — https://www.postgresql.org/docs/current/sql-createview.html
- PG18 CREATE FUNCTION — https://www.postgresql.org/docs/current/sql-createfunction.html
- PG18 ALTER TABLE / CREATE ROLE — https://www.postgresql.org/docs/current/sql-altertable.html · https://www.postgresql.org/docs/current/sql-createrole.html
- PG18 partitioning — https://www.postgresql.org/docs/current/ddl-partitioning.html
- PG18 SELECT (SKIP LOCKED) — https://www.postgresql.org/docs/current/sql-select.html
- PG18 NOTIFY — https://www.postgresql.org/docs/current/sql-notify.html
- PG18 routine vacuuming — https://www.postgresql.org/docs/current/routine-vacuuming.html
- PG18 continuous archiving — https://www.postgresql.org/docs/current/continuous-archiving.html
- PG18 connections / replication settings — https://www.postgresql.org/docs/current/runtime-config-connection.html · https://www.postgresql.org/docs/current/runtime-config-replication.html
- PG18 populating a database — https://www.postgresql.org/docs/current/populate.html
- PostgreSQL wiki, Number of database connections — https://wiki.postgresql.org/wiki/Number_Of_Database_Connections
- Recall.ai, "Postgres LISTEN/NOTIFY does not scale" — https://recall.ai/blog/postgres-listen-notify-does-not-scale
- DBOS, "Postgres LISTEN/NOTIFY actually scales" (2026-07-24) — https://dbos.dev/blog/postgres-listen-notify-scalability
- procrastinate — https://github.com/procrastinate-org/procrastinate (pyproject.toml, procrastinate/sql/schema.sql, releases)
- pgmq — https://github.com/pgmq/pgmq
- Valkey — https://github.com/valkey-io/valkey · https://valkey.io/topics/streams-intro/
- CloudNativePG releases — https://github.com/cloudnative-pg/cloudnative-pg/releases
- CNPG backup docs — https://cloudnative-pg.io/docs/devel/backup
- CNPG ImageVolume extensions — https://cloudnative-pg.io/docs/devel/imagevolume_extensions
- CNPG connection pooling — https://cloudnative-pg.io/docs/devel/connection_pooling · API https://github.com/cloudnative-pg/cloudnative-pg/blob/main/api/v1/pooler_types.go · https://github.com/cloudnative-pg/cloudnative-pg/blob/main/api/v1/cluster_types.go
- CNPG resource management — https://cloudnative-pg.io/docs/devel/resource_management
- CNPG recovery — https://cloudnative-pg.io/docs/devel/recovery
- Barman Cloud plugin releases / retention / object stores — https://github.com/cloudnative-pg/plugin-barman-cloud/releases · https://cloudnative-pg.io/plugin-barman-cloud/docs/retention/ · https://cloudnative-pg.io/plugin-barman-cloud/docs/object_stores/
- CNPG postgres-containers — https://github.com/cloudnative-pg/postgres-containers
- CNPG postgres-extensions-containers — https://github.com/cloudnative-pg/postgres-extensions-containers
- PgBouncer features / config / changelog — https://www.pgbouncer.org/features.html · https://www.pgbouncer.org/config.html · https://www.pgbouncer.org/changelog.html
- asyncpg FAQ — https://magicstack.github.io/asyncpg/current/faq.html ; releases https://github.com/MagicStack/asyncpg/releases
- ICO, right to erasure — https://ico.org.uk/for-organisations/guide-to-dp/guide-to-the-uk-gdpr/individual-rights/right-to-erasure
- GDPR Art. 17 — https://gdpr-info.eu/art-17-gdpr/
- Morris et al., "Text Embeddings Reveal (Almost) As Much As Text", EMNLP 2023 — https://arxiv.org/abs/2310.06816
- k6 — https://github.com/grafana/k6 · https://grafana.com/docs/k6/latest/using-k6/http-requests/ ; xk6-sse https://pkg.go.dev/github.com/phymbert/xk6-sse (secondary)
- Locust — https://github.com/locustio/locust · https://docs.locust.io/en/stable/increase-performance.html
- MCP spec 2025-11-25, Transports — https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- Lab: own measurements in `docker.io/pgvector/pgvector:0.8.1-pg18` (PG 18.2), 2026-10-07; scripts were ad hoc and not kept.

### Decision impact

**Precondition:** making Postgres the source of truth reverses CLAUDE.md's "Git is the source of truth" rule and the PLAN. It needs an ADR and owner approval first. The same holds for the new schema (revisions, RLS) and for `if_version` changing from sha256 to an integer revision.

| Topic | Recommended default | Why (evidence) |
|---|---|---|
| **Index strategy** | Store embeddings as **`halfvec(1024)`** with HNSW `m=16, ef_construction=64` (cosine). **List-partition `chunks` by namespace kind** (`personal`/`group`/`org`). Per search, run one vector query per kind and fuse with RRF: `personal` uses a B-tree on `namespace` and an exact sort; `group` uses HNSW with `SET LOCAL hnsw.iterative_scan = relaxed_order`, `hnsw.ef_search = 100`, `max_scan_tuples` 20k (tune 20–50k); `org` uses unfiltered HNSW. Run pgvector ≥ 0.8.4 on PG 18. Build indexes after bulk load with `maintenance_work_mem` ≈ 16 GB and a matching shm size. Rebuild with `REINDEX CONCURRENTLY` before vacuuming the index. | fp32 HNSW at 1024 dims packs one element per page: 8,192 B/row, about 41 GB at 5M rows. halfvec needs 2,730 B/row, about 14 GB [lab]. Without iterative scan, a filter matching 2.5 % of rows returned 1.15 of 10 results; relaxed_order brought recall back to the unfiltered level [lab]. Hash partitions are not pruned by the RLS predicate and cost about 10× the buffers [lab]. Per-namespace partial indexes do not scale [doc]. Check halfvec recall on the golden set before adopting. |
| **RLS pattern** | Use a separate app role that does not own the tables and has no `BYPASSRLS`. Set `ENABLE` + `FORCE ROW LEVEL SECURITY` on all tenant tables. Each request runs in one transaction that starts with `select set_config('app.ns', $1, true)` (array of readable namespaces computed by the API from the token) and `set_config('app.user', …, true)`. Policy: `namespace = ANY(coalesce(nullif(current_setting('app.ns', true), ''), '{}')::text[])`. All views `security_invoker = true`. No SECURITY DEFINER functions owned by the table owner (or pinned `search_path` + `REVOKE … FROM PUBLIC`). | `SET LOCAL` is transaction-scoped and works with PgBouncer transaction mode; plain `SET` leaks to the next borrower [doc]. Once a connection has used `SET LOCAL`, `current_setting` returns `''` on it, and the naive `::text[]` cast then errors [lab]. The HNSW plan survives RLS as a post-filter [lab]. A membership-table join is an equivalent alternative if the authorization model should live in the DB. |
| **Queue** | Your own small `jobs` table: write the job in the same transaction as the note/revision write (outbox), dequeue with `FOR UPDATE SKIP LOCKED`, and use `LISTEN/NOTIFY` only as a wake-up hint on a dedicated direct connection per worker, with 1–5 s polling as fallback. No procrastinate, no pgmq, no Valkey. | About 3,800 dequeues/s on a dev box [lab], far beyond need. procrastinate pulls in psycopg 3 as a second driver. pgmq is not in the CNPG images (a SQL-only install is possible) and adds little. Valkey adds a service and loses transactional enqueue. NOTIFY serialises commits through a global lock, so keep it low-rate [blog]. |
| **Pool sizing** | Start with **direct asyncpg pools**: API `max_size=10` per replica, workers 4 + 1 LISTEN connection. `max_connections ≥ N·10 + M·5 + 5 admin + 3 reserved`; the default of 100 covers 3 API + 2 worker replicas (48). Keep active queries around 2× the primary's cores. Add a CNPG `Pooler` (`type: rw`, **`poolMode: transaction`** set explicitly because the default is `session`, `default_pool_size` ≈ 2×cores, `max_prepared_statements` > 0) once replicas autoscale past about 10. Test asyncpg through PgBouncer prepared statements first. | [doc: PG wiki, PgBouncer, CNPG API, asyncpg FAQ] |
| **Backup / deletion horizon** | CNPG ≥ 1.30 with the **Barman Cloud Plugin** (not in-tree `barmanObjectStore`, which is removed in 1.31), `standard` image (includes pgvector). S3 or Azure Blob, `retentionPolicy: "30d"`, weekly base backups plus continuous WAL. Documented **deletion horizon ≈ 30 d + 7 d (+ any bucket versioning/soft-delete)**. GDPR delete = hard `DELETE` of the note, revisions, chunks and embeddings in one transaction, plus an ID-only erasure log that is re-applied after every restore/PITR. No crypto-shredding in v1; revisit only for revision content. | Retention keeps the newest backup before the recovery point and deletes older ones only after the next backup [doc]. The ICO "beyond use" position allows backups to age out [doc]. Embeddings and tsvectors can't be encrypted and stay invertible, so crypto-shredding protects only part of the data [doc + paper]. |
| **Instance sizing** | 3 × (8 vCPU / 64 GB, Guaranteed QoS, `shared_buffers` 16 GB), 500 GB SSD data PVC plus a separate WAL volume. Plan for the 128 GB class if fp32 vectors are kept. | [est] from the lab sizes and CNPG guidance; confirm with the load test. |
| **Load test tool** | **k6** (in a container, JS script) for the latency gate: POST JSON-RPC `tools/call` with `Authorization: Bearer`, the `Accept: application/json, text/event-stream` header and `MCP-Protocol-Version`, plus p95 thresholds (search 300 / read 100 / write 200 ms) that fail CI. Write the dataset generator in Python in the repo: seeded, Zipf namespace sizes, ≤16 KB bodies, clustered synthetic vectors, loaded via `COPY`. Locust is the alternative if the owner prefers Python-only tooling. | Both fit, because the server is stateless with JSON responses, so no SSE is needed [repo + MCP spec]. k6 is a single binary with built-in thresholds; AGPL is compatible. Locust needs multiple processes for higher load and adds Python dev dependencies [doc]. |

**Not verified (`[unverified]` in the file):**
- halfvec recall loss on bge-m3.
- 5M-row build time.
- Why the halfvec build was slow in the lab.
- Whether barman prunes WAL together with backups.
- Whether a pgvector 0.8.7 extension image for CNPG is published.
- asyncpg compatibility with PgBouncer `max_prepared_statements`.
- bge-m3 embedding throughput.

---

