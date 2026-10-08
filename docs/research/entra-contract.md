# Entra ID / Microsoft Graph contract for `tests/mock_idp`

Retrieved 2026-10-08. Scope: exactly the shapes `tests/mock_idp/app.py` imitates for
ADR-0006's facade (WP-22, issue #212) - nothing more. `docs/research/enterprise.md`
§1 already covers the broader Entra/MCP landscape (DCR, CIMD, `resource`, CA,
throttling limits by tenant size); this note only pins down request/response bodies
so the mock can be built without guessing. All facts below came from a direct read
of the cited Microsoft Learn page on 2026-10-08 (`curl` against `learn.microsoft.com`,
HTML stripped to text), not from recall.

## 1. v2 discovery document

`GET /{tenant}/v2.0/.well-known/openid-configuration` ([access-tokens](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens),
cross-checked against the live probe already in `enterprise.md` §1.1). Fields the
mock serves: `issuer`, `authorization_endpoint`, `token_endpoint`, `jwks_uri`,
`response_types_supported`, `response_modes_supported`, `scopes_supported`,
`subject_types_supported: ["pairwise"]`, `id_token_signing_alg_values_supported`,
`token_endpoint_auth_methods_supported`. **`code_challenge_methods_supported` is
deliberately absent** - `enterprise.md` §1.1 confirms real Entra omits it from both
`common` and tenant-specific documents, and the facade (ADR-0006 §1) must work
without it, so the mock has to reproduce the omission, not "helpfully" add it.

## 2. Authorization request (`/authorize`) and PKCE

Parameters, from [v2-oauth2-auth-code-flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow):

```
GET /{tenant}/oauth2/v2.0/authorize?
  client_id=...&response_type=code&redirect_uri=...&response_mode=query
  &scope=...&state=...&code_challenge=...&code_challenge_method=S256
  &nonce=...                      (needed whenever an ID token is requested)
```

`redirect_uri` must exactly match a registered URI (path is case-sensitive).
`code_challenge_method` is `S256` or `plain`; the mock only accepts `S256` since
that is what ADR-0006 §1 requires the facade to send. PKCE verification:
`code_challenge == BASE64URL(SHA256(code_verifier))`, no padding.

## 3. Token request (`/token`): `authorization_code` and `client_credentials`

Same source, "Request an access token with a client_secret" and
[v2-oauth2-client-creds-grant-flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow):

```
POST /{tenant}/oauth2/v2.0/token
Content-Type: application/x-www-form-urlencoded

client_id=...&scope=...&code=...&redirect_uri=...&grant_type=authorization_code
&code_verifier=...&client_secret=...
```

(`client_secret` may instead arrive via HTTP Basic auth - `client_secret_basic`,
the scheme ADR-0006 §1 picks.) Successful response:

```json
{
  "access_token": "...",
  "token_type": "Bearer",
  "expires_in": 3599,
  "scope": "...",
  "refresh_token": "...",
  "id_token": "..."
}
```

`refresh_token` only appears when `offline_access` was requested; `id_token` only
when `openid` was. Client credentials request (app-only, for Graph):

```
POST /{tenant}/oauth2/v2.0/token
Content-Type: application/x-www-form-urlencoded

client_id=...&scope=https%3A%2F%2Fgraph.microsoft.com%2F.default
&client_secret=...&grant_type=client_credentials
```

`scope` for Graph is always the literal `https://graph.microsoft.com/.default`
(resource identifier + `.default`; "All scopes included must be for a single
resource"). Response shape is the same `{token_type, expires_in, access_token}` as
above, no `refresh_token` ("refresh tokens will never be granted with this flow").
Error shape (either grant): `{"error": "...", "error_description": "..."}`,
400 Bad Request.

## 4. ID token claims

From [id-token-claims-reference](https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference)
and [access-token-claims-reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference),
plus `enterprise.md` §3 for the fields already pinned there:

- `iss`: `https://login.microsoftonline.com/{tid}/v2.0` - the concrete tenant GUID
  on a tenant-specific authority (never the `{tenantid}` template, which only
  appears on `common`/`organizations`). `ver: "2.0"`.
- `aud`: the facade's own `client_id` (GUID), per v2 semantics.
- `tid`: tenant GUID. `oid`: the Graph `id` of the user, stable across apps in one
  tenant - what ADR-0006 §3 keys identity on, not `sub`.
- `sub`: pairwise per application ID, i.e. a different value per `client_id` for
  the same user. The mock derives a stable-but-pairwise-looking value
  (`SHA256(client_id:oid)`) rather than reusing `oid`, so a test cannot
  accidentally rely on `sub == oid`, which real Entra never gives.
- `nonce`: echoed verbatim from the authorize request. "If it doesn't match, your
  application should reject the token" - the mock always echoes exactly what it
  received, so a test can also construct a *mismatching* nonce on the facade side
  to prove that rejection, without the mock's help.
- `roles`: the user's assigned app roles (ADR-0006 §3: `Memory.User`,
  `Memory.Curator`, `Memory.Admin`).
