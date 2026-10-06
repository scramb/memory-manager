# ADR-0001 — Implementation language: Python 3.12+ with the official MCP Python SDK

Status: Accepted · Date: 2026-10-06
Relates to: all application code; T-004 (toolchain skeleton)

## Context

The server needs: Streamable HTTP + stdio, an OAuth 2.1 authorization server that claude.ai accepts (DCR, PKCE, refresh rotation, revocation), Postgres with pgvector, Git operations including rebase, and a CLI. The technology pool in `CLAUDE.md` is Go, Rust, C, C++, React, Vue.js — Python is outside it and therefore a gate.

Facts from [`docs/research/mcp-sdks.md`](../research/mcp-sdks.md) (retrieved 2026-10-06):

| SDK | Release | Resource-server auth | Authorization-server helpers |
|---|---|---|---|
| Python `mcp` | 2.3.0 (2026-10-02) | yes | **yes** — `OAuthAuthorizationServerProvider` (authorize, token, DCR, revoke) |
| Go `go-sdk` | v1.8.0 (2026-09-04) | yes | no |
| TypeScript `@modelcontextprotocol/server` | 2.3.1 | yes | deprecated (moved to `server-legacy`) |
| Rust `rmcp` | 3.5.1 (2026-10-05) | **no** built-in middleware | no; three major versions between 03/2026 and 07/2026 |

The owner runs [`scramb/bring--mcp`](../research/bring-mcp-reference.md) in production as a claude.ai connector: Python, `mcp` 2.3.0, embedded authorization server built on exactly that provider.

## Options

### A — Python 3.12+, official `mcp` SDK, Starlette/uvicorn, `uv`
Pro: only SDK with authorization-server helpers; proven by the owner against claude.ai; OAuth/token/store code from bring--mcp is directly reusable; mature libraries for every other need (psycopg 3 + pgvector, pydantic, ULID, YAML) · Con: outside the technology pool; larger container image than a static binary; typing only as strong as mypy strict makes it.

### B — Rust with `rmcp`
Pro: in the pool; single static binary; strongest correctness guarantees; owner's preference ("cooler") · Con: no server-side auth support in the SDK — PRM, bearer middleware **and** the whole authorization server would be hand-written; SDK API churn (3 majors in 5 months); `gix` lacks rebase/push, `git2` needed.

### C — Go with `go-sdk`
Pro: in the pool; resource-server auth built in; small images · Con: no authorization-server helpers (hand-written or an external AS); stateful HTTP mode rejects the 2026-07-28 protocol (must run `Stateless`); `go-git` has no rebase — git CLI needed anyway.

## Decision

**A — Python.** The authorization server is the riskiest part of the project (claude.ai is strict and poorly debuggable), and Python is the only stack where the SDK provides it and where the owner has a working reference. The owner approved the deviation from the technology pool on 2026-10-06 ("Python ist fine. Rust wäre cooler, aber ist kein Muss").

Checked against the guardrails:
- Few dependencies: `mcp`, `starlette`, `uvicorn`, `psycopg[binary,pool]`, `pgvector`, `pydantic` (via `mcp`), `pyyaml`, `httpx` (embeddings, via `mcp`), `argon2-cffi`; each covers a real problem. No ORM (plain SQL + migrations), no web framework beyond Starlette.
- OSS first: all OSS.
- Container: multi-stage build with `uv`; runtime image needs the `git` binary (ADR-0003).
- Technology pool: deviation approved by the owner on 2026-10-06.

Tooling: `uv` (lock file), `ruff` (lint + format), `mypy --strict`, `pytest` + `testcontainers`, `pre-commit`.

## Consequences

- OAuth provider, token store and rate limiter follow bring--mcp; differences (audience validation, CIMD) are in ADR-0004.
- `make check` = `ruff format --check && ruff check && mypy && pytest`.
- Image is larger than a Go/Rust binary (~60–120 MB); acceptable.
- Re-evaluate if `rmcp` or `go-sdk` gain authorization-server support **and** the Python SDK falls behind on spec revisions — visible in `docs/research/mcp-sdks.md` updates.

## Reversibility

Expensive after M2 (full rewrite). The data format (Markdown + Git) and the DB schema are language-neutral, so a rewrite would not touch user data.
