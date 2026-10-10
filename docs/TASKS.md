# TASKS — memory-manager

Source of truth: GitHub issues in `scramb/memory-manager` — this file is the readable mirror and is updated with every change there. Milestones are carried as labels `milestone:M0`…`milestone:M17` until GitHub milestones exist.
Plan and architecture: [`PLAN.md`](./PLAN.md) · Last updated: 2026-10-08

Legend: `#13` = GitHub issue · `⛔` blocked · `O1` = open decision in the PLAN · ticked means **verified**, not "written".

Verification commands are written as `make <target>`; the Makefile is created in #4 (Python toolchain per ADR-0001).

---

## M0 — Scaffold

Goal: planning files, accepted ADRs for language, license, git library and auth model, green CI skeleton. · Due: open

### WP-01 — Planning skeleton · Branch: `wp/01-planning-skeleton` · PR: #55

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

### WP-16 — Enterprise decisions · F-01 · Branch: `wp/16-enterprise-decisions` · PR: #110

- [x] #93 Enterprise decisions are recorded as accepted ADRs with research and the F-01 plan

### WP-17 — Storage backend interface · F-01 · Branch: `wp/17-storage-interface` · PR: #111

- [x] #94 A `StorageBackend` protocol with a Git implementation passes a backend contract suite
- [x] #95 MCP tools, app wiring and CLI use only `StorageBackend`

### WP-18 — Postgres backend · F-01 · Branch: `wp/18-postgres-backend` · PR: #114

- [x] #96 The Postgres backend stores notes with append-only revisions and passes the read/write/edit contract tests
- [x] #97 `PostgresBackend` passes the full storage contract suite
- [x] #112 `serve` runs on `STORAGE_BACKEND=postgres` and passes the MCP conformance tests
- [x] #98 The indexer builds the search index from `vault_notes` in the write transaction
- [x] #99 Parallel writers in two processes against one Postgres lose no writes

### WP-19 — Namespaces and RLS · F-01 · Branch: `wp/19-namespace-rls` · PR: #121

- [x] #100 RLS limits every content table to the caller's namespaces even without a WHERE clause
- [x] #115 Static tokens carry an owner principal for Postgres mode
- [x] #116 Postgres-mode requests run only under the RLS identity
- [x] #118 `pytest tests/mcp` runs on its own without an import cycle
- [x] #119 The database resolves the caller's namespaces and records revision authors
- [x] #101 Principal and alias resolution enforce the namespace permission matrix in application code
- [x] #102 Search and index results carry `namespace_kind`

### WP-20 — Shared state and replicas · F-01 · Branch: `wp/20-shared-state` · PR: #113

- [x] #103 Rate limits, the login brute-force window and pending login state live in a Postgres-backed `SharedState`
- [x] #104 A Valkey implementation of `SharedState` passes the same contract suite
- [x] #105 Shutdown drains in-flight requests and the stateless transport behaviour is pinned by tests

### WP-21 — Latency baseline · F-01 · Branch: `wp/21-load-baseline` · PR: #139

- [x] #106 Two server processes serve one Postgres dataset consistently
- [x] #122 Rate limiters count in separate key spaces
- [x] #107 A deterministic generator produces a synthetic vault of configurable size
- [x] #117 Search stays fast for queries with very frequent terms
- [x] #120 One API replica sustains the modelled load within the latency targets
- [x] #108 k6 scenarios measure search, read and write latency against one replica
- [x] #123 Concurrent replica startup never fails on app-role grants
- [x] #124 The k6 smoke runs under RLS with registered principals and stays green
- [x] #109 A 100k-note baseline records per-tool latency and the RLS function cost

---

## M8 — Entra ID sign-in and asynchronous embeddings

Goal: Entra sign-in through the facade with roles and groups, a worker with an embedding queue, and deprovisioning through the Graph delta sync. · Due: open

### WP-22 — Entra login · F-01 · Branch: `wp/22-entra-login` · PR: #284

