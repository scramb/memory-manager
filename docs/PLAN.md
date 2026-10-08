# PLAN — memory-manager

> **Work backlog: [`docs/TASKS.md`](./TASKS.md) — always read it alongside.**
> This PLAN describes goal, architecture and decisions. What concretely needs doing
> lives exclusively in `docs/TASKS.md`.
> Source of truth for tasks: GitHub issues in `scramb/memory-manager`; `docs/TASKS.md` is the readable mirror.

Last updated: 2026-10-08 (F-02 decisions) · Development rules: [`CLAUDE.md`](../CLAUDE.md)

## Goal

A self-hosted, production-grade long-term memory for Claude that claude.ai (web/mobile) and Claude Code share through one remote MCP server. Human-readable Markdown in Git is the source of truth; Postgres (pgvector + full text) is a derived, rebuildable search index. A small set of well-described tools plus built-in usage rules keeps the memory curated instead of cluttered.

**Done (v0.1.0) when:** a user deploys the server (Compose or Helm/Flux), adds it as a custom connector in claude.ai and via `claude mcp add` in Claude Code, a fact saved in one client is found by `memory_search` in the other, the change is visible as a Git commit in the vault repo — and the signed release is on GHCR.

## Scope / not in scope

**In scope**
- Vault: Markdown + YAML frontmatter in a Git repo, one commit per change, authored by the client
- Serialized write queue with optimistic concurrency and conflict files
- Indexer + hybrid search (BM25/`ts_rank` + vector, RRF), pluggable embeddings, full-text fallback
- MCP server over Streamable HTTP and stdio; tools, server instructions, prompt `memory_guide`
- OAuth 2.1 (ADR-0004), static tokens, scopes, rate limits, audit log, secret scan
- CLI: `init`, `serve`, `reindex`, `doctor`, `import`, `export`, `token create`
- Container image, Compose, generic Kustomize base with deployment guide, Helm chart, Flux example — public-safe, operator values in the operator's own overlay
- Claude Code skill + `CLAUDE.md` snippet; retrieval eval in CI

**Explicitly not (v1)**
- Editing UI (Git, Obsidian or an editor do that); a read-only web view may come in v1.x
- LLM-based automatic fact extraction from chats (mem0-style) — Claude writes explicitly via tools
- Multi-tenancy across organisations; several users via namespaces only
- Hard deletion of notes through MCP tools (erasure exists only in enterprise mode, outside MCP — ADR-0007)

## Architecture

```
claude.ai ──(HTTPS + OAuth)──┐
Claude Code ──(HTTP/stdio)───┼──► MCP server ──► write queue ──► Git vault (Markdown, source of truth) ──► remote
                             │        │                               │
                             │        └──► search ◄── indexer ◄───────┘ (after commit / webhook / poll)
                             │                 │
                             │           Postgres (pgvector + tsvector)
                             └── embeddings: pluggable (Ollama/bge-m3 | OpenAI-compatible | none)
```

One process, one replica for writes (single-writer). The process owns the local clone; nothing else writes to it. Human edits arrive through the remote (webhook or poll) and are pulled before every write.

**Enterprise mode** (`STORAGE_BACKEND=postgres`, [ADR-0007](./adr/0007-storage-backend.md)–[ADR-0009](./adr/0009-stateless-replicas.md)): Postgres is the source of truth with append-only revisions. Several stateless `api` replicas sit behind a plain load balancer, and a `worker` deployment runs embedding jobs, the Graph delta sync and retention. Login is delegated to Entra ID through the embedded authorization server. Namespaces are personal (`me`), group, project or `org`, enforced by RLS and application code. Shared state lives in Valkey if configured, else Postgres.

### Components

