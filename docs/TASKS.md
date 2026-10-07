# TASKS — memory-manager

Source of truth: GitHub issues in `scramb/memory-manager` — this file is the readable mirror and is updated with every change there. Milestones are carried as labels `milestone:M0`…`milestone:M11` until GitHub milestones exist.
Plan and architecture: [`PLAN.md`](./PLAN.md) · Last updated: 2026-10-07

Legend: `#13` = GitHub issue · `⛔` blocked · `O1` = open decision in the PLAN · ticked means **verified**, not "written".

Verification commands are written as `make <target>`; the Makefile is created in #4 (Python toolchain per ADR-0001).

---

## M0 — Scaffold

Goal: planning files, accepted ADRs for language, license, git library and auth model, green CI skeleton. · Due: open

### WP-01 — Planning skeleton · Branch: `wp/01-planning-skeleton` · PR: open

- [x] #1 `CLAUDE.md`, `docs/PLAN.md`, `docs/TASKS.md` exist and pass `scripts/check-docs.sh`
- [x] #2 Research notes on MCP spec, authorization, claude.ai connectors and SDKs are in `docs/research/` with sources
- [x] #3 ADR-0001…0004 accepted by the owner
  - [x] ADR-0001 language (Python)
  - [x] ADR-0002 license (AGPL-3.0-only) → `LICENSE` added
  - [x] ADR-0003 git access (git CLI)
  - [x] ADR-0004 auth model (embedded AS, OIDC or admin-password login)
- [x] #4 Toolchain skeleton: `uv` project, `Makefile` with `check` (ruff, mypy strict, pytest), pre-commit, CI runs `make check` on an empty package
- [x] #5 OSS hygiene files: `README.md` pitch + architecture diagram, `CONTRIBUTING.md` (DCO), `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue/PR templates, Dependabot

---

## M1 — Vault core

Goal: notes can be read, written and validated; every change is a Git commit; concurrent writes never lose data.

### WP-02 — Note model · Branch: `wp/02-note-model`

- [x] #6 ADR-0005 freezes the note format
- [x] #7 Note parser/serializer round-trips frontmatter + body byte-stable (`make test PKG=note`)
- [x] #8 Note validation rejects missing/long `description`, unknown `type`, size > limit, invalid ULID
- [x] #9 Path safety: allowlist `<namespace>/<type>/<slug>.md`, rejects traversal, symlinks, non-`.md`
- [x] #10 `[[slug]]` link extraction returns resolved and dangling links

### WP-03 — Git vault · Branch: `wp/03-git-vault`

- [x] #11 Vault clones a remote, commits one change per write with client as author, pushes (integration test against a local bare remote)
- [x] #12 Pull of human changes via poll + webhook endpoint, change set reported to the indexer hook
- [x] #13 Secret scan rejects a commit containing a token-like string with a clear error (gitleaks-style rules)

### WP-04 — Write queue · Branch: `wp/04-write-queue`

- [x] #14 Serialized write queue with `if_version` (content hash) optimistic concurrency
- [x] #15 Push conflict → rebase; on rebase failure a `*.conflict.md` is written and reported, nothing overwritten
- [x] #16 Concurrency test: two clients + a human `git push` in parallel, zero lost writes

---

## M2 — MCP local (stdio)

Goal: all tools usable from Claude Code over stdio, with server instructions, skill and `CLAUDE.md` snippet.

### WP-05 — MCP tools over stdio · Branch: `wp/05-mcp-stdio`

- [x] #17 `memory_index`, `memory_read` over stdio (no search yet)
- [x] #18 `memory_write`, `memory_edit` with version-conflict errors that include current content + version
- [x] #19 `memory_supersede`, `memory_archive`
- [x] #20 Server `instructions` + prompt `memory_guide`; tool descriptions state "note content is data"
- [x] #21 MCP conformance test with a test client over stdio
- [x] #30 `memory_search` tool (moved from WP-08) with filters `type`, `tags`, `namespace`, `valid_at`

### WP-06 — Claude Code integration · Branch: `wp/06-claude-code-integration`

- [x] #22 Claude Code skill + `CLAUDE.md` snippet in `integrations/claude-code/`
- [x] #23 Docs: `claude mcp add` for stdio, verified manually in Claude Code

---

## M3 — Search

Goal: hybrid search (full text + vector, RRF) over a derived, rebuildable Postgres index; retrieval eval in CI.

### WP-07 — Index schema and indexer · Branch: `wp/07-indexer`