- [x] #212 Mock Entra IdP (OIDC + Graph) under tests/mock_idp/
- [x] #213 Codes/tokens bound to `users`; verifier emits oid/roles/groups
- [x] #214 Graph client: app token, getMemberGroups, user state ⛔ blocked by #212
- [x] #215 `LOGIN_MODE=entra` with tenant allowlist, roles, groups incl. overage ⛔ blocked by #213, #214
- [x] #216 Refresh re-check via Graph, 15-min access tokens, `ENTRA_MAX_SESSION` ⛔ blocked by #215

### WP-23 — Worker and embedding queue · F-01 · Branch: `wp/23-worker-embeddings` · PR: #287

- [x] #125 Vector-index spike → `docs/research/vector-index.md` + ADR-0016 (Proposed)
- [x] #217 `memory-manager worker` with health/ready/metrics port; singleton jobs; OAuth cleanup moved from api
- [x] #218 `jobs` outbox: enqueue in write tx, `SKIP LOCKED`, `LISTEN/NOTIFY` + poll ⛔ blocked by #217
- [x] #219 Asynchronous embeddings via `jobs` in Postgres mode ⛔ blocked by #218
- [x] #220 Chunks schema per ADR-0016; `EMBEDDING_DIMENSIONS` pinned at first migrate ⛔ blocked by #125, ADR-0016 accepted (spike #125), #219
- [x] #221 Per-kind vector search with ADR-0016 HNSW settings, RRF-fused ⛔ blocked by #220

### WP-24 — Deprovisioning · F-01 · Branch: `wp/24-deprovisioning` · PR: #290

- [x] #222 `disable_user`: revoke all token families + owned static tokens ⛔ blocked by #213, #216
- [x] #223 Graph users delta sync job in the worker ⛔ blocked by #222, #217, #214
- [x] #224 Static tokens in enterprise mode: mandatory expiry/scopes/owner
- [x] #225 M8 acceptance e2e with the mock IdP ⛔ blocked by #223, #219, #216

---

## M9 — Data lifecycle and governance

Goal: `memory_promote`, `/account` self-service and admin area, break-glass, erasure and retention, quotas, blocklists, SIEM export and `migrate git-to-postgres`. · Due: open

### WP-25 — Promote and account self-service · F-01 · Branch: `wp/25-promote-account` · PR: #289

- [x] #226 Both storage backends promote a note as a new superseding note
- [x] #227 Claude promotes a personal note into a shared namespace with `memory_promote` ⛔ blocked by #226
- [x] #228 `/account` browser sessions are stored as hashes with idle and absolute expiry
- [x] #229 Signed-in users reach the `/account` page in every embedded login mode ⛔ blocked by #228, #215
- [x] #230 Users download their personal memory as a Markdown ZIP from `/account` ⛔ blocked by #229

### WP-26 — Admin area, break-glass and erasure · F-01 · Branch: `wp/26-admin-erasure` · PR: #294

- [x] #231 Erasure hard-deletes a note, a namespace or a user's memory and pseudonymizes what stays ⛔ blocked by #219, #216
- [x] #232 Users erase their own personal memory after typing a confirmation ⛔ blocked by #229, #231
- [x] #233 After a restore the server replays the erasure log from `ERASURE_LOG_REPLAY_FILE` before `/readyz` turns 200 ⛔ blocked by #231, #245
- [x] #234 Admins manage namespaces, project members and namespace settings on `/account` ⛔ blocked by #229
- [x] #235 Admins revoke all sessions and tokens of a user immediately on `/account` ⛔ blocked by #234, #222
- [x] #236 Admins erase a note, a namespace or a user on `/account` with a recorded reason ⛔ blocked by #231, #234
- [x] #237 Break-glass grants need a second admin's approval and expire after one hour ⛔ blocked by #234
- [x] #238 Admins read a break-glass namespace in a read-only, audited viewer on `/account` ⛔ blocked by #237
- [x] #239 Users see a break-glass banner and a reference note in `me` until they acknowledge it ⛔ blocked by #237
- [x] #240 Personal memories of deprovisioned users are erased after `PERSONAL_RETENTION_DAYS` ⛔ blocked by #231, #219, #223
- [x] #241 After a user is deleted no content of theirs remains and their shared traces are pseudonymized ⛔ blocked by #236, #240

