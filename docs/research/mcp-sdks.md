# Official MCP SDKs (Go, Python, TypeScript, Rust) plus git and Postgres libraries

Retrieved: 2026-10-06

Method: versions and dates come from the package registries (proxy.golang.org, PyPI JSON, npm registry, crates.io API). Capabilities come from shallow clones of each SDK's default branch on 2026-10-06; for Go, the `v1.8.0` tag was also checked. The GitHub releases API was not reachable from this environment.

## 1. SDK tiers

The MCP SDK page lists **TypeScript, Python, C#, Go, Rust and Ruby as Tier 1**, Java as Tier 2, and Swift, PHP and Kotlin as Tier 3. [T1]

## 2. Comparison

| | **Go** `modelcontextprotocol/go-sdk` | **Python** `mcp` (official) | **TypeScript** `@modelcontextprotocol/server` v2 | **Rust** `rmcp` |
|---|---|---|---|---|
| Latest release | **v1.8.0, 2026-09-04** [G1] | **2.3.0, 2026-10-02**. The 1.x line continues with 1.30.0 (2026-09-07) [P1] | **2.3.1, 2026-10-05**. v1 `@modelcontextprotocol/sdk` is at 1.32.1 (2026-10-05) [N1] | **3.5.1, 2026-10-05** [R1] |
| Stable since | v1.0+ (there is no `/v2` module) [G1] | 2.0.0 on 2026-07-28. Semver, 2.x on `main`, 1.x gets security fixes only [P1], [P2] | v2.0.0 on 2026-07-27. v1.x gets fixes for at least 6 months after that [N2] | 1.0.0 on 2026-03-03, 2.0.0 on 2026-06-29, 3.0.0 on 2026-07-28 (three majors in about 5 months) [R1] |
| Latest spec | **2026-07-28**. `latestProtocolVersion = 2026-07-28`, and it supports 2024-11-05 through 2026-07-28 [G2] | **2026-07-28**. Separate lists `HANDSHAKE_PROTOCOL_VERSIONS` (2024-11-05 to 2025-11-25) and `MODERN_PROTOCOL_VERSIONS` (2026-07-28) [P3] | **2026-07-28**, implemented in v2.0.0. Conformance runs against 2025-11-25 and 2026-07-28 [N2] | **2026-07-28** (`ProtocolVersion::LATEST`). Conformance is 100% for 2025-11-25 and 2026-07-28 [R2], [R3] |
| Dual-era (legacy plus modern) | Yes, with a caveat. In the stateful HTTP handler, 2026-07-28 requests are rejected. Use `StreamableHTTPOptions{Stateless:true}` for modern clients; that mode still serves legacy POST requests by creating a temporary session per request [G3] | Yes. There is a separate `_streamable_http_modern.py` alongside the stateful session manager, which keeps legacy `Mcp-Session-Id` sessions [P4] | Yes. The legacy session transport (`sessionIdGenerator` and others) is still present in `streamableHttp.ts` [N3] | Yes, both suites pass conformance [R3] |
| Streamable HTTP server | `mcp.NewStreamableHTTPHandler` (net/http) [G3] | ASGI/Starlette `streamable_http` [P4] | `streamableHttp.ts`, plus middleware packages for Express, Fastify, Hono and Node HTTP [N2] | `transport-streamable-http-server` (tower/axum) [R4] |
| stdio | Yes (`StdioTransport`) [G4] | Yes (`server/stdio.py`) [P4] | Yes (`serveStdio`) [N3] | Yes (`transport-io`, plus examples) [R4] |
| Server `instructions` | `ServerOptions.Instructions` [G5] | `MCPServer(instructions=...)` [P5] | Yes, in the server info and options, per spec | `ServerInfo.instructions` [R5] |
| Prompts | Yes (`AddPrompt`) [G4] | Yes (`@server.prompt`) [P5] | Yes | Yes (`prompt_stdio` example, macros) [R4] |
| **Resource-server auth** (bearer verify plus RFC 9728) | **Yes:** `auth.RequireBearerToken(verifier, opts)` middleware (checks expiry and scopes; `TokenInfoFromContext`) and `auth.ProtectedResourceMetadataHandler` (serves PRM with CORS) [G6] | **Yes:** `TokenVerifier` protocol, `AuthSettings`, bearer middleware, PRM routes [P6] | **Yes:** `requireBearerAuth`, `verifyBearerToken`, `buildWwwAuthenticateHeader`, `scopeChallenge`, and `oauthMetadata` (PRM) [N3] | **No built-in middleware.** The `auth` feature is client-side (PKCE, RFC 8707, PRM, AS discovery, DCR, CIMD). Server examples (`simple_auth_streamhttp.rs`, `cimd_auth_streamhttp.rs`) hand-roll axum middleware [R6], [R4] |
| **Authorization-server helpers** | **No.** `oauthex` has metadata types, DCR and token-exchange helpers for clients. Enterprise-managed auth and client credentials are in `auth/extauth` [G6], [G7] | **Yes:** `OAuthAuthorizationServerProvider` with authorize, token, register and revoke handlers. No CIMD support found in the server AS code [P6] | **Deprecated.** The AS helpers (`mcpAuthRouter`, `ProxyOAuthServerProvider`) moved to `@modelcontextprotocol/server-legacy`, "Frozen… use … a dedicated OAuth server in production" [N4] | No |
| Client OAuth (for tests) | Yes. `auth.NewAuthorizationCodeHandler` supports CIMD, pre-registered clients and DCR; RFC 9207 `iss` checks; SSRF guards [G6] | Yes | Yes (CIMD, DCR deprecation noted) [N3] | Yes (full) [R6] |
| Language / runtime floor | Go 1.25 [G8] | Python ≥3.10 [P2] | Node, Bun, Deno [N2] | Rust 1.88 MSRV [R7] |

