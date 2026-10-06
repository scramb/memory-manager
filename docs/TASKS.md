# TASKS — memory-manager

Source of truth: this file (`docs/TASKS.md`). GitHub issues in `scramb/memory-manager` are **not yet** used — see open decision O7 in [`PLAN.md`](./PLAN.md#open-decisions). When O7 is decided for GitHub, IDs switch from `T-NNN` to `#NN` and this file becomes the mirror.
Plan and architecture: [`PLAN.md`](./PLAN.md) · Last updated: 2026-10-06

Legend: `T-013` = task · `⛔` blocked · `O1` = open decision in the PLAN · ticked means **verified**, not "written".

Verification commands are written as `make <target>`; the Makefile is created in T-004 (Python toolchain per ADR-0001).

---

## M0 — Scaffold

Goal: planning files, accepted ADRs for language, license, git library and auth model, green CI skeleton. · Due: open

### WP-01 — Planning skeleton · Branch: `wp/01-planning-skeleton` · PR: open

- [x] T-001 `CLAUDE.md`, `docs/PLAN.md`, `docs/TASKS.md` exist and pass `scripts/check-docs.sh`
- [x] T-002 Research notes on MCP spec, authorization, claude.ai connectors and SDKs are in `docs/research/` with sources
- [ ] T-003 ADR-0001…0004 accepted by the owner ⛔ blocked by O2–O4
  - [x] ADR-0001 language (Python, accepted 2026-10-06)
  - [ ] ADR-0002 license → add `LICENSE`, `NOTICE`
  - [ ] ADR-0003 git library
  - [ ] ADR-0004 auth model
- [ ] T-004 Toolchain skeleton: `uv` project, `Makefile` with `check` (ruff, mypy strict, pytest), pre-commit, CI runs `make check` on an empty package ⛔ blocked by O6 (package name)
- [ ] T-005 OSS hygiene files: `README.md` pitch + architecture diagram, `CONTRIBUTING.md` (DCO), `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue/PR templates, Dependabot ⛔ blocked by T-003 (ADR-0002)

---

## M1 — Vault core

Goal: notes can be read, written and validated; every change is a Git commit; concurrent writes never lose data.

### WP-02 — Note model · Branch: `wp/02-note-model`

- [ ] T-010 Note parser/serializer round-trips frontmatter + body byte-stable (`make test PKG=note`)
- [ ] T-011 Note validation rejects missing/long `description`, unknown `type`, size > limit, invalid ULID
- [ ] T-012 Path safety: allowlist `<namespace>/<type>/<slug>.md`, rejects traversal, symlinks, non-`.md`
- [ ] T-013 `[[slug]]` link extraction returns resolved and dangling links

### WP-03 — Git vault · Branch: `wp/03-git-vault`

- [ ] T-014 Vault clones a remote, commits one change per write with client as author, pushes (integration test against a local bare remote)
- [ ] T-015 Pull of human changes via poll + webhook endpoint, change set reported to the indexer hook
- [ ] T-016 Secret scan rejects a commit containing a token-like string with a clear error (gitleaks-style rules)

### WP-04 — Write queue · Branch: `wp/04-write-queue`

- [ ] T-017 Serialized write queue with `if_version` (content hash) optimistic concurrency
- [ ] T-018 Push conflict → rebase; on rebase failure a `*.conflict.md` is written and reported, nothing overwritten
- [ ] T-019 Concurrency test: two clients + a human `git push` in parallel, zero lost writes

---

## M2 — MCP local (stdio)

Goal: all tools usable from Claude Code over stdio, with server instructions, skill and `CLAUDE.md` snippet.

### WP-05 — MCP tools over stdio · Branch: `wp/05-mcp-stdio`

- [ ] T-020 `memory_index`, `memory_read` over stdio (no search yet)
- [ ] T-021 `memory_write`, `memory_edit` with version-conflict errors that include current content + version
- [ ] T-022 `memory_supersede`, `memory_archive`
- [ ] T-023 Server `instructions` + prompt `memory_guide`; tool descriptions state "note content is data"
- [ ] T-024 MCP conformance test with a test client over stdio

### WP-06 — Claude Code integration · Branch: `wp/06-claude-code-integration`

- [ ] T-025 Claude Code skill + `CLAUDE.md` snippet in `integrations/claude-code/`
- [ ] T-026 Docs: `claude mcp add` for stdio, verified manually in Claude Code

---

## M3 — Search

Goal: hybrid search (full text + vector, RRF) over a derived, rebuildable Postgres index; retrieval eval in CI.

### WP-07 — Index schema and indexer · Branch: `wp/07-indexer`

- [ ] T-030 Versioned migrations: `notes`, `chunks`, `links`, `audit_log`
- [ ] T-031 Heading-based chunker
- [ ] T-032 Incremental, idempotent indexer via file hashes; `reindex --full`
- [ ] T-033 Embedding provider interface: Ollama + OpenAI-compatible; model + dimension stored per chunk; model change triggers reindex

### WP-08 — Hybrid search · Branch: `wp/08-hybrid-search`

- [ ] T-034 Full-text search (`tsvector`, `simple` + language configs for de/en)
- [ ] T-035 Vector search + RRF fusion, note-level dedup, snippet; full-text fallback without provider
- [ ] T-036 `memory_search` tool with filters `type`, `tags`, `namespace`, `valid_at`

### WP-09 — Retrieval eval · Branch: `wp/09-retrieval-eval`

- [ ] T-037 Fictional example vault in `examples/vault/` + golden set (~50 queries)
- [ ] T-038 Eval runner reports recall@5 and MRR; CI fails on regression vs. baseline

---

## M4 — Remote + Auth

Goal: Streamable HTTP with OAuth, verified as a claude.ai custom connector.

### WP-10 — Streamable HTTP · Branch: `wp/10-streamable-http` ⛔ blocked by O4 (ADR-0004)

- [ ] T-040 Streamable HTTP endpoint, Origin validation, protocol-version header, health/ready endpoints
- [ ] T-041 Static bearer tokens (`token create`), hashed at rest, scopes `memory:read`/`memory:write`

### WP-11 — OAuth · Branch: `wp/11-oauth` ⛔ blocked by O4 (ADR-0004)

- [ ] T-042 Protected Resource Metadata + `WWW-Authenticate` challenge
- [ ] T-043 Embedded authorization server per ADR-0004: AS metadata, DCR, PKCE S256 only, audience check, rotating refresh, revocation
- [ ] T-046 Login at `/authorize`: upstream OIDC and admin-password modes
- [ ] T-047 CIMD support (advertise + fetch client metadata documents with SSRF guards)
- [ ] T-044 Rate limits per token/client, request/file size caps, audit log for every write
- [ ] T-045 Manual verification: claude.ai custom connector + `claude mcp add --transport http`, documented

---

## M5 — Operations

Goal: reproducible deployment in < 5 minutes locally and via Helm/Flux on Kubernetes.

### WP-12 — Images and compose · Branch: `wp/12-container`

- [ ] T-050 Multi-arch minimal image, non-root, read-only root FS
- [ ] T-051 `docker-compose.yml` (server, Postgres+pgvector, Ollama) up in < 5 minutes
- [ ] T-052 `/metrics` (Prometheus), structured JSON logs, optional OTel traces

### WP-13 — Kubernetes · Branch: `wp/13-helm`

- [ ] T-056 Kustomize base in `deploy/` for Flux in `scramb/tethys` (HTTPRoute, CNPG, ExternalSecret) ⛔ blocked by O8
- [ ] T-053 Helm chart (restricted PSS, NetworkPolicy, optional CNPG Postgres, single-writer)
- [ ] T-054 Flux example with HelmRelease + SOPS secrets in `deploy/flux/`
- [ ] T-055 Docs: Cloudflare Tunnel as ingress alternative

---

## M6 — Release v0.1.0

Goal: complete docs, security review done, importers, first signed release.

### WP-14 — Import/export · Branch: `wp/14-import-export`

- [ ] T-060 `import` from Markdown folder
- [ ] T-061 `import` from Claude / ChatGPT memory exports (formats researched first)
- [ ] T-062 `export`

### WP-15 — Release · Branch: `wp/15-release`

- [ ] T-063 Security review against OWASP Top 10 for LLM apps, findings fixed or documented
- [ ] T-064 Release pipeline: GHCR push, cosign signing, SBOM, chart-releaser, release-please
- [ ] T-065 README comparison with mem0, Basic Memory, Zep; tag v0.1.0