### WP-27 — Quotas, blocklists and SIEM export · F-01 · Branch: `wp/27-quotas-blocklists` · PR: #286

- [x] #242 Write rate quotas per user, namespace and token hold across replicas
- [x] #243 Note count and size quotas reject writes that would exceed a namespace's limit ⛔ blocked by #242
- [x] #244 Writes matching an operator blocklist category are rejected and audited without content ⛔ blocked by #226
- [x] #245 Every audit record is exported to stdout or OTLP for a SIEM

### WP-28 — Git-to-Postgres migration · F-01 · Branch: `wp/28-migrate-git` · PR: #285

- [x] #246 `migrate git-to-postgres --dry-run` reports the namespace mapping and every note it would import
- [x] #247 `migrate git-to-postgres` imports current notes byte-identically with Git history as revisions ⛔ blocked by #246
- [x] #248 The example vault migrates to Postgres with identical versions ⛔ blocked by #247
- [x] #283 `export` writes a Postgres-backed deployment to the Git vault archive format

---

## M10 — Enterprise operations

Goal: Helm enterprise profile, Entra OpenTofu module, Flux example proven on kind, and observability. · Due: open

### WP-29 — Helm enterprise profile · F-01 · Branch: `wp/29-helm-enterprise` · PR: #282

- [x] #249 The chart refuses more than one replica or an autoscaler unless storage.backend is postgres
- [x] #250 The chart renders separate api and worker Deployments with graceful shutdown for the postgres backend ⛔ blocked by #249, #219
- [x] #251 The api and worker Deployments scale on CPU, with optional RPS scaling through a KEDA ScaledObject ⛔ blocked by #250
- [x] #252 The CNPG cluster of the enterprise profile runs three instances with Barman Cloud plugin backups ⛔ blocked by #250
- [x] #253 The container image ships the valkey and otel extras
- [x] #254 The chart can deploy an optional Valkey without persistence for shared state ⛔ blocked by #250, #253
- [x] #255 NetworkPolicies limit api, worker, Valkey and Postgres traffic to the needed flows ⛔ blocked by #251, #252, #254

### WP-30 — Entra module and Flux example · F-01 · Branch: `wp/30-entra-flux` · PR: #288

- [x] #256 An OpenTofu module under deploy/entra creates the Entra app registration for the auth facade ⛔ blocked by #216
- [x] #257 A Flux enterprise example deploys the chart with the enterprise profile ⛔ blocked by #255, #216
- [x] #258 A kind E2E workflow rolls out the Flux enterprise example and gets /readyz 200 from three api replicas ⛔ blocked by #257, #216, #219
- [x] #259 An operator guide explains how to run the enterprise profile end to end ⛔ blocked by #256, #257, #258

### WP-31 — Observability · F-01 · Branch: `wp/31-observability` · PR: #292

- [x] #260 Every rate-limit and quota rejection is counted per limiter in /metrics ⛔ blocked by #242, #243
- [x] #261 Job queue length and embedding lag are exported as Prometheus gauges ⛔ blocked by #219
- [x] #262 OpenTelemetry traces cover an api request from the HTTP edge down to its database statements
- [x] #263 Worker jobs appear in the trace of the request that enqueued them ⛔ blocked by #262, #219
- [x] #264 The ServiceMonitor scrapes api and worker metrics separately ⛔ blocked by #250, #219
- [x] #265 A Grafana dashboard and PrometheusRule alerts cover latency budgets, errors, rate limits and embedding lag ⛔ blocked by #260, #261, #264

---

## M11 — Proven at target size, released as v0.2.0

Goal: Target-size load test incl. replica failure, threat model and compliance templates, upgrade guide and release v0.2.0. · Due: open