**FastMCP (PrefectHQ, standalone):**
- Latest release is 4.0.11 (2026-10-04); 4.0.0 shipped on 2026-08-31, and a 3.x line is maintained (3.4.8). [F1]
- FastMCP 1.0 was merged into the official SDK in 2024. In `mcp` 2.x, `mcp.server.fastmcp` was **renamed to `mcp.server.mcpserver.MCPServer`**, and importing the old path raises an error. [P5], [F2]
- FastMCP 4 depends on `mcp>=2,<3`. [F1]
- It ships `JWTVerifier` (JWKS, issuer and audience checks), `RemoteAuthProvider` for DCR-capable IdPs, and others. [F3]

**Notes on each SDK:**
- **Go:**
  - The roadmap marks Tier-1 status and client OAuth as completed. Tasks are experimental. [G7]
  - DNS-rebinding protection is on automatically for localhost. For cross-origin checks, wrap the handler in `http.CrossOriginProtection`; the `StreamableHTTPOptions.CrossOriginProtection` field is deprecated. [G3]
  - MRTR is supported (`mcp/mrtr.go`, documented in `docs/server.md`). [G4]
- **TypeScript:** v2 is a package split into `@modelcontextprotocol/server`, `client`, `core` and `middleware`. [N2]
- **Rust:**
  - Major versions churn fast (1.0 → 3.0 in under 5 months). [R1]
  - The roadmap lists "v3.0.0 stable released (2026-07-28)". [R3]

## 3. Go git options

- **go-git v5** (`github.com/go-git/go-git/v5`): latest **v5.19.3, 2026-10-04**. [GG1] Its COMPATIBILITY.md ([GG2]) says:
  - Supported: `clone`, `fetch`, **`push`** and `tag`, over SSH and smart HTTP(S) with token, password or SSH-key auth.
  - `pull` and `merge` support **fast-forward only**.
  - **`rebase` is not supported.** `cherry-pick`, `stash` and multiple worktrees are also not supported.
  - Pack protocol v2 is not supported. Dumb HTTP is not supported.
- **go-git v6:** **v6.0.0-beta.1, 2026-10-04** (pre-release). It adds partial `cherry-pick` and SHA-256, but **`rebase` is still not supported** and merge is still fast-forward only. [GG1], [GG3]
- **Implication:** a design that needs `pull --rebase` or conflict handling should **shell out to the `git` CLI** (for example via `os/exec`) or restrict itself to fast-forward-only flows. go-git is fine for clone, fetch, commit, push and reading history.

## 4. Go Postgres and pgvector

- **pgx v5** (`github.com/jackc/pgx/v5`): latest **v5.11.0, 2026-09-07**. [PG1]
- **pgvector-go** (`github.com/pgvector/pgvector-go`): latest **v0.4.1, 2026-07-30**. Pre-1.0, but it supports pgx, pg, Bun, Ent, GORM and sqlx. With pgx you call `pgxvec.RegisterTypes(ctx, conn)`, typically from the `AfterConnect` hook. [PG2], [PG3]

## 5. Rust equivalent (one paragraph)

**git2:**
- **git2 0.21.0** (2026-05-18) binds libgit2 through `libgit2-sys 0.18.7+1.9.6`.
- It exposes `Repository::rebase` and `Remote::push`, so rebase and push are available in-process.
- The cost is a C dependency (libgit2, plus OpenSSL/libssh2 for transports).

Sources: [RG1], [RG2]

**gix (gitoxide):**
- **gix 0.88.0** (2026-09-25) is pure Rust and pre-1.0.
- Its status lists **push**, merge and **rebase** as unchecked.
- So it is not suitable for writing back to remotes yet.

Sources: [RG1], [RG3]

**Database:**
- **sqlx 0.9.0** (2026-05-21): async, pure-Rust Postgres driver with compile-time checked queries. [RG1], [RG4]
- **pgvector 0.4.2** (2026-05-22): crate with sqlx and tokio-postgres support. [RG1]
- **tokio-postgres 0.7.18**: an alternative driver. [RG1]

**Conclusion:** the Rust stack is viable (git2 is more capable than go-git), but rmcp lacks server-side auth middleware and churns through major versions.

