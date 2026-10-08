# TASKS — memory-manager

Source of truth: GitHub issues in `scramb/memory-manager` — this file is the readable mirror and is updated with every change there. Milestones are carried as labels `milestone:M0`…`milestone:M17` until GitHub milestones exist.
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
- [x] #124 The k6 smoke runs under RLS with registered principals and stays green
- [x] #109 A 100k-note baseline records per-tool latency and the RLS function cost

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

---

## M12 — Shared client foundation

Goal: compatibility profiles, schema linter, usage rules from one source, personal tokens, per-profile conformance suite, `connect`/`doctor --client` for Claude Code — see [F-02](./features/F-02-client-integrations.md). · Due: open

### WP-35 — Client integration decisions · F-02 · Branch: `wp/35-client-decisions` · PR: open

- [ ] #126 Client integration decisions are recorded as accepted ADRs with research and the F-02 plan ⛔ blocked by O13–O16 (owner)
- [ ] #159 The client support matrix is approved by the owner ⛔ blocked by #129

### WP-36 — Usage rules from one source · F-02 · Branch: `wp/36-memory-guide` · PR: open

- [ ] #127 `docs/memory-guide.md` is the single source of the server instructions and the `memory_guide` prompt
- [ ] #128 `instructions generate` writes a short form and per-client instruction files, and CI rejects stale ones ⛔ blocked by #127

### WP-37 — Client docs skeleton · F-02 · Branch: `wp/37-client-docs` · PR: open

- [ ] #129 `docs/clients` has a page template, a support-matrix skeleton and pages for claude.ai and Claude Code

### WP-38 — Compatibility profiles · F-02 · Branch: `wp/38-compat-profiles` · PR: open

- [ ] #130 Compatibility profiles for default, claude.ai and Claude Code exist as data in `compat/` ⛔ blocked by #126
- [ ] #131 Each MCP request runs under the profile chosen by override, clientInfo or default ⛔ blocked by #130
- [ ] #132 Every tool carries MCP annotations and the core usage rules in its description ⛔ blocked by #126, #127
- [ ] #133 A schema linter fails CI when a tool violates a supported profile's limits ⛔ blocked by #130

### WP-39 — Personal tokens · F-02 · Branch: `wp/39-personal-tokens` · PR: open

- [ ] #134 Personal tokens carry a kind and are bounded by their owner's rights ⛔ blocked by #126
- [ ] #135 Users create, list and revoke their own personal tokens on `/account` ⛔ blocked by #134, WP-25

### WP-40 — Conformance suite per profile · F-02 · Branch: `wp/40-conformance-profiles` · PR: open

- [ ] #136 The conformance suite runs the full tool set and its error cases once per profile ⛔ blocked by #131, #132

### WP-41 — connect and doctor · F-02 · Branch: `wp/41-connect-doctor` · PR: open

- [ ] #137 `connect claude-code` merges the server into Claude Code's config, and `connect claude-ai` prints the setup steps
- [ ] #138 `doctor --client` proves reachability, auth, profile and a write round trip in a test namespace ⛔ blocked by #131, #137

---

## M13 — Open WebUI, released on its own

Goal: see [F-02](./features/F-02-client-integrations.md) → Milestones. ⛔ blocked by O13 (ADR-0011) · Due: open

### WP-42 — Open WebUI spike and per-user auth · F-02 · Branch: `wp/42-openwebui-auth` · PR: open

- [ ] #141 A pinned Open WebUI stack runs in Compose with memory-manager, Postgres and Ollama ⛔ blocked by #126
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
- [ ] #157 Open WebUI works with Entra sign-in through the auth facade ⛔ blocked by #144, F-01 WP-22 (Entra login)
- [ ] #158 A release with Open WebUI support is published ⛔ blocked by #155, #156

---

## M14 — IDE and CLI clients

Goal: see [F-02](./features/F-02-client-integrations.md) → Milestones. ⛔ blocked by O19 (support matrix) · Due: open

### WP-47 — Headless CLI harness and Codex · F-02 · Branch: `wp/47-headless-cli` · PR: open

