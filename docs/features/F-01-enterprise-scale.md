# F-01 — Enterprise Scale

Status: in progress · Created: 2026-10-07
Milestones: M7, M8, M9, M10, M11 · Work packages: WP-16 … WP-34 · Label: `feature:F-01`

## Benefit

An organisation with Microsoft Entra ID can run memory-manager for about 2,000 employees. Each employee has a private memory plus shared group, project and org memory, signs in with their company account from claude.ai and Claude Code, and the server scales horizontally. Single-user and team deployments on the Git backend keep working unchanged.

**Done when:**
- The k6 load test against the synthetic target-size dataset (1M notes, 5M chunks) on 3 API replicas meets search p95 < 300 ms, read p95 < 100 ms and write p95 < 200 ms, with < 1 % failed requests while one replica is killed.
- The permission matrix and RLS suites are green.
- The enterprise Helm profile is rolled out by the Flux example.
- The compliance templates are in `docs/compliance/`.
- `v0.2.0` is released with an upgrade guide.

## Not in scope

- Multi-tenancy across organisations: one Entra tenant (plus explicitly allowed guest tenants) per deployment.
- SCIM provisioning. The Graph delta sync covers deprovisioning (ADR-0006).
- Accepting Entra-issued access tokens directly (resource-server mode for service principals) and JWKS validation (ADR-0006).
- Stateful MCP sessions, elicitation, sampling, server-initiated notifications (ADR-0009).
- An editing UI. `/account` covers self-service and administration only, with no note editor.
- An admin CLI for namespaces, ACLs, erasure or break-glass. Administration happens on `/account` (owner, 2026-10-07).
- Crypto-shredding of revisions (research §3, revisit later).
- Automatic mirroring of Entra groups into namespaces. Group namespaces are created explicitly.
- Live Git round trip in enterprise mode. Export to Git/blob is one-way.

## Existing users

- **Git backend (default):** no behaviour change. `STORAGE_BACKEND` defaults to `git`, and the chart keeps `replicas: 1` + `Recreate` for it and refuses an HPA.
- **MCP tool contract:** additive only.
  - New tool `memory_promote`.
  - New result field `namespace_kind`.
  - `path` stays `<namespace>/<type>/<slug>.md`; `if_version` stays the SHA-256 content hash.
- **Login modes `oidc` and `password`:** unchanged; `entra` is a third mode.
- **Static tokens:** unchanged outside enterprise mode. In enterprise mode, new tokens require scopes, an expiry and an owner.
- **Migration path:** `memory-manager migrate git-to-postgres` (M9). Rollback means `export` from Postgres to a Git vault.
- **CLAUDE.md rules adapted** for the Postgres backend (source of truth, erasure outside MCP), see ADR-0007.

## Architecture delta

| Component | Delta | ADR |
|---|---|---|
| `src/memory_manager/storage/` | new: `StorageBackend` protocol, Git implementation (wraps `queue.py` + `vault/repo.py`), Postgres implementation | [ADR-0007](../adr/0007-storage-backend.md) |
| `src/memory_manager/db/migrations/` | new tables: `vault_notes`, `vault_revisions`, `namespaces`, `users`, `user_groups`, `project_members`, `namespace_settings`, `break_glass_grants`, `jobs`, `erasure_log`, `rate_limits`; app role + RLS policies | ADR-0007, [ADR-0008](../adr/0008-namespace-permissions.md) |
| `src/memory_manager/index/` | extended: indexes from `vault_notes` as well as from the Git tree; `halfvec` + partitioning by namespace kind (enterprise) | ADR-0007 |
| `src/memory_manager/mcp/` | extended: principal (oid, roles, groups), alias resolution (`me`), `memory_promote`, `namespace_kind` | ADR-0008 |
| `src/memory_manager/auth/` | extended: login mode `entra`, Graph client (overage, delta sync), `SharedState` (Valkey or Postgres) replacing in-memory limiter and pending state | [ADR-0006](../adr/0006-enterprise-auth-entra.md), [ADR-0009](../adr/0009-stateless-replicas.md) |
| `src/memory_manager/worker.py` | new: worker process (embedding jobs, delta sync, retention, cleanup) | ADR-0007, ADR-0009 |
| `/account` (HTTP, server-rendered) | new: self-service export/delete, admin area (namespaces, ACLs, erasure, break-glass) | ADR-0008 |
| `charts/memory-manager` | extended: enterprise values profile, `api`/`worker` deployments, HPA, PDB, topology spread, NetworkPolicies, CNPG with Barman Cloud plugin, optional Valkey, ServiceMonitor | ADR-0009 |
| `deploy/entra/`, `deploy/flux/` | new: OpenTofu/Terraform module for the app registration; Flux enterprise example | ADR-0006 |
| `loadtest/` | new: deterministic dataset generator (Python), k6 scenarios, results in `docs/benchmarks/` | — (owner 2026-10-07: k6) |
| `docs/security/`, `docs/compliance/` | new: STRIDE threat model, pen-test checklist, compliance templates | — |