---

## Sources
- [T1] MCP SDK list/tiers — https://modelcontextprotocol.io/docs/sdk (repo file docs/docs/2026-07-28/sdk.mdx); tiers: https://modelcontextprotocol.io/community/sdk-tiers
- [G1] Go module versions — https://proxy.golang.org/github.com/modelcontextprotocol/go-sdk/@v/list , https://proxy.golang.org/github.com/modelcontextprotocol/go-sdk/@latest ; https://pkg.go.dev/github.com/modelcontextprotocol/go-sdk
- [G2] https://github.com/modelcontextprotocol/go-sdk/blob/v1.8.0/mcp/shared.go
- [G3] https://github.com/modelcontextprotocol/go-sdk/blob/main/mcp/streamable.go
- [G4] https://github.com/modelcontextprotocol/go-sdk/blob/main/docs/server.md
- [G5] https://github.com/modelcontextprotocol/go-sdk/blob/main/mcp/server.go
- [G6] https://github.com/modelcontextprotocol/go-sdk/blob/main/docs/protocol.md#authorization ; https://pkg.go.dev/github.com/modelcontextprotocol/go-sdk/auth
- [G7] https://github.com/modelcontextprotocol/go-sdk/blob/main/ROADMAP.md
- [G8] https://github.com/modelcontextprotocol/go-sdk/blob/main/go.mod
- [P1] https://pypi.org/project/mcp/ (JSON: https://pypi.org/pypi/mcp/json)
- [P2] https://github.com/modelcontextprotocol/python-sdk/blob/main/VERSIONING.md ; pyproject.toml
- [P3] https://github.com/modelcontextprotocol/python-sdk/blob/main/src/mcp-types/mcp_types/version.py
- [P4] https://github.com/modelcontextprotocol/python-sdk/tree/main/src/mcp/server
- [P5] https://github.com/modelcontextprotocol/python-sdk/blob/main/src/mcp/server/fastmcp.py ; src/mcp/server/mcpserver/server.py
- [P6] https://github.com/modelcontextprotocol/python-sdk/tree/main/src/mcp/server/auth
- [N1] https://registry.npmjs.org/@modelcontextprotocol/server ; https://registry.npmjs.org/@modelcontextprotocol/sdk
- [N2] https://github.com/modelcontextprotocol/typescript-sdk (README.md, ROADMAP.md)
- [N3] https://github.com/modelcontextprotocol/typescript-sdk/tree/main/packages/server/src/server
- [N4] https://github.com/modelcontextprotocol/typescript-sdk/blob/main/packages/server-legacy/package.json
- [R1] https://crates.io/crates/rmcp (API: https://crates.io/api/v1/crates/rmcp)
- [R2] https://github.com/modelcontextprotocol/rust-sdk/blob/main/crates/rmcp/src/model.rs
- [R3] https://github.com/modelcontextprotocol/rust-sdk/blob/main/ROADMAP.md
- [R4] https://github.com/modelcontextprotocol/rust-sdk/tree/main/examples/servers/src ; crates/rmcp/Cargo.toml
- [R5] https://github.com/modelcontextprotocol/rust-sdk/blob/main/crates/rmcp/src/model.rs (ServerInfo.instructions)
- [R6] https://github.com/modelcontextprotocol/rust-sdk/blob/main/docs/OAUTH_SUPPORT.md
- [R7] https://github.com/modelcontextprotocol/rust-sdk/blob/main/Cargo.toml
- [F1] https://pypi.org/project/fastmcp/ ; https://pypi.org/project/fastmcp-slim/
- [F2] https://github.com/PrefectHQ/fastmcp (README)
- [F3] https://github.com/PrefectHQ/fastmcp/blob/main/docs/servers/auth/authentication.mdx
- [GG1] https://proxy.golang.org/github.com/go-git/go-git/v5/@latest ; https://proxy.golang.org/github.com/go-git/go-git/v6/@latest
- [GG2] https://github.com/go-git/go-git/blob/v5.19.3/COMPATIBILITY.md
- [GG3] https://github.com/go-git/go-git/blob/v6.0.0-beta.1/COMPATIBILITY.md
- [PG1] https://proxy.golang.org/github.com/jackc/pgx/v5/@latest
- [PG2] https://proxy.golang.org/github.com/pgvector/pgvector-go/@latest
- [PG3] https://github.com/pgvector/pgvector-go (README)
- [RG1] crates.io API — https://crates.io/crates/git2 , https://crates.io/crates/gix , https://crates.io/crates/sqlx , https://crates.io/crates/pgvector , https://crates.io/crates/tokio-postgres
- [RG2] https://github.com/rust-lang/git2-rs (src/repo.rs `rebase`, src/remote.rs `push`, libgit2-sys/Cargo.toml)
- [RG3] https://github.com/GitoxideLabs/gitoxide/blob/main/crate-status.md ; README "Features for 1.0"
- [RG4] https://github.com/launchbadge/sqlx (README)
