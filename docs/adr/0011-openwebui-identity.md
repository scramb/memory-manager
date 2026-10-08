# ADR-0011 — Open WebUI identity: per-user OAuth for tools, personal tokens for the filter, no trusted headers in v1

Status: Accepted · Date: 2026-10-08
Relates to: auth, integrations/openwebui, F-02 Client Integrations (M13, WP-42 … WP-46); [ADR-0004](./0004-auth-model.md), [ADR-0006](./0006-enterprise-auth-entra.md), [ADR-0008](./0008-namespace-permissions.md)

## Context

Every Open WebUI request must run as the real user and land in that user's namespace, never under a shared service account (F-02). Open WebUI reaches memory-manager over two paths, which need an identity each:

1. **Tools.** The model calls `memory_*` through Open WebUI's native MCP client (Streamable HTTP). An admin configures the connection.
2. **Filter.** `integrations/openwebui/filter_memory.py` runs inside Open WebUI before each request (`inlet`). It calls `memory_search` and injects a context block. In `outlet` it acts only on explicit "remember …" or "forget …" requests from the user. A filter receives `__user__` (Open WebUI user id, email, role) and `__oauth_token__` (the user's SSO token at Open WebUI's own IdP). It does **not** receive the per-user OAuth token of the MCP connection. That is unverified, and the spike in WP-42 settles it.

What Open WebUI v0.11.4 offers ([research](../research/clients/openwebui.md)):

| Mode | Per user? | Fits today's auth model? |
|---|---|---|
| `oauth_2.1` (DCR) / `oauth_2.1_static`: Open WebUI is an OAuth client of **our** AS, each user consents once | yes | yes: DCR, PKCE S256 and `resource` exist. Not yet verified: `client_secret_post` and the redirect `/oauth/clients/<id>/callback`. No CIMD. |
| `system_oauth`: forward the user's SSO access token | yes | no: needs resource-server mode for IdP-issued tokens, which [ADR-0006](./0006-enterprise-auth-entra.md) §8 excludes from v1 |
| Trusted user headers (`X-OpenWebUI-User-*`, or HS256 `X-OpenWebUI-User-Jwt`) | yes | no: a new "trusted proxy" mode. The JWT secret is global and shared with every backend Open WebUI calls, and the identity is Open WebUI's id, not ours or the IdP's `oid`. |
| `bearer` (one key per connection) | no | yes, but a shared service account is forbidden by F-02 |

## Options

### A — Tools: per-user OAuth 2.1 against our AS · Filter: personal token per user (`UserValves`)
- **Tools.** Each user connects once, through the same login as claude.ai: `oidc`/`password` in single-user mode, the Entra facade in enterprise mode.
- **Filter.** The filter uses a personal token ([ADR-0012](./0012-personal-tokens.md)), scope `memory:read` (plus `memory:write` only when `outlet` is on). The user pastes it into the filter's per-user `UserValves`. Without one, the filter does nothing.

Pro: no new trust path; identity is ours, bound to `oid` in enterprise mode; works in single-user mode with the Git backend. Every token is revocable and audited per user.
Con: two set-up steps per user (OAuth consent, paste a token). OAuth tools cannot be model defaults in Open WebUI, so each user enables the tool per chat; the filter covers automatic recall. Where Open WebUI stores `UserValves`, and whether it encrypts them, is unverified (WP-42).

### B — Forward Open WebUI's SSO token (`system_oauth`, and `__oauth_token__` in the filter)
Pro: no extra consent and no token pasting when Open WebUI and the facade share Entra.
Con: memory-manager must validate Entra-issued tokens (JWKS, JOSE dependency, `aud`/`iss`/`oid`). ADR-0006 rejected that for v1. It needs Open WebUI to request our API scope at login (effect on its Graph features is unverified). It does not work for local Open WebUI accounts or with the Git backend.

### C — Trusted user headers, hard-off by default
The signed `X-OpenWebUI-User-Jwt` header is accepted only with a dedicated secret and only from allowlisted networks or over mTLS. A mapping turns the Open WebUI user into our principal.
Pro: zero user interaction; works for filter and tools alike.
Con: Open WebUI becomes a fully trusted impersonator of every user. The HS256 secret is shared with every backend Open WebUI talks to, so any of them could mint identities. It weakens a security guardrail and needs its own threat model.

### D — One service token
Rejected: it violates the per-user identity requirement.

## Decision

**A**, accepted by the owner on 2026-10-08, who also confirmed Python for `integrations/openwebui/` (the filter and tool run inside Open WebUI, which only executes Python). Option C (trusted headers) stays a possible later addition for zero-touch onboarding, but only through its own ADR with a threat model and never as the default. The deciding reason is that it adds no trust path: every request carries a token our AS or our CLI issued to that user, revocable and audited. B stays the follow-up once resource-server mode exists (ADR-0006 §8).

If the WP-42 spike shows that Open WebUI's DCR request cannot be served without weakening the AS (for example, it needs an open redirect pattern), this ADR goes back to the owner before any workaround.

Checked against the guardrails:
- Few dependencies: none new.
- OSS first: yes.
- Container: unchanged.
- Technology pool: the filter is Python because Open WebUI only runs Python functions. That extends the ADR-0001 deviation to `integrations/openwebui/`, confirmed by the owner on 2026-10-08.

## Consequences

- The AS must accept confidential DCR clients with `client_secret_post` and Open WebUI's redirect URI shape. This is verified live in WP-42 against a pinned Open WebUI.
- Personal tokens (ADR-0012) become a prerequisite of the filter. Without self-service (`/account`, WP-25), users get them from the CLI or from an admin.
- The docs must say plainly: enable the memory tool per chat, paste a read-only token into the filter.

## Reversibility

Cheap. All modes are configuration in Open WebUI. B and C can be added later without touching A.