| Component | Task | Technology | Runs as |
|---|---|---|---|
| Vault | parse/validate/serialize notes, path safety, links | Python | module in the server |
| Git | clone, commit, fetch, rebase, push | git CLI (ADR-0003) | subprocess |
| Write queue | serialize writes, `if_version` check, conflict files, secret scan, audit | Python asyncio | module |
| Indexer | chunk by headings, hash-based incremental indexing, `reindex --full` | Python | background task |
| Search | full text + vector, RRF, note-level dedup, filters | SQL on Postgres 16 + pgvector | module |
| Embeddings | provider interface: Ollama, OpenAI-compatible, none | `httpx` | external service |
| MCP server | tools, instructions, prompt; Streamable HTTP + stdio | official `mcp` SDK 2.x (ADR-0001) | container / local process |
| Auth | embedded OAuth 2.1 AS, static tokens, login via upstream OIDC, admin password or Entra ID (facade) | `mcp` auth provider (ADR-0004, ADR-0006) | module |
| Storage backend | `StorageBackend` protocol: Git (default) or Postgres with revisions (enterprise) | Python + SQL (ADR-0007) | module |
| Namespaces + authz | principal, aliases, permission matrix; Postgres RLS | Python + SQL (ADR-0008) | module + DB policies |
| Shared state | rate limits, brute-force window, pending login state | Valkey (optional) or Postgres (ADR-0009) | module |
| Worker | embedding job queue (`SKIP LOCKED`), Graph delta sync, retention, cleanup | Python | separate deployment (enterprise) |
| Account pages | self-service export/delete, admin area (namespaces, ACLs, erasure, break-glass) | server-rendered HTML | module in the server |
| Compatibility profiles | per-client limits and instruction delivery, schema linter | Python (ADR-0010) | module |
| Usage rules | `docs/memory-guide.md` → instructions, prompt, short form, client instruction files | Python generator | CLI + CI check |
| Client adapters | `connect <client>` config merge, `doctor --client` | Python | CLI |
| Client integrations | Open WebUI filter/tool, instruction files and config examples per client | Python (Open WebUI) + config | `integrations/<client>/` |
| CLI | operations | Python entry point | same image |

### Data flow (write)

1. Client calls `memory_edit(path, old_str, new_str, if_version)`.
2. Auth middleware checks token, audience, scope `memory:write`, namespace.
3. Write queue (serial): `git fetch` + fast-forward; compare `if_version` with the current content hash → conflict returns current content + version.
4. Apply edit, validate note (frontmatter, `description`, size), secret scan.
5. `git commit` with client as author, `git push`; on rejection rebase, on rebase failure write `*.conflict.md` and report.
6. Audit log row (who, what, when, commit SHA); indexer is notified and re-indexes the changed file.

### Data model

Note file `<namespace>/<type>/<slug>.md` with frontmatter `id` (ULID), `title`, `description` (required, ≤150 chars), `type`, `tags`, `aliases`, `created`, `updated`, `valid_from`, `valid_to`, `supersedes`, `source`. `[[slug]]` links become rows in `links`. File size cap (16 KB) forces consolidation. Postgres tables: `notes`, `chunks` (text, tsvector, embedding, model, dimension), `links`, `audit_log`, plus auth tables (`oauth_clients`, `tokens`, `static_tokens`). The exact format (charsets, canonical serialization, archive layout) is fixed in [ADR-0005](./adr/0005-note-format.md).

### Protocol targets

- Claude speaks MCP **2025-11-25** today; the newest spec is **2026-07-28** (stateless, `server/discover`, DCR deprecated). The SDK serves both on one endpoint; we test against both. ([research](./research/mcp-auth-and-connectors.md))
- Server instructions ≤ 2,048 characters (Claude Code truncates); tool results well under 150,000 characters (claude.ai cap).

## Technology decisions