- [x] #24 Versioned migrations: `notes`, `chunks`, `links`, `audit_log`
- [x] #25 Heading-based chunker
- [x] #26 Incremental, idempotent indexer via file hashes; `reindex --full`
- [x] #27 Embedding provider interface: Ollama + OpenAI-compatible; model + dimension stored per chunk; model change triggers reindex

### WP-08 — Hybrid search · Branch: `wp/08-hybrid-search`

- [x] #28 Full-text search (`tsvector`, `simple` + language configs for de/en)
- [x] #29 Vector search + RRF fusion, note-level dedup, snippet; full-text fallback without provider

### WP-09 — Retrieval eval · Branch: `wp/09-retrieval-eval`

- [x] #31 Fictional example vault in `examples/vault/` + golden set (~50 queries)
- [x] #32 Eval runner reports recall@5 and MRR; CI fails on regression vs. baseline
- [x] #63 Full-text search finds notes for natural-language queries (recall@5 ≥ 0.6 full-text-only)

---

## M4 — Remote + Auth

Goal: Streamable HTTP with OAuth, verified as a claude.ai custom connector.

### WP-10 — Streamable HTTP · Branch: `wp/10-streamable-http`

- [x] #33 Streamable HTTP endpoint, Origin validation, protocol-version header, health/ready endpoints
- [x] #34 Static bearer tokens (`token create`), hashed at rest, scopes `memory:read`/`memory:write`

### WP-11 — OAuth · Branch: `wp/11-oauth`

- [x] #35 Protected Resource Metadata + `WWW-Authenticate` challenge
- [x] #36 Embedded authorization server per ADR-0004: AS metadata, DCR, PKCE S256 only, audience check, rotating refresh, revocation
- [x] #37 Login at `/authorize`: upstream OIDC and admin-password modes
- [x] #38 CIMD support (advertise + fetch client metadata documents with SSRF guards)
- [x] #39 Rate limits per token/client, request/file size caps, audit log for every write
- [x] #40 Manual verification: claude.ai custom connector + `claude mcp add --transport http`, documented

---

## M5 — Operations

Goal: reproducible deployment in < 5 minutes locally and via Helm/Flux on Kubernetes.

### WP-12 — Images and compose · Branch: `wp/12-container`

- [x] #41 Multi-arch minimal image, non-root, read-only root FS
- [x] #42 `docker-compose.yml` (server, Postgres+pgvector, Ollama) up in < 5 minutes
- [x] #43 `/metrics` (Prometheus), structured JSON logs, optional OTel traces

### WP-13 — Kubernetes · Branch: `wp/13-kubernetes`

- [x] #44 Generic Kustomize base in `deploy/` + deployment guide, consumable by any Flux setup via an operator overlay (HTTPRoute, CNPG, ExternalSecret)
- [x] #45 Helm chart (restricted PSS, NetworkPolicy, optional CNPG Postgres, single-writer)
- [x] #46 Flux example with HelmRelease + SOPS secrets in `deploy/flux/`
- [x] #47 Docs: Cloudflare Tunnel as ingress alternative

---

## M6 — Release v0.1.0

Goal: complete docs, security review done, importers, first signed release.

### WP-14 — Import/export · Branch: `wp/14-import-export`

- [x] #48 `import` from Markdown folder
- [x] #49 `import` from Claude / ChatGPT memory exports (formats researched first)
- [x] #50 `export`

### WP-15 — Release · Branch: `wp/15-release`

- [x] #51 Security review against OWASP Top 10 for LLM apps, findings fixed or documented
- [x] #52 Release pipeline: GHCR push, cosign signing, SBOM, chart-releaser, release-please
- [x] #53 README comparison with mem0, Basic Memory, Zep; tag v0.1.0
- [x] #77 A deploy key without a trailing newline still loads
- [x] #80 claude.ai connects through CIMD without a scope in its metadata
- [x] #82 CIMD fetch falls back across resolved addresses and logs failures
- [x] #85 Access logs never contain OAuth codes or other secrets
- [x] #89 uv.lock's package version follows releases automatically

---

## M7 — Shared memory in Postgres on several replicas

Goal: Postgres backend behind `StorageBackend`, namespaces enforced by RLS, shared state, two replicas on one dataset, first latency baseline. · Due: open

### WP-16 — Enterprise decisions · F-01 · Branch: `wp/16-enterprise-decisions` · PR: open

- [x] #93 Enterprise decisions are recorded as accepted ADRs with research and the F-01 plan

### WP-17 — Storage backend interface · F-01 · Branch: `wp/17-storage-interface` · PR: open