- `groups`: object IDs, subject to overage (below).
- `idp`: present and different from `iss` for guest (B2B) users - "identical to
  the value of the issuer claim unless the user account isn't in the same tenant
  as the issuer - guests, for instance". So a guest's `tid`/`iss` are still the
  **resource tenant's** (the tenant they signed into, same as any member), while
  `idp` names their home tenant's STS. The mock does not model guests (ADR-0006
  has no guest-specific behaviour to test against); this is recorded so a future
  task knows `tid`/`iss` alone cannot distinguish a guest from a member.

### Groups overage

[id-token-claims-reference](https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference)
("Groups overage claim"), exact shape:

```json
{
  "_claim_names": { "groups": "src1" },
  "_claim_sources": { "src1": { "endpoint": "<url to this user's group membership>" } }
}
```

`groups` is omitted entirely when this fires (limit: 200 for JWTs, `enterprise.md`
§5). ADR-0006 §4 has the facade fall back to Graph `getMemberGroups` on this
signal; the mock lets a test force it per user regardless of how many groups that
user actually has, so the fallback path is testable without creating 200 groups.

## 5. Microsoft Graph: `getMemberGroups`

[directoryobject-getmembergroups](https://learn.microsoft.com/en-us/graph/api/directoryobject-getmembergroups?view=graph-rest-1.0),
page updated 2026-04-07:

```
POST /users/{id}/getMemberGroups
Content-Type: application/json

{ "securityEnabledOnly": false }
```

Response:

```json
{
  "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#Collection(Edm.String)",
  "value": ["<group-id>", "..."]
}
```

**Application permissions** (least to most privileged), confirmed on this page for
"Group memberships for a user": `User.ReadBasic.All` + `GroupMember.Read.All`,
`User.Read.All` + `GroupMember.Read.All`, ... This matches ADR-0006's addendum
2026-10-08 choice of `User.Read.All` + `GroupMember.Read.All`.

## 6. Microsoft Graph: `users/delta`

[user-delta](https://learn.microsoft.com/en-us/graph/api/user-delta?view=graph-rest-1.0)
and [delta-query-overview](https://learn.microsoft.com/en-us/graph/delta-query-overview),
both retrieved 2026-10-08:

```
GET /users/delta                                    (first round, full sync)
GET /users/delta?$deltatoken=<token>                 (next round, from a prior deltaLink)
GET /users/delta?$skiptoken=<token>                  (next page within a round)
```

Response always has a `value` array plus **either** `@odata.nextLink` (more pages
in this round) **or** `@odata.deltaLink` (round complete; save this whole URL
for the next round's request):

```json
{
  "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#users",
  "@odata.nextLink": "https://graph.microsoft.com/v1.0/users/delta?$skiptoken=...",
  "value": [ { "id": "...", "displayName": "...", "userPrincipalName": "...", "...": "..." } ]
}
```

Removed objects: `{"id": "...", "@removed": {"reason": "changed" | "deleted"}}` -
`changed` means soft-deleted/restorable, `deleted` means permanently gone. Delta
tokens for directory objects (users included) are valid **7 days**; an expired or
reset token gets `410 Gone` with a `Location` header carrying a fresh request URL
with an empty `$deltatoken` ("Synchronization reset" in delta-query-overview),
meaning the application must restart with a full sync. The mock does not implement
the 410/7-day expiry itself (no test in #212 exercises it); it is recorded here so
#222/WP-24's deprovisioning worker test can decide whether to add it later.
**Least-privileged application permission:** `User.Read.All` (both pages agree;
`User.ReadBasic.All` is explicitly not listed as sufficient for delta).

## 7. Graph throttling: 429 and `Retry-After`

General shape, from [throttling](https://learn.microsoft.com/en-us/graph/throttling):

```
HTTP/1.1 429 Too Many Requests
Content-Type: application/json
Retry-After: 10

{
  "error": {
    "code": "TooManyRequests",
    "message": "Please retry again later.",
    "innerError": { "code": "429", "status": "429", "request-id": "...", "date": "..." }
  }
}
```

"All the resources and APIs described in the Service-specific limits provide a
`Retry-After` header **except where indicated**." `enterprise.md` §5 already
records that the *identity-and-access* throttling tier (`User.getMemberGroups`
sits under it) is explicitly one of the exceptions - no `Retry-After` there,
clients must back off themselves. The mock's fault injection lets a test choose
either shape (with or without `Retry-After`) per call, since both are real
Graph behaviour depending on which limit tripped - the facade has to cope with
both.

## 8. 503 (Graph unreachable)

Not a documented Graph response shape as such - this is ADR-0006's own addendum
2026-10-08 ("Graph unreachable during a refresh: the facade answers with a
retryable error (HTTP 503, `temporarily_unavailable`)"), i.e. the contract the
*facade* must expose to its own callers when Graph itself is down. The mock's
fault injection can return a bare `503` from any Graph endpoint to let a WP-24
test drive that facade behaviour; it does not claim 503 is Graph's own
documented throttling shape (that is 429, §7 above).

## Sources (all retrieved 2026-10-08)

- https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow
- https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow
- https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference
- https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference
- https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens
- https://learn.microsoft.com/en-us/graph/api/directoryobject-getmembergroups?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/api/user-delta?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/delta-query-overview
- https://learn.microsoft.com/en-us/graph/throttling
- https://learn.microsoft.com/en-us/graph/throttling-limits (cross-check only, already cited in `enterprise.md` §5)
