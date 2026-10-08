# ADR-0006 — Enterprise auth: the embedded authorization server becomes a facade in front of Entra ID

Status: Accepted · Date: 2026-10-07
Relates to: auth, F-xx Enterprise Scale; extends [ADR-0004](./0004-auth-model.md)

## Context

Enterprise mode must authenticate about 2,000 employees through Microsoft Entra ID, map Entra app roles and groups to permissions ([ADR-0008](./0008-namespace-permissions.md)), and cut off deprovisioned users. claude.ai custom connectors and Claude Code must keep working without per-user setup. Facts from [`docs/research/enterprise.md`](../research/enterprise.md) §1 and [`mcp-auth-and-connectors.md`](../research/mcp-auth-and-connectors.md):

- claude.ai uses only the **first** `authorization_servers` entry in the PRM. It refuses an AS whose metadata lacks `code_challenge_methods_supported`, and it sends RFC 8707 `resource` = canonical MCP URL.
- Entra's v2 OIDC discovery document has **no** `code_challenge_methods_supported`, and there is **no** RFC 8414 document (live probe 2026-10-07). Entra offers **neither DCR nor CIMD**, and Microsoft gives no date for either.
- Entra rejects a `resource` that does not match the identifier URI of the requested scope (`AADSTS9010010`). The https identifier URI must be on a domain verified in the tenant, and the v2 `aud` is the app's client ID, never the MCP URL.
- Continuous Access Evaluation does not cover custom APIs. A validated Entra JWT stays valid for 60–90 min after the user is disabled.
- Conditional Access applies to a confidential client that requests an ID token. CA is evaluated at sign-in, not on every API call.
- Entra ignores the port on `http://localhost` redirects (public "Mobile and desktop" platform).

ADR-0004 already gives us an embedded AS: PRM, AS metadata, DCR, CIMD, PKCE S256, enforced audience, opaque hashed tokens, rotating refresh, revocation. It also has an upstream OIDC login with no JOSE dependency (identity from a TLS-direct token response).

## Options

### A — Clients use Entra directly with a pre-registered client
The PRM points at Entra. claude.ai uses "your own OAuth client", Claude Code uses `--client-id`, and the server validates Entra JWTs via JWKS.
Pro: no token issuance in our code; CA applies on each Entra refresh · Con: **does not work with claude.ai** (no `code_challenge_methods_supported`). Claude Code hits the `resource` bugs. The audience is a GUID, not our URL. Every user configures a client ID. Disabled users keep access for up to 90 min. Needs a JOSE dependency.

### B — Authorization-server facade (extend the embedded AS)
MCP clients keep talking to our AS: DCR, CIMD, PKCE and `resource` exactly as today. `/authorize` delegates the login to Entra. The facade is one **confidential** OIDC client per tenant (authorization code + PKCE, tenant-specific authority, plain v2 scopes `openid profile offline_access`, no `resource` towards Entra). It takes `oid`, `tid`, `roles` and `groups` from the ID token. It issues its **own** short-lived opaque tokens, bound to the Entra user.
Pro: works with claude.ai and Claude Code today; reuses tested code; audience binding stays ours; revocation is under our control; no per-user setup · Con: we keep owning token issuance; CA is re-evaluated only when the facade goes back to Entra (see Decision); a Graph app permission is needed for overage and deprovisioning.

### C — Upstream gateway (Azure API Management or similar)
Pro: Microsoft maintains a sample · Con: APIM only validates tokens. DCR, `/authorize` and `/token` are hand-written policies (sample marked "Experimental"). That is option B in a proprietary, Azure-only product, which conflicts with OSS first, container by default and generic deployment.

## Decision

**B**, accepted by the owner on 2026-10-07. The owner also decided: no JWKS check of the ID token in v1; Graph application permissions with admin consent are a prerequisite; the Graph delta sync is enough for deprovisioning in v1 (no SCIM). Concretely:

1. **New login mode `entra`** next to `oidc` and `password` (ADR-0004 L3). It reuses the OIDC client in `auth/login_oidc.py`.
   - Authority `https://login.microsoftonline.com/<tenant-id>/v2.0` (never `common`).
   - Confidential client with `client_secret_basic`.
   - Allowed tenant list `ENTRA_ALLOWED_TENANTS`, default = the authority's tenant only.
   - `tid`, `iss`, `aud`, `exp` and `nonce` of the ID token are checked.
2. **ID-token signature.** The ID token comes TLS-direct from the token endpoint, so OIDC Core §3.1.3.7 lets TLS server authentication replace the signature check. That is the ADR-0004 addendum pattern, with no new dependency. Additionally verifying the signature via JWKS (defence in depth) would cost one JOSE dependency (`joserfc`, BSD-3) plus a JWKS cache. The owner decided against it on 2026-10-07; it comes back only together with resource-server mode (item 8).
3. **Identity.** The user key is `oid`, not the pairwise `sub`. Roles come from the `roles` claim (`Memory.User`, `Memory.Curator`, `Memory.Admin`). A user with none of these is denied. The app registration sets "assignment required".
4. **Groups.**
   - Recommended: `groupMembershipClaims = ApplicationGroup`, so only groups assigned to the app appear and overage becomes practically irrelevant.
   - On overage (`_claim_names.groups` or `hasgroups`), the facade calls Graph `POST /users/{oid}/getMemberGroups` with its own app-only token (`GroupMember.Read.All`, `User.ReadBasic.All`, admin consent).
   - The result is cached in Postgres with a configurable TTL (default 1 h).