### WP-32 — Load test at target size · F-01 · Branch: `wp/32-load-target` · PR: #298

- [x] #266 Generator produces a 1M-note / ~5M-chunk vault with deterministic synthetic vectors ⛔ blocked by #221, ADR-0016 accepted (spike #125)
- [x] #267 Loader writes chunks with synthetic vectors and builds the ADR-0016 HNSW index ⛔ blocked by #266, #221
- [x] #268 Embedding stub answers query embeddings deterministically for load tests ⛔ blocked by #266
- [x] #269 Local load test runs 3 replicas with a replica kill on Postgres or Valkey shared state ⛔ blocked by #267, #268
- [x] #291 Vector-only searches meet the F-01 search latency budget ⛔ blocked by #269
- [x] #293 Vector-only search finds its target on the Helm deployment as on a single process ⛔ blocked by #291
- [x] #270 Generic Kubernetes runner executes the load test against the enterprise profile ⛔ blocked by #269, #255, #253, #257
- [x] #296 Unfiltered vector searches on the user partition use the namespace B-tree instead of scanning every chunk
- [x] #271 The target-size benchmark report shows F-01's targets met on both shared-state implementations ⛔ blocked by #270, #263, #265

### WP-33 — Security and compliance documents · F-01 · Branch: `wp/33-security-compliance` · PR: #295

- [x] #272 STRIDE threat model covers every trust boundary of the enterprise deployment ⛔ blocked by #232, #227, #230, #241, #233, #238, #239, #235, #243, #244, #245, #255
- [x] #273 Pen-test checklist turns the threat model into executable test cases ⛔ blocked by #272
- [x] #274 Compliance templates for data flow, records of processing and TOMs ⛔ blocked by #272, #263, #265
- [x] #275 Compliance templates for the deletion concept with backup horizon and roles and permissions ⛔ blocked by #274, #241, #233, #238, #239, #235, #243, #244, #245, #252
- [x] #276 Compliance templates for DPIA, employee transparency notice and Germany section ⛔ blocked by #274, #275

### WP-34 — Release v0.2.0 · F-01 · Branch: `wp/34-release-0-2` · PR: #299

- [x] #277 Upgrade smoke test proves 0.1.x → 0.2.0 incl. migration and export rollback ⛔ blocked by #248
- [x] #278 Upgrade guide 0.1.x → 0.2.0 incl. git-to-postgres migration and export rollback ⛔ blocked by #277, #259
- [x] #279 README enterprise section with links to guides, benchmark and compliance ⛔ blocked by #278, #271, #276
- [x] #280 v0.2.0 released with cosign-verified image and Helm chart ⛔ blocked by #224, #225, #273, #279

---

## M12 — Shared client foundation

Goal: compatibility profiles, schema linter, usage rules from one source, personal tokens, per-profile conformance suite, `connect`/`doctor --client` for Claude Code — see [F-02](./features/F-02-client-integrations.md). · Due: open

### WP-35 — Client integration decisions · F-02 · Branch: `wp/35-client-decisions` · PR: open

- [x] #126 Client integration decisions are recorded as accepted ADRs with research and the F-02 plan
- [x] #159 The client support matrix is approved by the owner

### WP-36 — Usage rules from one source · F-02 · Branch: `wp/36-memory-guide` · PR: #307

- [x] #127 `docs/memory-guide.md` is the single source of the server instructions and the `memory_guide` prompt
- [x] #128 `instructions generate` writes a short form and per-client instruction files, and CI rejects stale ones

### WP-37 — Client docs skeleton · F-02 · Branch: `wp/37-client-docs` · PR: #304

- [x] #129 `docs/clients` has a page template, the approved support matrix and pages for claude.ai and Claude Code

### WP-38 — Compatibility profiles · F-02 · Branch: `wp/38-compat-profiles` · PR: open