- [ ] #161 A headless E2E harness runs client CLIs in containers against a test instance ⛔ blocked by #136, #159, O21 (owner)
- [ ] #162 Claude Code runs headless in CI and reads and writes memory ⛔ blocked by #161, #137
- [ ] #163 Codex has a profile, integration files and connect and doctor support ⛔ blocked by #159, #133, #137, #138
- [ ] #164 Codex CLI runs headless in CI against the test instance ⛔ blocked by #161, #163

### WP-48 — Gemini CLI and Code Assist · F-02 · Branch: `wp/48-gemini-cli` · PR: open

- [ ] #165 The authorization server returns `iss` in the authorization response when it advertises it
- [ ] #166 Gemini CLI has a profile, integration files and connect and doctor support ⛔ blocked by #159, #165, #133, #137, #138
- [ ] #167 Gemini CLI runs headless in CI against the test instance ⛔ blocked by #161, #166
- [ ] #168 Gemini Code Assist setup is documented and checked manually ⛔ blocked by #166, #160

### WP-49 — Cursor · F-02 · Branch: `wp/49-cursor` · PR: open

- [ ] #169 Cursor has a profile, integration files and connect and doctor support ⛔ blocked by #159, #133, #137, #138
- [ ] #170 Cursor's handling of a static token next to OAuth discovery is verified and documented ⛔ blocked by #169
- [ ] #171 Cursor Teams rollout is documented ⛔ blocked by #169, #160

### WP-50 — GitHub Copilot · F-02 · Branch: `wp/50-copilot` · PR: open

- [ ] #172 GitHub Copilot in VS Code and Copilot CLI has a profile, integration files and connect and doctor support ⛔ blocked by #159, #133, #137, #138
- [ ] #173 Copilot coding agent and JetBrains use memory-manager with a personal token ⛔ blocked by #172, #134, #160
- [ ] #174 Copilot Business and Enterprise MCP policy rollout is documented ⛔ blocked by #172

### WP-51 — Google Antigravity · F-02 · Branch: `wp/51-antigravity` · PR: open

- [ ] #175 Antigravity has a profile, integration files and connect and doctor support ⛔ blocked by #159, #133, #137, #138, #160

---

## M15 — Autonomous agent runtimes

Goal: see [F-02](./features/F-02-client-integrations.md) → Milestones. ⛔ blocked by O16 (ADR-0013), O19 · Due: open

### WP-52 — Agent identity and write guard · F-02 · Branch: `wp/52-agent-identity` · PR: open

- [ ] #176 Agent tokens and the agent namespace kind exist on both storage backends ⛔ blocked by #126, #134
- [ ] #177 The agent write policy is enforced on the server ⛔ blocked by #176
- [ ] #178 Pending agent writes wait for owner approval through the CLI ⛔ blocked by #177
- [ ] #179 Owners approve agent writes on `/account` ⛔ blocked by #178, F-01 WP-25 (/account)
- [ ] #180 Delegation grants let an agent read or write its owner's namespace only when granted ⛔ blocked by #176
- [ ] #181 Audit log and metrics name the agent and the triggering channel ⛔ blocked by #176
- [ ] #182 Per-agent quotas limit requests and writes ⛔ blocked by #176, F-01 WP-27 (quotas)
- [ ] #183 A third party's remember request never reaches the owner's namespace ⛔ blocked by #177, #180, #178

### WP-53 — Hermes Agent · F-02 · Branch: `wp/53-hermes` · PR: open

- [ ] #184 Hermes Agent has a profile, config, skill and connect and doctor support ⛔ blocked by #159, #176, #132, #137, #138
- [ ] #185 `memory-manager import hermes` imports Hermes memory files ⛔ blocked by #159, #176
- [ ] #186 Hermes runs headless against memory-manager in a Compose E2E with a pinned version ⛔ blocked by #184, #183

### WP-54 — OpenClaw · F-02 · Branch: `wp/54-openclaw` · PR: open