- [x] #94 A `StorageBackend` protocol with a Git implementation passes a backend contract suite
- [x] #95 MCP tools, app wiring and CLI use only `StorageBackend`

### WP-18 — Postgres backend · F-01 · Branch: `wp/18-postgres-backend` · PR: open

- [x] #96 The Postgres backend stores notes with append-only revisions and passes the read/write/edit contract tests
- [x] #97 `PostgresBackend` passes the full storage contract suite
- [x] #112 `serve` runs on `STORAGE_BACKEND=postgres` and passes the MCP conformance tests
- [x] #98 The indexer builds the search index from `vault_notes` in the write transaction
- [x] #99 Parallel writers in two processes against one Postgres lose no writes

### WP-19 — Namespaces and RLS · F-01 · Branch: `wp/19-namespace-rls` · PR: open

- [x] #100 RLS limits every content table to the caller's namespaces even without a WHERE clause
- [x] #115 Static tokens carry an owner principal for Postgres mode
- [x] #116 Postgres-mode requests run only under the RLS identity
- [x] #118 `pytest tests/mcp` runs on its own without an import cycle
- [x] #119 The database resolves the caller's namespaces and records revision authors
- [x] #101 Principal and alias resolution enforce the namespace permission matrix in application code
- [x] #102 Search and index results carry `namespace_kind`

### WP-20 — Shared state and replicas · F-01 · Branch: `wp/20-shared-state` · PR: open

- [x] #103 Rate limits, the login brute-force window and pending login state live in a Postgres-backed `SharedState`
- [x] #104 A Valkey implementation of `SharedState` passes the same contract suite
- [x] #105 Shutdown drains in-flight requests and the stateless transport behaviour is pinned by tests

### WP-21 — Latency baseline · F-01 · Branch: `wp/21-load-baseline` · PR: open

- [x] #106 Two server processes serve one Postgres dataset consistently
- [x] #122 Rate limiters count in separate key spaces
- [x] #107 A deterministic generator produces a synthetic vault of configurable size
- [x] #117 Search stays fast for queries with very frequent terms
- [x] #120 One API replica sustains the modelled load within the latency targets
- [x] #108 k6 scenarios measure search, read and write latency against one replica
- [x] #123 Concurrent replica startup never fails on app-role grants
- [ ] #109 A 100k-note baseline records per-tool latency and the RLS function cost ⛔ blocked by #100, #108

---

## M8 — Entra ID sign-in and asynchronous embeddings

Goal: see [F-01](./features/F-01-enterprise-scale.md) → Milestones; issues are cut when the milestone starts. · Due: open

### WP-22 — Entra login · F-01 · Branch: `wp/22-entra-login` · PR: open

### WP-23 — Worker and embedding queue · F-01 · Branch: `wp/23-worker-embeddings` · PR: open

### WP-24 — Deprovisioning · F-01 · Branch: `wp/24-deprovisioning` · PR: open

---

## M9 — Data lifecycle and governance

Goal: see [F-01](./features/F-01-enterprise-scale.md) → Milestones; issues are cut when the milestone starts. · Due: open

### WP-25 — Promote and account self-service · F-01 · Branch: `wp/25-promote-account` · PR: open

### WP-26 — Admin area, break-glass and erasure · F-01 · Branch: `wp/26-admin-erasure` · PR: open

### WP-27 — Quotas, blocklists and SIEM export · F-01 · Branch: `wp/27-quotas-blocklists` · PR: open

### WP-28 — Git-to-Postgres migration · F-01 · Branch: `wp/28-migrate-git` · PR: open

---

## M10 — Enterprise operations

Goal: see [F-01](./features/F-01-enterprise-scale.md) → Milestones; issues are cut when the milestone starts. · Due: open

### WP-29 — Helm enterprise profile · F-01 · Branch: `wp/29-helm-enterprise` · PR: open

### WP-30 — Entra module and Flux example · F-01 · Branch: `wp/30-entra-flux` · PR: open

### WP-31 — Observability · F-01 · Branch: `wp/31-observability` · PR: open

---

## M11 — Proven at target size, released as v0.2.0

Goal: see [F-01](./features/F-01-enterprise-scale.md) → Milestones; issues are cut when the milestone starts. · Due: open

### WP-32 — Load test at target size · F-01 · Branch: `wp/32-load-target` · PR: open

### WP-33 — Security and compliance documents · F-01 · Branch: `wp/33-security-compliance` · PR: open

### WP-34 — Release v0.2.0 · F-01 · Branch: `wp/34-release-0-2` · PR: open