- [ ] #130 Compatibility profiles for default, claude.ai and Claude Code exist as data in `compat/`
- [ ] #131 Each MCP request runs under the profile chosen by override, clientInfo or default ⛔ blocked by #130
- [ ] #132 Every tool carries MCP annotations and the core usage rules in its description
- [ ] #133 A schema linter fails CI when a tool violates a supported profile's limits ⛔ blocked by #130

### WP-39 — Personal tokens · F-02 · Branch: `wp/39-personal-tokens` · PR: open

- [ ] #134 Personal tokens carry a kind and are bounded by their owner's rights
- [ ] #135 Users create, list and revoke their own personal tokens on `/account` ⛔ blocked by #134, #229

### WP-40 — Conformance suite per profile · F-02 · Branch: `wp/40-conformance-profiles` · PR: open

- [ ] #136 The conformance suite runs the full tool set and its error cases once per profile ⛔ blocked by #131, #132

### WP-41 — connect and doctor · F-02 · Branch: `wp/41-connect-doctor` · PR: open

- [ ] #137 `connect claude-code` merges the server into Claude Code's config, and `connect claude-ai` prints the setup steps
- [ ] #138 `doctor --client` proves reachability, auth, profile and a write round trip in a test namespace ⛔ blocked by #131, #137

---

## M13 — Open WebUI, released on its own

Goal: see [F-02](./features/F-02-client-integrations.md) → Milestones. · Due: open

### WP-42 — Open WebUI spike and per-user auth · F-02 · Branch: `wp/42-openwebui-auth` · PR: open

- [ ] #141 A pinned Open WebUI stack runs in Compose with memory-manager, Postgres and Ollama
- [ ] #142 Open WebUI's OAuth client, UserValves storage and filter token access are verified against the pinned stack ⛔ blocked by #141
- [ ] #143 Open WebUI registers via DCR and each user completes OAuth against our authorization server ⛔ blocked by #142
- [ ] #144 Two Open WebUI users write and read only their own memory over native MCP ⛔ blocked by #143

### WP-43 — Open WebUI profile · F-02 · Branch: `wp/43-openwebui-profile` · PR: open

- [ ] #145 The openwebui profile exists and passes the conformance suite ⛔ blocked by #136, #142
- [ ] #146 Open WebUI tool calls return the same results as the conformance suite ⛔ blocked by #144, #145

### WP-44 — Open WebUI filter · F-02 · Branch: `wp/44-openwebui-filter` · PR: open

- [ ] #147 The Open WebUI filter injects relevant notes as a marked data block within a token budget ⛔ blocked by #128, #142, #134
- [ ] #148 The filter outlet saves or archives a note only on an explicit remember or forget request ⛔ blocked by #147
- [ ] #149 `tool_memory.py` and `system_prompt.md` cover Open WebUI setups without native MCP ⛔ blocked by #128, #142
- [ ] #150 The filter injects relevant notes and stays within its budget against the pinned stack ⛔ blocked by #144, #147
- [ ] #151 A 7-8B local model gives useful memory answers in filter mode in a nightly check ⛔ blocked by #150

### WP-45 — Open WebUI memory import · F-02 · Branch: `wp/45-openwebui-import` · PR: open

- [ ] #152 `memory-manager import openwebui` imports a user's built-in Open WebUI memories ⛔ blocked by #142
- [ ] #153 The docs explain how to disable or fence Open WebUI's built-in memory ⛔ blocked by #142

### WP-46 — Open WebUI deployment, docs and release · F-02 · Branch: `wp/46-openwebui-release` · PR: open

- [ ] #154 A Helm values example runs memory-manager next to the Open WebUI chart with a NetworkPolicy ⛔ blocked by #143
- [ ] #155 Open WebUI docs let an admin connect it in under 15 minutes ⛔ blocked by #148, #152, #154, #129
- [ ] #156 CI runs the Open WebUI E2E against the pinned version and the two previous minors ⛔ blocked by #146, #150
- [ ] #157 Open WebUI works with Entra sign-in through the auth facade ⛔ blocked by #144, #216 (F-01 WP-22)
- [ ] #158 A release with Open WebUI support is published ⛔ blocked by #155, #156