- [ ] #187 OpenClaw has a profile, config, skill and connect and doctor support ⛔ blocked by #159, #176, #137, #138
- [ ] #188 `memory-manager import openclaw` imports OpenClaw memory files ⛔ blocked by #159, #176
- [ ] #189 OpenClaw runs headless against memory-manager in a Compose E2E with a pinned version ⛔ blocked by #187, #183

### WP-55 — Native agent memory integration · F-02 · Branch: `wp/55-agent-native` · PR: open

- [ ] #190 The decision on native agent memory integration is recorded ⛔ blocked by #159, #184, #187
- [ ] #191 A Hermes memory provider stores through memory-manager ⛔ blocked by #190, #186
- [ ] #192 An OpenClaw memory-slot plugin stores through memory-manager ⛔ blocked by #190, #189

---

## M16 — Web clients

Goal: see [F-02](./features/F-02-client-integrations.md) → Milestones. ⛔ blocked by O19 · Due: open

### WP-56 — ChatGPT · F-02 · Branch: `wp/56-chatgpt` · PR: open

- [ ] #194 ChatGPT has a profile, a docs page and connect steps ⛔ blocked by #159, #132, #137, #160
- [ ] #195 ChatGPT Business and Enterprise rollout is documented ⛔ blocked by #194

### WP-57 — Gemini Enterprise · F-02 · Branch: `wp/57-gemini-enterprise` · PR: open

- [ ] #196 The decision on pre-registered OAuth clients for Gemini Enterprise is recorded ⛔ blocked by #159
- [ ] #197 Operators can register confidential OAuth clients in the authorization server ⛔ blocked by #196
- [ ] #198 Gemini Enterprise is documented within its limits and the consumer Gemini app is documented as not possible ⛔ blocked by #196, #160

### WP-58 — Manual client checklist · F-02 · Branch: `wp/58-client-checklist` · PR: open

- [ ] #160 `docs/release/client-checklist.md` lists the manual acceptance steps for every GUI and web client ⛔ blocked by #159
- [ ] #193 The client checklist is part of the release process ⛔ blocked by #160

---

## M17 — v1.0.0-rc

Goal: see [F-02](./features/F-02-client-integrations.md) → Milestones. · Due: open

### WP-59 — Stable API and compatibility policy · F-02 · Branch: `wp/59-stable-api` · PR: open

- [ ] #199 `docs/compatibility.md` defines the SemVer and deprecation policy ⛔ blocked by #159
- [ ] #200 The tool contract, config format, note format and CLI are documented as stable reference ⛔ blocked by #199
- [ ] #201 A contract snapshot test fails on breaking changes to the tool contract ⛔ blocked by #199
- [ ] #202 An upgrade guide from the last 0.x release exists and its migrations are tested ⛔ blocked by #199, F-01 WP-34 (v0.2.0)

### WP-60 — Security review for the release candidate · F-02 · Branch: `wp/60-security-rc` · PR: open

- [ ] #203 The threat model covers Open WebUI identity, personal tokens and agents with third-party input ⛔ blocked by F-01 WP-33 (threat model), #143, #183, #134
- [ ] #204 The release candidate has no open High or Critical security findings ⛔ blocked by #203

### WP-61 — Documentation website · F-02 · Branch: `wp/61-docs-site` · PR: open

- [ ] #205 The decision on docs website tooling is recorded ⛔ blocked by #159
- [ ] #206 A docs website with quickstart, client pages, support matrix, operations and enterprise is built in CI ⛔ blocked by #205, #200, #155
- [ ] #207 The README lists supported clients as text with links and no logos ⛔ blocked by #159

### WP-62 — Release v1.0.0-rc.1 · F-02 · Branch: `wp/62-release-rc` · PR: open

- [ ] #208 Every feature in TASKS is done or deferred past 1.0 with a reason ⛔ blocked by #158, #204, #202
- [ ] #209 `v1.0.0-rc.1` is released with notes, a signed image and the Helm chart ⛔ blocked by #208, #193, #206, #207, #201, F-01 WP-34 (v0.2.0)