| Topic | Decision | Instead of | Why (one sentence) | ADR |
|---|---|---|---|---|
| Language | Python 3.12+, official `mcp` SDK, `uv` | Rust/`rmcp`, Go/`go-sdk` | only SDK with authorization-server helpers; owner's bring--mcp proves it with claude.ai | [ADR-0001](./adr/0001-implementation-language.md) |
| License | AGPL-3.0-only + DCO | Apache-2.0, MIT | hosted derivatives must stay open | [ADR-0002](./adr/0002-license.md) |
| Git access | git CLI wrapper | pygit2, GitPython | correct rebase/conflict behaviour for free | [ADR-0003](./adr/0003-git-access.md) |
| Auth | embedded OAuth AS + static tokens; login via upstream OIDC or admin password | external AS (Hydra) | proven pattern; no IdP required for self-hosters | [ADR-0004](./adr/0004-auth-model.md) |
| Note format | Markdown + canonical YAML frontmatter, `<namespace>/<type>/<slug>.md`, 16 KB cap | free-form YAML, TOML | byte-stable round trip makes `if_version` meaningful | [ADR-0005](./adr/0005-note-format.md) |
| Database | PostgreSQL 16+ with pgvector, plain SQL + versioned migrations | ORM, dedicated vector DB | one well-known service for full text + vectors | — (set by brief) |
| Enterprise auth | embedded AS as facade, login delegated to Entra ID, own short-lived tokens bound to `oid`; Graph delta sync for deprovisioning | clients against Entra directly, APIM gateway | Entra has no DCR/CIMD and lacks `code_challenge_methods_supported`, so claude.ai cannot use it directly | [ADR-0006](./adr/0006-enterprise-auth-entra.md) |
| Storage backend | `StorageBackend` interface: Git (default) or Postgres with append-only revisions (enterprise) | scaled Git, Postgres only | horizontal writes and GDPR erasure without breaking single-user/team mode | [ADR-0007](./adr/0007-storage-backend.md) |
| Namespaces + permissions | registry with aliases (`me`, groups, projects, `org`) in the existing path format; RLS derives access from membership tables, app checks independently | type-prefixed IDs in paths, app-computed namespace list | no tool contract break; two independent access computations | [ADR-0008](./adr/0008-namespace-permissions.md) |
| Client compatibility | one strict tool surface for all clients, profiles change only how usage rules are delivered, selected by `?profile=`/header/`clientInfo` | per-client tool surfaces | one contract and test matrix that works on any stateless replica | [ADR-0010](./adr/0010-client-compatibility-profiles.md) |
| Open WebUI identity | per-user OAuth for tools, personal token for the filter; Python allowed in `integrations/openwebui/` | SSO token forwarding, trusted headers | no new trust path | [ADR-0011](./adr/0011-openwebui-identity.md) |
| Personal tokens | owner-bound static tokens with `kind`, self-service on `/account` | refresh tokens, CLI only | users get revocable credentials without an operator | [ADR-0012](./adr/0012-personal-tokens.md) |
| Agent runtimes | own agent identity and namespace, server-side write policy with approval queue; MCP integration only in v1 | runtime guards, native plugins | the write guard sits outside the agent's prompt | [ADR-0013](./adr/0013-agent-identity.md), [ADR-0014](./adr/0014-agent-integration-tier.md) |
| Clients without DCR/CIMD | operator-registered confidential OAuth clients | Entra routing, unsupported | per-user identity for Gemini Enterprise without resource-server mode | [ADR-0015](./adr/0015-preregistered-oauth-clients.md) |
| Documentation | plain Markdown on GitHub, `docs/README.md` as index | MkDocs Material | no extra tooling | — (owner 2026-10-08) |
| Client E2E in CI | Open WebUI and agent runtimes against a scripted stub model; CLI clients checked manually with a local harness | model API keys in CI (for now) | no secrets or spend in CI; a CI token with a spend limit may follow, then the harness (#161) runs the CLI checks in CI | — (owner 2026-10-08) |
| Scaling | stateless Streamable HTTP, no sticky sessions; shared state in Valkey if configured, else Postgres | stateful sessions, Valkey mandatory | any replica serves any request; no extra service for small setups | [ADR-0009](./adr/0009-stateless-replicas.md) |
| Load test | k6 in a container, p95 thresholds as CI gate | Locust | single binary with built-in thresholds; JS scripts count as test tooling | — (owner 2026-10-07) |
| Deployment | repo ships its own generic deployment like bring--mcp: Kustomize base in `deploy/` + `deploy/README.md`, Helm chart, Flux example; no operator-specific values (hosts, secrets, cluster names) in this public repo — those live in the operator's own overlay | Helm only; owner-specific manifests in the repo | same pattern as bring--mcp, safe for a public repo ([reference](./research/bring-mcp-reference.md)) | — (owner 2026-10-06) |

Guardrails: few dependencies, OSS first, container by default, application languages from the pool (Go, Rust, C, C++, React, Vue.js) — Python is an owner-approved deviation (ADR-0001).

## Implementation strategy

Risk first, then breadth: the vault and write queue (data safety) come before any network exposure; stdio MCP proves the tool design in Claude Code before auth is involved; search comes before remote so the remote connector is useful from day one; auth is isolated in M4 because it is the part most dependent on claude.ai behaviour.

| Milestone | Goal | Work packages |
|---|---|---|
| M0 | scaffold, accepted ADRs, CI skeleton | WP-01 |
| M1 | vault core | WP-02, WP-03, WP-04 |
| M2 | MCP local (stdio) | WP-05, WP-06 |
| M3 | search + retrieval eval | WP-07, WP-08, WP-09 |
| M4 | remote + auth, verified with claude.ai | WP-10, WP-11 |
| M5 | operations | WP-12, WP-13 |
| M6 | release v0.1.0 | WP-14, WP-15 |
| M7 | shared memory in Postgres on several replicas (F-01) | WP-16 … WP-21 |
| M8 | Entra ID sign-in, asynchronous embeddings (F-01) | WP-22 … WP-24 |
| M9 | data lifecycle and governance (F-01) | WP-25 … WP-28 |
| M10 | enterprise operations (F-01) | WP-29 … WP-31 |
| M11 | proven at target size, release v0.2.0 (F-01) | WP-32 … WP-34 |
| M12 | shared client foundation: profiles, linter, usage rules from one source, personal tokens, `connect`/`doctor` (F-02) | WP-35 … WP-41 |
| M13 | Open WebUI, released on its own (F-02) | WP-42 … WP-46 |
| M14 | IDE and CLI clients (F-02) | WP-47 … WP-51 |
| M15 | autonomous agent runtimes (F-02) | WP-52 … WP-55 |
| M16 | web clients (F-02) | WP-56 … WP-58 |
| M17 | v1.0.0-rc (F-02) | WP-59 … WP-62 |

## Features

| Feature | Benefit | Milestones | Status |
|---|---|---|---|
| [F-01 Enterprise Scale](./features/F-01-enterprise-scale.md) | about 2,000 Entra users with personal, group, project and org memory on a horizontally scaled server | M7–M11 | in progress |
| [F-02 Client Integrations](./features/F-02-client-integrations.md) | one memory from every common AI client (Open WebUI, IDEs, CLIs, agent runtimes, ChatGPT, Gemini), set up with `connect` and checked with `doctor`; ends in v1.0.0-rc | M12–M17 | planned |

## Open decisions

| # | Question | Options | Blocks | Who decides |
|---|---|---|---|---|
| O1 | Language | — decided: Python (ADR-0001) | — | owner ✔ 2026-10-06 |
| O2 | License | — decided: AGPL-3.0-only (ADR-0002) | — | owner ✔ 2026-10-06 |
| O3 | Git access | — decided: git CLI (ADR-0003) | — | owner ✔ 2026-10-06 |
| O4 | Auth model incl. login | — decided: embedded AS, OIDC or admin-password login (ADR-0004) | — | owner ✔ 2026-10-06 |
| O5 | Note format | — decided: as drafted, types `user`/`feedback`/`project`/`reference`/`fact` (ADR-0005) | — | owner ✔ 2026-10-06 |
| O6 | Final project / CLI name | — decided: keep `memory-manager` (package `memory_manager`, CLI `memory-manager`) | — | owner ✔ 2026-10-06 |
| O7 | Track tasks as GitHub issues | — decided: yes, issues are the source of truth | — | owner ✔ 2026-10-06 |
| O8 | Deployment artefacts | — decided: own generic deployment as in bring--mcp, public-safe (see Technology decisions) | — | owner ✔ 2026-10-06 |
| O9 | Enterprise auth with Entra ID | — decided: AS facade, no JWKS check in v1, Graph permissions required, delta sync instead of SCIM (ADR-0006) | — | owner ✔ 2026-10-07 |
| O10 | Storage backend for enterprise mode | — decided: `StorageBackend` with Postgres SoT; CLAUDE.md rules adapted (ADR-0007) | — | owner ✔ 2026-10-07 |
| O11 | Namespace and permission model | — decided: aliases + RLS from membership tables; `memory_promote`, `namespace_kind`, `/account` incl. admin area; break-glass default 2 admins (ADR-0008) | — | owner ✔ 2026-10-07 |
| O12 | Sessions and shared state across replicas | — decided: stateless; Valkey optional with Postgres fallback (ADR-0009) | — | owner ✔ 2026-10-07 |
| O13 | Open WebUI identity | — decided: per-user OAuth for tools, personal token for the filter; trusted headers only later via own ADR (ADR-0011) | — | owner ✔ 2026-10-08 |
| O14 | Client compatibility and profile selection | — decided: one strict tool surface, profiles for delivery, explicit selection (ADR-0010) | — | owner ✔ 2026-10-08 |
| O15 | Personal tokens and `/account` outside enterprise mode | — decided: extend static tokens, `/account` token section whenever the AS runs (ADR-0012) | — | owner ✔ 2026-10-08 |
| O16 | Agent identity and write guard | — decided: agent namespace kind, server-side policy, approval queue (ADR-0013) | — | owner ✔ 2026-10-08 |
| O17 | Native agent memory integration (tier 2) | — decided: tier 1 (MCP) only in v1 (ADR-0014) | — | owner ✔ 2026-10-08 |
| O18 | Clients without DCR/CIMD (Gemini Enterprise) | — decided: operator-registered confidential OAuth clients (ADR-0015) | — | owner ✔ 2026-10-08 |
| O19 | Client support matrix | — decided: draft approved as is ([F-02](./features/F-02-client-integrations.md)) | — | owner ✔ 2026-10-08 |
| O20 | Docs website tooling | — decided: plain Markdown on GitHub, no site generator | — | owner ✔ 2026-10-08 |
| O21 | Model API keys for CLI E2E in CI | — decided: none for now; CLI clients verified manually, agents and Open WebUI against a stub model in CI. Possible later: a CI token with a spend limit for the headless CLI checks | — | owner ✔ 2026-10-08 |

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| claude.ai connector behaviour changes or differs from docs | connector fails to connect | follow bring--mcp pattern; manual verification task #40; research notes dated |
| Spec churn (2026-07-28 removed sessions, deprecated DCR) | rework in auth/transport | rely on SDK dual-version support; CIMD follow-up planned |
| Prompt injection through stored notes | Claude acts on note content | "content is data" in tool descriptions and instructions; no code path executes note content; OWASP LLM review (#51) |
| Secrets written into memory | leak via Git remote | secret scan before commit; rejected with clear message |
| Concurrent human + Claude edits | lost updates | single writer, `if_version`, rebase, conflict files, concurrency test #16 |
| Embedding model change | stale vectors | model + dimension per chunk; automatic reindex |
| AGPL deters some corporate users | fewer adopters/contributors | README explains obligations plainly; unmodified self-hosting has no extra duties |
| Python outside the pool | second stack to maintain | deviation recorded and approved; re-evaluation trigger in ADR-0001 |