---

## M14 — IDE and CLI clients

Goal: see [F-02](./features/F-02-client-integrations.md) → Milestones. · Due: open

### WP-47 — Headless CLI harness and Codex · F-02 · Branch: `wp/47-headless-cli` · PR: open

- [ ] #161 A local headless harness runs client CLIs against a test instance ⛔ blocked by #136
- [ ] #162 Claude Code is verified headless with the harness and recorded in the checklist ⛔ blocked by #161, #137, #160
- [ ] #163 Codex has a profile, integration files and connect and doctor support ⛔ blocked by #133, #137, #138
- [ ] #164 Codex CLI is verified headless with the harness and recorded in the checklist ⛔ blocked by #161, #163, #160

### WP-48 — Gemini CLI and Code Assist · F-02 · Branch: `wp/48-gemini-cli` · PR: open

- [ ] #165 The authorization server returns `iss` in the authorization response when it advertises it
- [ ] #166 Gemini CLI has a profile, integration files and connect and doctor support ⛔ blocked by #165, #133, #137, #138
- [ ] #167 Gemini CLI is verified headless with the harness and recorded in the checklist ⛔ blocked by #161, #166, #160
- [ ] #168 Gemini Code Assist setup is documented and checked manually ⛔ blocked by #166, #160

### WP-49 — Cursor · F-02 · Branch: `wp/49-cursor` · PR: open

- [ ] #169 Cursor has a profile, integration files and connect and doctor support ⛔ blocked by #133, #137, #138
- [ ] #170 Cursor's handling of a static token next to OAuth discovery is verified and documented ⛔ blocked by #169
- [ ] #171 Cursor Teams rollout is documented ⛔ blocked by #169, #160

### WP-50 — GitHub Copilot · F-02 · Branch: `wp/50-copilot` · PR: open

- [ ] #172 GitHub Copilot in VS Code and Copilot CLI has a profile, integration files and connect and doctor support ⛔ blocked by #133, #137, #138
- [ ] #173 Copilot coding agent and JetBrains use memory-manager with a personal token ⛔ blocked by #172, #134, #160
- [ ] #174 Copilot Business and Enterprise MCP policy rollout is documented ⛔ blocked by #172

### WP-51 — Google Antigravity · F-02 · Branch: `wp/51-antigravity` · PR: open

- [ ] #175 Antigravity has a profile, integration files and connect and doctor support ⛔ blocked by #133, #137, #138, #160

---

## M15 — Autonomous agent runtimes

Goal: see [F-02](./features/F-02-client-integrations.md) → Milestones. · Due: open

### WP-52 — Agent identity and write guard · F-02 · Branch: `wp/52-agent-identity` · PR: open

- [ ] #176 Agent tokens and the agent namespace kind exist on both storage backends ⛔ blocked by #134
- [ ] #177 The agent write policy is enforced on the server ⛔ blocked by #176
- [ ] #178 Pending agent writes wait for owner approval through the CLI ⛔ blocked by #177
- [ ] #179 Owners approve agent writes on `/account` ⛔ blocked by #178, #229 (F-01 WP-25)
- [ ] #180 Delegation grants let an agent read or write its owner's namespace only when granted ⛔ blocked by #176
- [ ] #181 Audit log and metrics name the agent and the triggering channel ⛔ blocked by #176
- [ ] #182 Per-agent quotas limit requests and writes ⛔ blocked by #176, #242 (F-01 WP-27)
- [ ] #183 A third party's remember request never reaches the owner's namespace ⛔ blocked by #177, #180, #178

### WP-53 — Hermes Agent · F-02 · Branch: `wp/53-hermes` · PR: open

- [ ] #184 Hermes Agent has a profile, config, skill and connect and doctor support ⛔ blocked by #176, #132, #137, #138
- [ ] #185 `memory-manager import hermes` imports Hermes memory files ⛔ blocked by #176
- [ ] #186 Hermes runs headless against memory-manager in a Compose E2E with a pinned version ⛔ blocked by #184, #183

