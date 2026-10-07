# ADR-0004 — Auth model: embedded OAuth 2.1 authorization server + static tokens, upstream OIDC for login

Status: Accepted · Date: 2026-10-06
Relates to: MCP server, M4 (WP-10, WP-11); T-041, T-042, T-043

## Context

Facts from [`docs/research/mcp-auth-and-connectors.md`](../research/mcp-auth-and-connectors.md), [`ory-hydra-for-mcp.md`](../research/ory-hydra-for-mcp.md), [`bring-mcp-reference.md`](../research/bring-mcp-reference.md):

- The MCP server MUST serve Protected Resource Metadata (RFC 9728) and answer 401 with `WWW-Authenticate: Bearer resource_metadata=…`.
- Clients MUST use PKCE S256 and send RFC 8707 `resource`; the server MUST check the token audience and never pass tokens through. claude.ai sends the canonical server URL as `resource`.
- claude.ai picks client registration in this order: pre-registered client ("use your own OAuth client"), CIMD (only if the AS advertises `client_id_metadata_document_supported` **and** `none` auth), DCR. Static bearer headers are only a limited beta. No `client_credentials`.
- Claude Code uses its own CIMD with loopback redirects on random ports, or `--header "Authorization: Bearer …"`, or `--client-id/--callback-port`.
- claude.ai calls come from 160.79.104.0/21; OAuth endpoints must answer within 10 s; refresh tokens must rotate for public clients.
- Spec 2026-07-28 deprecates DCR in favour of CIMD — but Claude still speaks 2025-11-25.
- The owner's bring--mcp proves: embedded AS via the Python SDK provider + DCR + PKCE + opaque hashed tokens + rotating refresh works with claude.ai. It did **not** enforce the audience (`validate_token_resource=False`).
- Ory Hydra (v26.2.0) works as an external AS only with a custom consent app that maps `resource` → `audience`; it has no CIMD and its DCR endpoint is unauthenticated.

There are two separate questions: **who issues tokens** (authorization server) and **how a human proves identity** during `/authorize` (login).

## Options — authorization server

### A — Embedded AS (bring--mcp pattern), plus static tokens
The server is its own AS via `OAuthAuthorizationServerProvider`: PRM, AS metadata, DCR, `/authorize`, `/token` (PKCE S256 only), `/revoke`. Opaque tokens, SHA-256 hashed in Postgres, access 1 h, refresh rotating with family revocation. Audience **enforced** (unlike bring). Static bearer tokens (`token create`, hashed, scoped) for Claude Code and CI.
Pro: proven against claude.ai by the owner; one deployable; works for every self-hoster without an IdP · Con: we own security-critical OAuth code (mitigated: the SDK does the protocol, we implement storage + login); DCR is deprecated in the newest spec (CIMD can be added on top later).

### B — External AS (Hydra/Keycloak/…), server is resource server only
Pro: no OAuth code in the project; central identity · Con: Hydra needs a custom consent app for audience binding and DCR cleanup; not proven with claude.ai by the owner; every self-hoster needs an IdP; generic external-AS support means per-IdP quirks.

### C — A now, B as optional mode later
External-AS mode (introspection / JWKS verification + PRM pointing to the external issuer) added post-v0.1 if users ask.

## Options — login at `/authorize`

### L1 — Built-in single admin password (argon2 hash from env/secret)
Pro: zero dependencies on other systems; 5-minute quickstart · Con: one more password; brute-force protection needed (rate limit as in bring).

### L2 — Upstream OIDC login (the embedded AS is an OIDC *client* of Kratos/Hydra, Keycloak, Authelia, Google, …)
Pro: reuses existing identity incl. MFA/passkeys; maps `sub`/claims to namespaces; the owner's Ory stack fits directly here — without Hydra having to understand MCP · Con: one more dependency (OIDC client library), redirect config per deployment.

### L3 — Both, configured per deployment

## Decision

Accepted by the owner on 2026-10-06. **A + C for the authorization server, L3 for login:** embedded AS with enforced audience, PKCE S256 only, rotating refresh, revocation; static scoped tokens for Claude Code and CI; login via upstream OIDC (production, e.g. the owner's Ory stack) **or** a single admin password (quickstart). External-AS mode stays a later option. CIMD support (advertising + fetching client metadata documents with SSRF guards) is planned as a follow-up within M4 so that Claude Code and future clients can skip DCR.

Scopes: `memory:read`, `memory:write`. Token subject → allowed namespaces via config (`namespaces: {"<sub>": ["carsten"]}`).

Checked against the guardrails:
- Few dependencies: OAuth protocol from `mcp`; `argon2-cffi` for the admin password; one OIDC client library for L2 (`authlib` or a ~200-line own client on `httpx` + `joserfc` — decided in T-043).
- OSS first: yes.
- Container: no extra service required; with L2 the IdP is external.
- Technology pool: within ADR-0001.

## Consequences

- PRM `resource` must equal the user-entered URL exactly; `PUBLIC_URL` + `MCP_PATH` is canonicalised once at startup.
- DCR-registered clients accumulate (one per claude.ai connect); a cleanup of clients without live tokens runs periodically.
- Redirect URIs: exact match, plus loopback (`127.0.0.1`/`localhost`, any port) for Claude Code.
- `/authorize` and `/token` must stay well under 10 s — no embedding or Git work in that path.
- Rate limiting state is in-process (single replica), same as bring.

## Reversibility

Medium. Token format and DB tables are internal; switching to an external AS later means adding verification, not removing data. The login method is configuration.

## Addendum 2026-10-07 — implementation notes (#36, #37)

- **Client secrets of confidential DCR clients are encrypted at rest, not hashed.** The SDK's `ClientAuthenticator` compares `client_secret` in plaintext and has no injection point, so a hash cannot be checked. These secrets are stored with Fernet (authenticated encryption, key `OAUTH_CLIENT_SECRET_KEY`, required when the AS is on). Access tokens, refresh tokens and authorization codes remain SHA-256 hashes. claude.ai and Claude Code register as public clients (no secret), so the encrypted path only serves rare confidential clients.
- **`resource` is mandatory** at `/authorize` and `/token`; a missing or foreign value is `invalid_target`.
- **Upstream OIDC client (L2):** no new dependency. The client is ~200 lines on `httpx`: discovery, authorization code + PKCE, confidential `client_secret_basic`. The identity is taken from the `userinfo` endpoint, called with the access token that the token endpoint returned directly over TLS. Because of that, no ID-token signature validation (JOSE) is needed. An allowlist (`OIDC_ALLOWED_EMAILS` / `OIDC_ALLOWED_SUBJECTS`) is mandatory and denies by default, because an upstream IdP may issue tokens to any of its accounts.
