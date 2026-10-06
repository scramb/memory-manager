# Reference implementation: `scramb/bring--mcp`

Retrieved: 2026-10-06 · Commit `0b010da` ("Replace the API key with OAuth and per-user Bring! accounts") · Release deployed: `0.4.0`

An existing MCP server by the same owner that works today as a claude.ai custom connector. Read to reuse what is proven and to spot differences.

## Findings

| Aspect | bring--mcp | Source |
|---|---|---|
| Language / SDK | Python ≥ 3.11, official MCP Python SDK `mcp` 2.3.0 (`mcp.server.mcpserver.MCPServer`), Starlette, uvicorn, asyncpg | `pyproject.toml`, `uv.lock` |
| Transport | Streamable HTTP at `/mcp`, `MCP_JSON_RESPONSE=true` (JSON responses instead of SSE) | `deploy/deployment.yaml` |
| Authorization server | **Embedded in the app** — the SDK's `OAuthAuthorizationServerProvider`. **No Ory Hydra involved.** | `src/bring_hermes/oauth.py`, `server.py` |
| Endpoints | `/.well-known/oauth-protected-resource/mcp`, `/.well-known/oauth-authorization-server`, `/register` (DCR), `/authorize`, `/token` (PKCE), `/revoke`, own `/login` page | `README.md` "How it works" |
| Client registration | Dynamic Client Registration enabled, single scope `bring` | `server.py` `ClientRegistrationOptions` |
| Resource indicator | `validate_token_resource=False` — RFC 8707 `resource` not enforced ("clients that omit the indicator still work") | `server.py` |
| Tokens | Opaque random strings, stored as SHA-256 hashes; access 1 h; refresh rotates on every use, token families | `store.py`, `README.md` |
| Identity | Login with the user's Bring! credentials; token subject = Bring! user uuid | `oauth.py` |
| Abuse protection | In-memory limiter: 5 failed logins per e-mail per 10 min (forces 1 replica) | `oauth.py`, `deployment.yaml` |
| Server instructions | `instructions=` string on the server | `server.py` |
| Persistence | Postgres via CloudNativePG `Cluster`, `sslmode=require` | `deploy/database.yaml` |
| Secrets | `ExternalSecret` pulling from OpenBao | `deploy/README.md` |
| Kubernetes | Kustomize base in `deploy/`; Flux in `scramb/tethys` applies it, impersonating a namespace-scoped deployer role; Gateway API `HTTPRoute` on the Istio ingress gateway; Istio ambient mesh with L4 isolation; non-root, read-only root FS, drop ALL caps, seccomp RuntimeDefault; `/healthz` + `/readyz` (503 when DB unreachable) | `deploy/*.yaml`, `deploy/README.md` |
| Releases | Tag pinned in `kustomization.yaml`, never `latest`, no image automation; Helm chart exists but is not used on the cluster | `deploy/kustomization.yaml`, `deploy/README.md` |

## Consequences for memory-manager

1. **The working claude.ai pattern is "embedded AS + DCR + PKCE + opaque hashed tokens".** It is proven against claude.ai by this repo. Hydra as external AS is *not* proven by it.
2. **Deployment conventions to adopt:** Kustomize base in `deploy/` consumed by Flux in `scramb/tethys`, Gateway API `HTTPRoute` (not `Ingress`), CNPG, ExternalSecret/OpenBao (rather than SOPS as the brief suggests), Istio ambient, pinned tags. A Helm chart remains useful for other OSS users.
3. **`validate_token_resource=False`** suggests that claude.ai's `resource` parameter handling was not relied on — to be checked against the connector research before ADR-0004.
4. **Single replica** was accepted there; memory-manager needs single-writer anyway (write queue), so the same constraint is acceptable.
5. The Python SDK 2.x is the one the owner has running experience with — relevant for ADR-0001 (Python is outside the technology pool).