## Milestones

Risk first. M7 proves the data model at scale: Postgres as source of truth, RLS that holds, several replicas, measured latency. That is where the feature can fail. Entra login (M8) extends a login path that already works. Governance, operations and the release build on both.

| Milestone | Delivers | Acceptance | Work packages |
|---|---|---|---|
| M7 — Shared memory in Postgres on several replicas | Postgres backend behind `StorageBackend`, namespaces with RLS, shared state, two replicas serving one dataset; first latency baseline | `uv run pytest tests/e2e/test_replicas.py` green (two server processes, one Postgres: write on one, read on the other, foreign namespace denied, one process killed mid-run) and `docs/benchmarks/baseline.md` committed | WP-16, WP-17, WP-18, WP-19, WP-20, WP-21 |
| M8 — Entra ID sign-in and asynchronous embeddings | login mode `entra` with roles and groups (incl. overage), worker process with embedding queue, Graph delta sync revoking deprovisioned users | e2e test with the mock IdP: sign-in via DCR client, note written into `me`, user disabled in the mock → next call 401 within one sync interval; search returns a fresh note via full text before its embedding exists | WP-22, WP-23, WP-24 |
| M9 — Data lifecycle and governance | `memory_promote`, `/account` self-service and admin area, break-glass, erasure + retention, quotas, blocklists, SIEM audit export, `migrate git-to-postgres` | erasure test: after deleting a user, no content in `vault_notes`, `vault_revisions`, `chunks`, `jobs` or audit payloads; example vault migrated with identical versions | WP-25, WP-26, WP-27, WP-28 |
| M10 — Enterprise operations | Helm enterprise profile, CNPG backups, optional Valkey, observability (OTel, metrics, Grafana dashboard, alerts), `deploy/entra/`, Flux example | `helm template` with the enterprise values passes the chart tests; a kind cluster rolls out the Flux example and `/readyz` is 200 on 3 API replicas | WP-29, WP-30, WP-31 |
| M11 — Proven at target size, released as v0.2.0 | load test at target size incl. replica failure, threat model + pen-test checklist, compliance templates, upgrade guide, release | benchmark report in `docs/benchmarks/` meets all targets; `v0.2.0` tag with signed image and chart | WP-32, WP-33, WP-34 |

Work packages of M8–M11 (cut into issues on 2026-10-08, ahead of the code at the owner's request; each milestone's issues are re-read against the code when it starts):