5. **Facade tokens.**
   - Access token 15 min (configurable). Refresh rotating, as today.
   - **Every refresh** re-checks the user against Graph (`accountEnabled`, existence) and refreshes groups when their TTL has expired.
   - After `ENTRA_MAX_SESSION` (default 12 h, meant to match the tenant's CA sign-in frequency) the refresh token dies, and the client must log in through Entra again. That is where CA, MFA and sign-in frequency are re-applied.
   - Entra refresh tokens are **not** stored.
6. **Deprovisioning.**
   - A worker job runs a Graph delta query on users every 5 min (configurable).
   - `accountEnabled=false` or `@removed` marks the user disabled and revokes all of their token families. Token validation already reads the token row, so cut-off is immediate once the change is detected.
   - Worst case: the 5 min delta interval, bounded above by the 15 min access-token lifetime.
   - Retention handling of personal memories after deletion is in [ADR-0008](./0008-namespace-permissions.md). SCIM is a later option and not in v1.
7. **Static tokens** stay. In enterprise mode, creating one requires explicit scopes, an expiry date (maximum configurable, default 90 days) and an owner. Existing deployments without enterprise mode are unchanged.
8. **Not included:** accepting Entra-issued access tokens directly (resource-server mode for service principals). It would need JWKS validation and therefore JOSE. It is a follow-up if CI or service principals need it.
9. **`deploy/entra/`**: OpenTofu/Terraform module (`azuread` provider 3.x, MPL-2.0) for the app registration, app roles, web redirect to `<public-url>/oidc/callback`, "assignment required", group claims, the client secret, and the Graph application permissions. Admin consent stays a manual operator step.

Checked against the guardrails:
- Few dependencies: none new (Graph over `httpx`; JOSE only if the sub-decision says so).
- OSS first: yes; Entra itself is the customer's IdP and not a project dependency.
- Container: no extra service.
- Technology pool: within ADR-0001; Terraform/OpenTofu is infrastructure tooling.

## Consequences

- The AS metadata and PRM don't change. claude.ai keeps using CIMD or DCR, and Claude Code keeps using CIMD.
- New tables: `users` (oid, tid, display name, disabled_at, last_seen), `user_groups` cache, and the delta-query cursor. Token rows reference `users`.
- Graph application permissions need tenant-admin consent. That has to be stated in the operator guide and the compliance templates.
- CA is enforced at login and every `ENTRA_MAX_SESSION`, not per call. The compliance templates must state this, and operators align `ENTRA_MAX_SESSION` with their sign-in frequency policy.
- Pending login state must live in shared state (Valkey or Postgres) before there is more than one replica ([ADR-0009](./0009-stateless-replicas.md)).

## Reversibility

Cheap to medium. Tokens are opaque and internal, so adding resource-server mode later is additive. Switching to option A would need clients and Entra to change first.

## Addendum 2026-10-08 — Graph permissions, refresh behaviour and mode limits

Cutting WP-22 and WP-24 into issues raised five points the decision above leaves open or gets wrong. The owner decided on 2026-10-08:

- **Graph application permissions are `User.Read.All` and `GroupMember.Read.All`.** The users delta query of §6 needs `User.Read.All` as its least-privileged application permission; `User.ReadBasic.All` is not enough ([research](../research/enterprise.md) §1, Microsoft Graph reference retrieved 2026-10-08). `User.Read.All` with `GroupMember.Read.All` also covers `getMemberGroups` (§4). This replaces `User.ReadBasic.All` in §4 and §9. Admin consent stays a manual operator step.
- **A removed app role takes effect at the next Entra login**, at the latest after `ENTRA_MAX_SESSION`. The refresh re-check in §5 stays limited to `accountEnabled`, existence and groups. Reading `appRoleAssignments` on every refresh would need `Directory.Read.All` and is rejected. When a role must go immediately, a `Memory.Admin` revokes the user's sessions and tokens in the admin area on `/account`.
- **Graph unreachable during a refresh:** the facade answers with a retryable error (HTTP 503, `temporarily_unavailable`) and neither rotates nor consumes the refresh token. Access is never granted without the check, and a short Graph outage does not force every user to sign in again.
- **`LOGIN_MODE=entra` requires `STORAGE_BACKEND=postgres`.** The server refuses to start otherwise, because roles, groups and `me` exist only with the namespace registry ([ADR-0008](./0008-namespace-permissions.md)).
- **The owner of an enterprise static token must exist in `users`**, i.e. must have signed in once. Otherwise the delta sync could never revoke the token of a departed owner.
