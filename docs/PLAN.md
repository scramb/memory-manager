# PLAN — memory-manager

> **Work backlog: [`docs/TASKS.md`](./TASKS.md) — always read it alongside.**
> This PLAN describes goal, architecture and decisions. What concretely needs doing
> lives exclusively in `docs/TASKS.md`.
> Source of truth for tasks: GitHub issues in `scramb/memory-manager`; `docs/TASKS.md` is the readable mirror.

Last updated: 2026-10-06 · Development rules: [`CLAUDE.md`](../CLAUDE.md)

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
- Hard deletion of notes

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
| Auth | embedded OAuth 2.1 AS, static tokens, upstream OIDC or admin-password login | `mcp` auth provider (ADR-0004) | module |
| CLI | operations | Python entry point | same image |

### Data flow (write)

1. Client calls `memory_edit(path, old_str, new_str, if_version)`.
2. Auth middleware checks token, audience, scope `memory:write`, namespace.
3. Write queue (serial): `git fetch` + fast-forward; compare `if_version` with the current content hash → conflict returns current content + version.
4. Apply edit, validate note (frontmatter, `description`, size), secret scan.
5. `git commit` with client as author, `git push`; on rejection rebase, on rebase failure write `*.conflict.md` and report.
6. Audit log row (who, what, when, commit SHA); indexer is notified and re-indexes the changed file.

### Data model

Note file `<namespace>/<type>/<slug>.md` with frontmatter `id` (ULID), `title`, `description` (required, ≤150 chars), `type`, `tags`, `aliases`, `created`, `updated`, `valid_from`, `valid_to`, `supersedes`, `source`. `[[slug]]` links become rows in `links`. File size cap (16 KB) forces consolidation. Postgres tables: `notes`, `chunks` (text, tsvector, embedding, model, dimension), `links`, `audit_log`, plus auth tables (`oauth_clients`, `tokens`, `static_tokens`). The note format is frozen by ADR-0005 at the start of M1 (O5).

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
| Database | PostgreSQL 16+ with pgvector, plain SQL + versioned migrations | ORM, dedicated vector DB | one well-known service for full text + vectors | — (set by brief) |
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

## Open decisions

| # | Question | Options | Blocks | Who decides |
|---|---|---|---|---|
| O1 | Language | — decided: Python (ADR-0001) | — | owner ✔ 2026-10-06 |
| O2 | License | — decided: AGPL-3.0-only (ADR-0002) | — | owner ✔ 2026-10-06 |
| O3 | Git access | — decided: git CLI (ADR-0003) | — | owner ✔ 2026-10-06 |
| O4 | Auth model incl. login | — decided: embedded AS, OIDC or admin-password login (ADR-0004) | — | owner ✔ 2026-10-06 |
| O5 | Freeze note format (frontmatter fields, path scheme, size cap) as ADR-0005 | as in brief · adjusted | #6 → WP-02 | owner, start of M1 |
| O6 | Final project / CLI name | keep `memory-manager` · new name | #5 (README), package name in #4 | owner |
| O7 | Track tasks as GitHub issues | — decided: yes, issues are the source of truth | — | owner ✔ 2026-10-06 |
| O8 | Deployment artefacts | — decided: own generic deployment as in bring--mcp, public-safe (see Technology decisions) | — | owner ✔ 2026-10-06 |

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