- **WP-22** `wp/22-entra-login`: login mode `entra` (confidential client, tenant allowlist, roles, groups incl. Graph overage), `users`/`user_groups` filled at login, refresh re-check, `ENTRA_MAX_SESSION`, mock IdP for tests. Issues: #212, #213, #214, #215, #216.
- **WP-23** `wp/23-worker-embeddings`: vector-index spike (#125, proposes ADR-0016), `worker` entry point, `jobs` outbox with `SKIP LOCKED` + `LISTEN/NOTIFY`, asynchronous embeddings, singleton jobs via advisory lock. Issues: #125, #217, #218, #219, #220, #221.
- **WP-24** `wp/24-deprovisioning`: Graph delta sync job, token-family revocation for disabled/deleted users, static tokens with mandatory expiry/scopes/owner in enterprise mode. Issues: #222, #223, #224, #225.
- **WP-25** `wp/25-promote-account`: `memory_promote`, `/account` page with export (Markdown ZIP) and delete-my-memory. Issues: #226, #227, #228, #229, #230, #232.
- **WP-26** `wp/26-admin-erasure`: admin area (namespaces, ACLs, settings), break-glass with two-admin approval, erasure + `erasure_log` replay, retention job, deletion test. Issues: #231, #233, #234, #235, #236, #237, #238, #239, #240, #241.
- **WP-27** `wp/27-quotas-blocklists`: quotas per user/namespace (count, size, rate) on shared state, configurable blocklist categories checked server-side, audit export to stdout/OTLP for SIEM. Issues: #242, #243, #244, #245.
- **WP-28** `wp/28-migrate-git`: `memory-manager migrate git-to-postgres` with history as revisions, dry run, namespace mapping. Issues: #246, #247, #248.
- **WP-29** `wp/29-helm-enterprise`: enterprise values profile, `api`/`worker` deployments, HPA (CPU + RPS), PDB, topology spread, NetworkPolicies, CNPG + Barman Cloud plugin, optional Valkey. Issues: #249, #250, #251, #252, #253, #254, #255.
- **WP-30** `wp/30-entra-flux`: `deploy/entra/` OpenTofu module, Flux enterprise example, operator guide. Issues: #256, #257, #258, #259.
- **WP-31** `wp/31-observability`: OTel traces across api/worker/DB, per-tool metrics (latency, errors, rate-limit hits, queue length, embedding lag), Grafana dashboard + alert rules, ServiceMonitor. Issues: #260, #261, #262, #263, #264, #265.
- **WP-32** `wp/32-load-target`: target-size dataset, k6 runs on 3 replicas incl. replica kill, report and hardware profile in `docs/benchmarks/`. Issues: #266, #267, #268, #269, #270, #271.
- **WP-33** `wp/33-security-compliance`: `docs/security/threat-model.md` (STRIDE), pen-test checklist, `docs/compliance/` templates (data flow, TOMs, deletion concept, roles and permissions, DPIA template, employee transparency notice). Issues: #272, #273, #274, #275, #276.
- **WP-34** `wp/34-release-0-2`: upgrade guide, README enterprise section, release `v0.2.0`. Issues: #277, #278, #279, #280.

## Open decisions

| Question | Options | Blocks |
|---|---|---|
| Vector index and partitioning (PLAN O22) | measured by the spike #125 at the start of WP-23: `halfvec` vs `vector` (recall on the golden set, size, latency), HNSW parameters, partitioning of `chunks` by namespace kind incl. `agent` (ADR-0013); the result is proposed as ADR-0016 | the `chunks` schema in WP-23 and everything that builds on it; WP-32 |
| R2 cost under load | — decided 2026-10-08 by the owner: keep R2 ([baseline](../benchmarks/baseline.md): ~1.6 ms vs ~0.1 ms per access-function call, targets met at 100k); re-evaluate only if WP-32 misses the budget at target size | — |

Decided by the owner on 2026-10-08 while cutting M8–M11 (PLAN O23–O37):

- **Embedding dimension:** the existing `EMBEDDING_DIMENSIONS` setting is pinned at the first migration in Postgres mode (default 1024) and immutable afterwards. A model with another dimension needs a reindex.
- **Load test at target size:** runs on an existing operator Kubernetes cluster as it is. The report in `docs/benchmarks/` names only a generic hardware profile and is not a sizing commitment.
- **`/account`:** cookie session with a hashed row in Postgres and CSRF tokens, available in every embedded-AS login mode; enterprise sections only with the Postgres backend ([ADR-0008](../adr/0008-namespace-permissions.md) addendum 2026-10-08).
- **Blocklist:** operator-defined categories with regex/keyword lists. A hit rejects the write and is audited without content. Empty by default, with an example file.
- **Quotas** also key per token, because agent quotas (ADR-0013) reuse them.
- **Autoscaling:** CPU HPA always, RPS optional through a KEDA `ScaledObject` ([ADR-0009](../adr/0009-stateless-replicas.md) addendum 2026-10-08).
- **Break-glass notification:** `/account` banner until acknowledged plus a system note in the user's `me` namespace (ADR-0008 addendum 2026-10-08).
- **kind/Flux E2E:** own CI workflow, path-filtered on `charts/`, `deploy/` and `Dockerfile`, plus manual dispatch.
- **Entra test double:** own mock IdP under `tests/`, imitating OIDC, Graph `getMemberGroups` and the users delta query. No new dependency.
- **Compliance templates:** English, GDPR-generic, with an optional Germany section (works council, §87(1) no. 6 BetrVG).
- **`migrate git-to-postgres`** takes repeatable `--map <git-ns>=<kind>:<key>[:<alias>]` flags, no map file.
- **v0.2.0** is cut with a `Release-As: 0.2.0` footer, because release-please bumps only the patch for a `feat` in 0.x.
- **Entra:** Graph permissions `User.Read.All` + `GroupMember.Read.All`; a removed role takes effect at the next Entra login; a Graph outage during refresh gives a retryable error; `entra` requires the Postgres backend; static-token owners must exist in `users` ([ADR-0006](../adr/0006-enterprise-auth-entra.md) addendum 2026-10-08).
- **Erasure:** removes `me` and the identity, pseudonymizes the user's authorship in shared namespaces and redacts audit paths; `erasure_log` survives restores through the SIEM export and `ERASURE_LOG_REPLAY_FILE` ([ADR-0007](../adr/0007-storage-backend.md) addendum 2026-10-08).
- **Break-glass reads** happen only in a read-only viewer on `/account`.
- **Image:** ships the `otel` extra next to `valkey` (ADR-0009 addendum 2026-10-08).
- **Worker** serves health and metrics on its own port; `jobs` carries a `traceparent` column; database spans are hand-written (no new dependency).
- **Order:** F-01 is finished before F-02 starts.

## Spikes

The latency baseline (WP-21) was the spike for the RLS function cost (`docs/benchmarks/baseline.md`). It ran without embeddings, so the index strategy gets its own spike: #125 opens WP-23, writes `docs/research/vector-index.md` and proposes ADR-0016.