### WP-54 — OpenClaw · F-02 · Branch: `wp/54-openclaw` · PR: open

- [ ] #187 OpenClaw has a profile, config, skill and connect and doctor support ⛔ blocked by #176, #137, #138
- [ ] #188 `memory-manager import openclaw` imports OpenClaw memory files ⛔ blocked by #176
- [ ] #189 OpenClaw runs headless against memory-manager in a Compose E2E with a pinned version ⛔ blocked by #187, #183

### WP-55 — Native agent memory integration · F-02 · Branch: `wp/55-agent-native` · PR: open

- [x] #190 The decision on native agent memory integration is recorded

Deferred past 1.0 by ADR-0014: #191 (Hermes provider), #192 (OpenClaw plugin), both closed as not planned.

---

## M16 — Web clients

Goal: see [F-02](./features/F-02-client-integrations.md) → Milestones. · Due: open

### WP-56 — ChatGPT · F-02 · Branch: `wp/56-chatgpt` · PR: open

- [ ] #194 ChatGPT has a profile, a docs page and connect steps ⛔ blocked by #132, #137, #160
- [ ] #195 ChatGPT Business and Enterprise rollout is documented ⛔ blocked by #194

### WP-57 — Gemini Enterprise · F-02 · Branch: `wp/57-gemini-enterprise` · PR: open

- [x] #196 The decision on pre-registered OAuth clients for Gemini Enterprise is recorded
- [ ] #197 Operators can register confidential OAuth clients in the authorization server
- [ ] #198 Gemini Enterprise is documented within its limits and the consumer Gemini app is documented as not possible ⛔ blocked by #160

### WP-58 — Manual client checklist · F-02 · Branch: `wp/58-client-checklist` · PR: open

- [ ] #160 `docs/release/client-checklist.md` lists the manual acceptance steps for every GUI and web client
- [ ] #193 The client checklist is part of the release process ⛔ blocked by #160

---

## M17 — v1.0.0-rc

Goal: see [F-02](./features/F-02-client-integrations.md) → Milestones. · Due: open

### WP-59 — Stable API and compatibility policy · F-02 · Branch: `wp/59-stable-api` · PR: open

- [ ] #199 `docs/compatibility.md` defines the SemVer and deprecation policy
- [ ] #200 The tool contract, config format, note format and CLI are documented as stable reference ⛔ blocked by #199
- [ ] #201 A contract snapshot test fails on breaking changes to the tool contract ⛔ blocked by #199
- [ ] #202 An upgrade guide from the last 0.x release exists and its migrations are tested ⛔ blocked by #199, #280 (F-01 WP-34)

### WP-60 — Security review for the release candidate · F-02 · Branch: `wp/60-security-rc` · PR: open

- [ ] #203 The threat model covers Open WebUI identity, personal tokens and agents with third-party input ⛔ blocked by #272 (F-01 WP-33), #143, #183, #134
- [ ] #204 The release candidate has no open High or Critical security findings ⛔ blocked by #203

### WP-61 — Documentation website · F-02 · Branch: `wp/61-docs-site` · PR: open

- [x] #205 The decision on docs website tooling is recorded
- [ ] #206 `docs/README.md` is a navigable index with quickstart, client pages, support matrix, operations and enterprise ⛔ blocked by #200, #155
- [ ] #207 The README lists supported clients as text with links and no logos

### WP-62 — Release v1.0.0-rc.1 · F-02 · Branch: `wp/62-release-rc` · PR: open

- [ ] #208 Every feature in TASKS is done or deferred past 1.0 with a reason ⛔ blocked by #158, #204, #202
- [ ] #209 `v1.0.0-rc.1` is released with notes, a signed image and the Helm chart ⛔ blocked by #208, #193, #206, #207, #201, #280 (F-01 WP-34)
