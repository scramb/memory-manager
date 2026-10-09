# Threat model — enterprise deployment

Date: 2026-10-09 · Scope: every trust boundary F-01 (`docs/features/F-01-*.md`
work packages WP-22–WP-31) adds to the enterprise deployment
(`STORAGE_BACKEND=postgres`, [ADR-0006](../adr/0006-enterprise-auth-entra.md)–
[ADR-0009](../adr/0009-stateless-replicas.md)) · Method: STRIDE per flow of
the diagram below, each category backed by a file:line or ADR § pointer,
checked by reading the referenced code in this worktree (CLAUDE.md: "verify,
don't recall"), not by re-deriving architecture already decided.

[`docs/security-review.md`](../security-review.md) (2026-10-07) is a
code-level review of v0.1 against the OWASP Top 10 for LLM Applications 2025
plus classic web/OAuth concerns. Its findings are **referenced, not
repeated**: where an enterprise flow reuses a v0.1 control (token hashing,
symlink-safe reads, prompt-injection framing, SQL parameterisation, CSP,
webhook HMAC), this document points at that review instead of re-deriving it.
This document's own job is the boundaries v0.1 did not have at all: Entra
login and Graph, several stateless replicas on shared Postgres with RLS,
Valkey, the `/account` browser session and admin area, break-glass, erasure,
quotas, a note-content blocklist, and the SIEM audit export.

**Out of scope here:** running a pen test — [`pentest-checklist.md`](./pentest-checklist.md)
(#273) turns every threat of severity medium or higher above into an
executable test case — compliance templates
(#274–#276 / 33c–33e), F-02 elements — Open WebUI identity, personal tokens,
agent runtimes (tracked under [#203](https://github.com/scramb/memory-manager/issues/203),
see "Out of scope / extension points" below) — and fixing any finding this
document surfaces (each becomes its own issue).

## Assets

Everything [`docs/security-review.md`](../security-review.md)'s own threat
model names, plus what F-01 adds:

- Note content, including personal facts in a user's `me` namespace.
- OAuth/static-token secrets and their hashes; **new:** Entra facade tokens
  bound to `oid`, personal static tokens with an owner, `/account` session
  ids (hashed, `account_sessions.session_hash`).
- The audit log; **new:** the `erasure_log` (IDs, actor, reason, row counts —
  never content) and its SIEM export copy.
- **New:** the Entra app registration's client secret and the Graph
  application-permission grant (`User.Read.All`, `GroupMember.Read.All`,
  tenant-admin consent) — compromise of either lets an attacker enumerate or
  impersonate any tenant user towards this server.
- **New:** `users`/`user_groups`/`namespaces`/`project_members` — the
  permission-relevant registry RLS and the application both read.
- **New:** a disabled or departed user's personal namespace during its
  `PERSONAL_RETENTION_DAYS` retention window, and a break-glass grant's
  target namespace while the grant is live.
- **New:** the external embedding API (unchanged asset from v0.1, now also
  reachable from the `worker` deployment, not only the single `api` process).

**Entry points**, in addition to v0.1's (see
[`docs/security-review.md`](../security-review.md)): the Entra login
redirect and callback (`/oidc/callback`, shared with `oidc` mode), the
embedded AS's own `/authorize`/`/token` now fronting Entra as a facade,
Microsoft Graph (reached by this server, not reaching in), the `worker`
deployment's own `/healthz`/`/readyz` and metrics, `/account` and
`/account/admin/*`, and — operator-only — `ERASURE_LOG_REPLAY_FILE` and
`BLOCKLIST_FILE` read at startup.

**Trust boundaries**, in addition to v0.1's four: (5) Microsoft Entra ID and
Microsoft Graph are trusted identity/directory providers reached over HTTPS,
outside this project's control; (6) a browser holding an `/account` session
cookie is a different principal class from an MCP bearer-token client — same
human, different credential, different blast radius if stolen; (7) an `api`
or `worker` replica is interchangeable with any other (ADR-0009) — no replica
is more trusted than another; (8) Valkey, if configured, holds only
loss-tolerant state and is not a store of record.

## Diagram

```mermaid
flowchart LR
    subgraph untrusted["Untrusted — MCP clients"]
        claudeai["claude.ai"]
        claudecode["Claude Code"]
    end

    subgraph browser["Browser (session cookie, not a bearer token)"]
        user["user / Memory.Admin"]
    end

    subgraph trusted["Trusted — this deployment"]
        api["api (stateless, N replicas)\nMCP + facade AS + /account"]
        worker["worker (stateless, N replicas)\nembedding jobs, delta sync, retention"]
    end

    subgraph data["Data plane"]
        pg[("Postgres\nRLS-protected content + registry")]
        valkey[("Valkey (optional)\nrate limits, pending login")]
    end

    subgraph msft["Microsoft 365 tenant — trusted, external"]
        entra["Entra ID\n(OIDC login)"]
        graph["Microsoft Graph\n(app-only, delta sync)"]
    end

    subgraph externals["Untrusted network peers"]
        embed["embedding API\n(optional, pluggable)"]
        siem["SIEM export target\n(stdout/OTLP collector)"]
    end

    claudeai -- "HTTPS + OAuth (facade)" --> api
    claudecode -- "HTTP/stdio + OAuth (facade)" --> api
    user -- "HTTPS + session cookie + CSRF" --> api
    api -- "RLS-switched connection\n(db.rls.request_connection)" --> pg
    worker -- "owner connection\n(system identity)" --> pg
    api -. "rate limits, pending login\n(if VALKEY_URL set)" .-> valkey
    api -- "authorization code + PKCE\n(ADR-0006 facade)" --> entra
    api -- "app-only: user_state, member_groups" --> graph
    worker -- "app-only: users/delta\n(deprovisioning, #223)" --> graph
    worker -- "chunk text, batched" --> embed
    api -- "audit_log rows\n(metadata only)" --> siem
    worker -- "erasure_log rows\n(metadata only)" --> siem
```

## STRIDE legend

**S**poofing · **T**ampering · **R**epudiation · **I**nformation disclosure ·
**D**enial of service · **E**levation of privilege.

## Flow 1 — MCP client (claude.ai / Claude Code) ↔ `api`

| # | Threat | Asset | Mitigation | Residual risk | Severity |
|---|---|---|---|---|---|
| S | A client presents a forged or replayed bearer token | facade tokens, note content | OAuth 2.1 + PKCE S256 enforced by the SDK before the provider runs (`security-review.md` ASVS "Authentication"); RFC 8707 `resource` checked on **every** verification, not only at issuance (`auth/verifier.py:68-84,104-120`) | none beyond v0.1's own scope | Low |
| T | A client mutates its own claims to reach another namespace or role | namespace content, RLS registry | `oid`/`roles` are read verbatim from the verified token's claims, never recomputed from caller input (`db/rls.py::current_principal`, L257-285); both the app (`mcp/authz.py`) and Postgres RLS (`db/rls.py`, Flow 2) independently gate access from the same claims (ADR-0008 R2) | RLS protects against a missing filter, not against SQL injection that could set a session variable — parameterised queries stay mandatory (ADR-0008 Consequences; `security-review.md` ASVS "Injection") | Low |
| R | A client denies having written or read a note | audit trail | every processed write gets one `audit_log` row naming actor/op/outcome (CLAUDE.md "audit log for every write", `app.py::_audit_write_hook` L472-498); `vault_revisions.author_oid` ties every revision to a principal (ADR-0008 addendum "curate is author-based") | ordinary reads (`memory_search`/`memory_index`/`memory_read`) are **not** individually audited — unchanged v0.1 scope, accepted there; the one *new* read path that is audited end-to-end is the break-glass viewer (Flow 8) | Informational, accepted (unchanged from v0.1) |
| I | A client enumerates or reads content outside its namespaces | note content, cross-namespace existence | namespace restriction fails closed, checked before any query is built, both for search and for a by-id read (`security-review.md` LLM02, carried forward unchanged); a result's new `namespace_kind` field (ADR-0008) adds no new disclosure — it reports only what the caller can already read | none new | Low |
| D | A client floods write or search calls | availability, Postgres/embedding capacity | per-token/write rate limiting (`security-review.md` LLM10, unchanged); **new:** `QuotaChecker` per-user/namespace/token fixed-window budgets on top, off by default (`quotas.py::QuotaChecker` L146, `check_write` L182) | `check_write` fails **open** on a `SharedState` backend error (Postgres/Valkey outage) rather than rejecting every write (module docstring L38-45) — a deliberate availability-over-strictness choice, logged at WARNING and counted in `mm_rate_limit_hits_total`; accepted, same reasoning as `http.py`'s own fail-open path | Low, accepted |
| E | A client's token claims are trusted to grant a role/namespace it does not actually hold | RLS registry, shared/org namespaces | two **independent** computations of access (Python from claims, SQL from `user_groups`/`project_members`/`namespaces` via `mm_readable_ns()`/`mm_writable_ns()`) must agree; disagreement fails closed (ADR-0008 addendum "when the two disagree, the request fails closed") | none new | Low |

## Flow 2 — `api`/`worker` ↔ Postgres (RLS content, registry, quotas, jobs outbox)

| # | Threat | Asset | Mitigation | Residual risk | Severity |
|---|---|---|---|---|---|
| S | A request transaction runs under the wrong principal | every RLS table | `request_connection` raises `NoPrincipal` *before* a connection is even acquired if the request carries no `oid` claim — never falls through to running as the owner (`db/rls.py:288-314`) | `serve --stdio` refuses `STORAGE_BACKEND=postgres` outright, because a claimed stdio identity would protect nothing against a local process holding the owner credentials (ADR-0008 addendum, 2026-10-07) — accepted, documented limit of enterprise mode's remote-only requirement | Low, accepted |
| T | A request transaction (or a bug in a future code path) writes/reads another namespace's rows directly in SQL | `vault_notes`, `notes`, `chunks`, `links`, `vault_revisions` | `ENABLE`/`FORCE ROW LEVEL SECURITY` on all five tables (`db/rls.py` module docstring L2-16, `0005_rls.sql`); the app role holds no grant on `namespaces`/membership tables at all — only `SECURITY DEFINER` functions read those with the owner's privileges (`db/rls.py::grant_app_role` L113-146); `grant_app_role`/`check_app_role` both refuse a role that is a superuser, has `BYPASSRLS`, or is the owner itself (`db/rls.py:122,146,317-341`) | the RLS policy function sits on the hot path of every query; its cost under load is the load test's job, not this document's (ADR-0008 Consequences) | Low |
| R | A system job (indexer, `reindex --full`, worker) acts with no attributable identity | audit trail for system-run writes | the owner role is an explicit, named system identity with its own policy (`TO` the migrating role, `db/rls.py` addendum 2026-10-07 "the owner role is the system identity") rather than an implicit bypass; every erasure call writes its own `erasure_log` + redacted `audit_log` row naming the real actor (`storage/erasure.py::_write_erasure_log` L257-285) | a human holding the raw `DATABASE_URL` is the fully-trusted operator boundary (trust boundary 4, `security-review.md`) — accepted, unchanged | Informational, accepted |
| I | The `jobs` outbox leaks note content through its payload; **and:** an erased user's identity survives, re-linkable, in notes they co-authored in a shared namespace | embedding job payloads; a shared namespace's `vault_revisions` and `audit_log` | the app role gets **no `SELECT` at all** on `jobs` — it can insert, never read a row back (`db/rls.py:81-86`); payloads carry only `{"note_id": ..., "version": ...}`, never note text (`storage/erasure.py` module docstring L25-28, matching `jobs.py`'s own contract). **Erasure (ADR-0007 §3 addendum 2026-10-08):** `storage/erasure.py::erase_user` (L440-526) hard-deletes the personal namespace outright, but a shared namespace's own notes stay — the user's authorship there is instead pseudonymized: every `vault_revisions` row with `author_oid = oid` gets `author`/`author_oid` set to the literal `'erased'` (`erase_user` L478-482); `audit_log` rows for the erased personal namespace have their `path` rewritten to `[erased]` and `detail` cut down to `audit.DETAIL_ALLOWLIST` keys only, via `_redact_audit_for_namespace`/`_redact_audit_for_paths` (`storage/erasure.py::_redact_audit_for_namespace` L242-246, `_redact_audit_for_paths` L235-239) — not for the shared namespace's own audit rows, which stay as they are, since the note itself (not its erased co-author's identity) is what they describe | a restore/PITR before the `erasure_log` replay runs brings the pre-pseudonymization `author_oid` and the un-redacted `audit_log` path back, same backup-horizon caveat as Flow 7 R (default 30 d + 7 d, `ERASURE_LOG_REPLAY_FILE` closes it, #233) | Low |
| D | One namespace (or one hostile write-scoped token) exhausts shared Postgres capacity | multi-tenant availability | `StorageQuotaChecker` bounds per-namespace note count/size, independent of the per-request rate limit (`quotas.py::StorageQuotaExceeded` L316, `StorageQuotaChecker` L345) | off by default, like every `QUOTA_*` budget — an operator who never sets it keeps v0.1's unbounded-per-namespace behaviour; accepted, same shape as the blocklist's opt-in default | Low, accepted |
| E | SQL injection elevates a request's effective role or namespace set | every RLS table | every query is parameterised (`security-review.md` ASVS "Injection", unchanged); the RLS functions themselves are security-critical SQL with a pinned `search_path` and `REVOKE … FROM PUBLIC` (`db/rls.py` module docstring L18-27) | RLS is explicitly **not** a defence against SQL injection that sets session variables — only against a forgotten `WHERE` (ADR-0008 Consequences, stated verbatim) | Low, accepted with this explicit caveat |

## Flow 3 — `api` ↔ Valkey (optional shared state)

| # | Threat | Asset | Mitigation | Residual risk | Severity |
|---|---|---|---|---|---|
| S | An unauthenticated peer impersonates Valkey on the network | rate-limit counters, pending login state | `VALKEY_URL` accepts only `redis://`/`rediss://`/`unix://` (`config.py::_parse_valkey_url` L870-878); the value itself is never logged, since it may carry a password (same function's own comment) | TLS/auth on the connection is operator configuration (`rediss://` + URL credentials), not enforced by this code — same operator-trust shape as `DATABASE_URL`; accepted | Low, accepted |
| T | A compromised Valkey instance is fed forged counters to bypass rate limiting | rate limiting | Valkey holds only state ADR-0009 explicitly calls loss-tolerant ("Valkey runs without persistence"); durable data (tokens, users, group cache, audit) never lives there (ADR-0009 §2) | a compromised Valkey can at most reset or inflate counters — it cannot forge an access token or a role, which live only in Postgres | Low, accepted |
| R | n/a — Valkey holds no attributable records | — | not applicable: nothing in Valkey is ever read back as an audit fact | — | n/a |
| I | Rate-limit keys (token hash, client IP) leak through Valkey | token hashes, IPs | keys are the same SHA-256 token hash already used elsewhere (CLAUDE.md "token hashes only"), never the raw token; `ValkeySharedState`/`PostgresSharedState` share one contract so neither backend sees anything the other wouldn't (`auth/shared_state.py` module docstring L30-56) | none new | Low |
| D | Valkey becomes unavailable | rate limiting, login | losing Valkey means only reset counters and aborted logins in progress, by design (ADR-0009 §2); `http.py`'s fail-open reasoning applies the same way it does to the Postgres implementation | a deployment that sets `VALKEY_URL` but runs it without any redundancy loses rate limiting until it recovers — documented as acceptable because Valkey is explicitly optional and the Postgres fallback exists for exactly this case | Low, accepted |
| E | n/a — Valkey has no concept of roles this server trusts | — | not applicable | — | n/a |

## Flow 4 — `api` (facade) ↔ Entra ID (OIDC login)

| # | Threat | Asset | Mitigation | Residual risk | Severity |
|---|---|---|---|---|---|
| S | A forged ID token or a token from the wrong tenant is accepted | user identity (`oid`) | authority is pinned to `https://login.microsoftonline.com/<tenant-id>/v2.0`, never `common` (ADR-0006 §1); `tid`/`iss`/`aud`/`exp`/`nonce` checked (ADR-0006 §1); the ID token arrives TLS-direct from the token endpoint, so OIDC Core §3.1.3.7 lets TLS server authentication replace the JOSE signature check — an explicit owner decision, not an oversight (ADR-0006 §2, Decision item 2) | no JWKS signature check in v1 by owner decision; comes back only together with resource-server mode (ADR-0006 Decision item 8) — accepted, documented residual | Low, accepted |
| T | A tampered `roles`/`groups` claim grants excess privilege | RLS registry, permission matrix | roles come only from the `roles` claim set by Entra app-role assignment ("assignment required" on the app registration, ADR-0006 §3); group overage is resolved by this server's own app-only Graph call, never trusted from a client-supplied claim (ADR-0006 §4) | none new | Low |
| R | A user denies having logged in / having been disabled | login/session audit | `disable_user` writes one `audit_log` row per call, success or already-disabled, naming the reason (`auth/users.py::disable_user` L217-246) | — | Low |
| I | Entra discovery/metadata responses leak to an unintended cache consumer across replicas | OIDC discovery document | discovery is cached per-replica with a short, bounded TTL (1 h, ADR-0009 §3) — deliberately local, not shared, since it carries no secret | none new | Low |
| D | Entra/Graph outage blocks every login or every refresh | login availability | a refresh that cannot reach Graph answers a retryable `503 temporarily_unavailable`, never consumes or rotates the refresh token (ADR-0006 addendum 2026-10-08, implemented in `auth/login_entra.py::check_refresh` L254-298, `EntraRefreshOutcome.UNAVAILABLE`) | access is never granted without the check — a sustained Graph outage degrades to "no new logins/refreshes" rather than "open access"; accepted as the deliberate trade-off | Low, accepted |
| E | A disabled or deprovisioned user keeps access | token validity | every refresh re-checks `accountEnabled`/existence via Graph and revokes the token family on disable (`auth/login_entra.py::check_refresh` L277-279, calling `auth/users.py::disable_user`); every **access-token verification**, not only refresh, also fails once `users.disabled_at` is set (`auth/verifier.py::_verify_oauth_access_token` L104-120) — tighter than a refresh-only check | **Explicit residual risk (named in the issue):** Conditional Access and an Entra **role removal** take effect only at the user's next Entra login, bounded above by `ENTRA_MAX_SESSION` (default 12 h) — the per-refresh check deliberately does **not** re-read `appRoleAssignments` (owner decision 2026-10-08, "Reading `appRoleAssignments` on every refresh … is rejected", ADR-0006 addendum). Mitigated by an admin revoking the user's sessions and tokens immediately on `/account` (`account/admin.py`'s "revoke access" action, L51-69; `auth/users.py::revoke_all_credentials` L189-214 for OAuth/static tokens, `account/sessions.py::revoke_all_for_oid` L211-225 for browser sessions) | Medium, accepted with an immediate manual remedy |

## Flow 5 — `api`/`worker` ↔ Microsoft Graph (app-only)

| # | Threat | Asset | Mitigation | Residual risk | Severity |
|---|---|---|---|---|---|
| S | A spoofed Graph endpoint is reached instead of the real tenant | Graph app-only token, user/group data | `GraphClient` refuses a non-`https://` `authority_base_url`/`graph_base_url` unless `ENTRA_ALLOW_INSECURE_AUTHORITY` is explicitly set (`auth/graph.py::GraphClient.__init__` L155-175) | the insecure-authority escape hatch exists for test doubles (`tests/mock_idp`) and must never be set in production — operator misconfiguration risk, documented in the variable's own name | Low, accepted |
| T | Graph application permissions are broader than needed | tenant user/group directory | least-privileged permission set chosen explicitly: `User.Read.All` + `GroupMember.Read.All`, not the broader `Directory.Read.All` that would be needed to read `appRoleAssignments` per refresh (ADR-0006 addendum 2026-10-08, with the Microsoft Graph reference citation) | admin consent for these application permissions is tenant-wide and a manual operator step (ADR-0006 §9) — the OpenTofu module in `deploy/entra/` provisions the app registration and roles but **not** the consent click itself | Low, accepted |
| R | A deprovisioning or group-membership change cannot be traced to a specific delta round | compliance evidence | the delta cursor (`@odata.deltaLink`) and both `last_run_at`/`last_success_at` are persisted (`worker.py::_save_entra_delta_cursor` L414-428); every disable goes through the same audited `disable_user` as Flow 4 | — | Low |
| I | App-only Graph responses (which include every tenant user's basic profile) are over-exposed | tenant directory data | the app-only token and every Graph response stay server-side; nothing from a `users/delta` page or `getMemberGroups` response is ever returned to an MCP client or rendered in `/account` beyond the derived `oid`/role/group-id values already used for authorization | none new | Low |
| D | A Graph `429`/`5xx` storm stalls the delta sync or every login's refresh check | login/refresh availability, deprovisioning timeliness | `GraphClient` retries are bounded, honouring `Retry-After` (`auth/graph.py::_retry_after_seconds`/`_sleep_before_retry` L399-415); a failed delta round leaves the cursor untouched so the next scheduled run retries the identical round rather than skipping ahead (`worker.py::_entra_delta_sync_job` module docstring L431-443) | worst-case deprovisioning latency is the 5 min delta interval, bounded above by the 15 min access-token lifetime (ADR-0006 §6) — an explicitly accepted window, not unbounded | Low, accepted |
| E | A Graph response is trusted to grant a role it was never asked about | roles vs. groups | Graph is consulted only for `accountEnabled`/existence and group membership — **never** for app roles (ADR-0006 addendum: role changes are deliberately not read from Graph on refresh, see Flow 4's E row); roles stay sourced from the ID token's own `roles` claim at login time only | ties to Flow 4's E residual risk (role removal lag) — same mitigation, not duplicated here | Medium, accepted (same as Flow 4 E) |

## Flow 6 — `worker` ↔ embedding API (optional, pluggable)

Unchanged from v0.1 ([`docs/security-review.md`](../security-review.md)
A-02, "note content sent to an external embedding API", accepted) except for
*who* makes the call: the `worker` deployment now makes it asynchronously,
not the single `api` process inline with a write (ADR-0007 §4, "a write
commits the note … then returns; the `worker` pulls embedding jobs").

| # | Threat | Asset | Mitigation | Residual risk | Severity |
|---|---|---|---|---|---|
| S | A spoofed embedding endpoint is reached | note content in transit | operator-configured `EMBEDDING_PROVIDER`/base URL; `ollama`/`none` keep every byte local (`security-review.md` A-02, unchanged) | operator's own choice of a non-TLS or wrong endpoint is out of this server's control, same as v0.1 | Informational, accepted (unchanged) |
| T | A mixed-model/-dimension response corrupts the index | chunk vectors | chunks are stamped with the model/dimension they were produced with, and a response disagreeing on dimension within a batch is rejected (`security-review.md` LLM08, unchanged) | — | Low |
| R | n/a | — | — | — | n/a |
| I | Note content leaves this process's trust boundary | note content | `security-review.md` A-02, deliberate and documented, `ollama`/`none` opt out entirely | an operator who sets `EMBEDDING_PROVIDER=openai` accepts this by that explicit choice (`SECURITY.md` "Security properties") | Informational, accepted (unchanged) |
| D | The embedding provider is slow or down | indexing latency, not correctness | asynchronous embedding jobs mean a slow/unavailable provider delays `mm_embedding_lag_seconds` (`worker.py::_refresh_metrics`/`_EMBED_NOTE_KIND` L277-335) rather than blocking the write that triggered it — an availability **improvement** over v0.1's inline call | retrieval falls back to full text per chunk while vectors are missing (ADR-0007 §4) — no write-path outage from this flow at all now | Low, improved over v0.1 |
| E | n/a | — | — | — | n/a |

## Flow 7 — `api`/`worker` → export targets (SIEM audit export, erasure-log replay, Markdown export)

Three distinct sub-flows share this boundary: (a) `AuditExporter` pushing
every `audit_log`/`erasure_log` row out to stdout/OTLP; (b) an operator
feeding `ERASURE_LOG_REPLAY_FILE` back in after a restore; (c) `export`
producing a Markdown copy that ADR-0007 states is "never read back".

| # | Threat | Asset | Mitigation | Residual risk | Severity |
|---|---|---|---|---|---|
| S | A forged OTLP collector receives the audit stream | audit trail (metadata) | the OTLP target address (`OTEL_EXPORTER_OTLP_ENDPOINT`) is operator configuration, the same variable `observability/tracing.py` already uses for spans — no new trust decision introduced here | operator must point it at a trusted collector; out of this server's control, same as any outbound TLS endpoint | Low, accepted |
| T | An attacker feeds a crafted `ERASURE_LOG_REPLAY_FILE` to resurrect erased data or erase extra data | erased content, erasure guarantee | the file is parsed strictly and rejected on a malformed line before any replay runs (`storage/erasure_replay.py::parse_replay_file`/`_parse_line` L116-176); replay is idempotent and runs under an advisory lock so one replica replays while others wait (commit `ac66586`'s own description) | the file itself is operator-supplied input read only at startup from a path the operator controls — same trust level as `DATABASE_URL`/`BLOCKLIST_FILE`; accepted | Low, accepted |
| R | An erasure cannot be reconstructed after a backup restore rolls back `erasure_log` itself | GDPR Art. 17 compliance evidence | every `erasure_log` row is also exported through the same `AuditExporter` the moment its transaction commits (`storage/erasure.py::_export_erasure` L288-316, fixed for #231/#233) — the off-database copy a restore's rollback cannot touch | documented horizon: backup retention (default 30 d) + 7 d (ADR-0007 §3 addendum); within that window the exported copy is the only record, so losing the SIEM target loses replayability too — operator responsibility, documented in the restore runbook | Low, accepted |
| I | Exported records carry more than metadata | note content | `audit.DETAIL_ALLOWLIST` bounds every `detail` key any call site may set, including erasure's own (`storage/erasure.py` module docstring L30-39); `_export_erasure` exports exactly the same dict, never re-derives one with more fields (`storage/erasure.py:288-316`) | none new | Low |
| D | A SIEM outage blocks writes or erasures | write/erasure availability | `AuditExporter.export` never raises — a target failure is logged and swallowed, the same contract the DB insert itself already gives (`observability/audit_export.py` module docstring L28-32, `_safe_emit` L122-126) | an operator who requires every erasure to reach the SIEM before considering it "done" must monitor export failures themselves — this server does not block on them by design | Low, accepted |
| E | The Markdown export is read back into the Postgres backend, reintroducing erased or stale content | erasure guarantee, data integrity | ADR-0007 §3: export "is never read back" — it is a portability artefact, not an import source; `migrate_git.py`'s `git-to-postgres` import is a distinct, one-time, explicitly operator-invoked path for the *opposite* direction (Git → Postgres at migration time), not a loop back from `export` | enforced by convention/documentation, not by a technical control that refuses a re-import — an operator who scripts one anyway bypasses erasure outside this server's visibility; accepted, same class of risk as any operator with direct DB access (trust boundary 4) | Low, accepted |

## Flow 8 — browser (user / `Memory.Admin`) ↔ `/account`

Covers the session cookie itself, self-service export/delete, the admin
area (namespace/ACL management, "revoke access", "erase"), and break-glass.

| # | Threat | Asset | Mitigation | Residual risk | Severity |
|---|---|---|---|---|---|
| S | A stolen or forged session cookie impersonates the user | `/account` session | opaque, random session id, `HttpOnly`/`Secure`/`SameSite=Strict`/`Path=/account` (ADR-0008 addendum 2026-10-08); only its SHA-256 hash is stored (`account/sessions.py` module docstring L1-10, `create` L122-157) | stealing the cookie itself (XSS, device compromise) is the generic session-hijack risk every cookie-based auth carries — mitigated by the strict `Content-Security-Policy` the login/`/account` templates already set (`security-review.md` LLM05, unchanged), not eliminated | Low, accepted |
| T | A cross-site request triggers a state-changing `/account` action | namespace registry, erasure, revocation | every state-changing form carries a per-session CSRF token, verified in constant time (`account/sessions.py::csrf_token` L228-231, `verify_csrf` L234-236); the token is an HMAC keyed by the *raw* session id, reproducible by nobody who holds only the stored hash; every admin route's own `_authorize_admin_form` checks it before touching the database, right after the session/role check (`account/admin.py::_authorize_admin_form` L355-393, specifically L386-389) — `account/export.py`/`account/delete.py`'s route handlers follow the identical shape for their own single form each | a GET-based action would bypass this — every mutating `/account` route is confirmed `POST`-only: every `Route(...)` registration in `account/admin.py` (L734-740), `account/export.py` (L185) and `account/delete.py` (L167) names `methods=["POST"]` explicitly | Low |
| R | An admin denies having created a namespace, revoked a user or erased content | admin audit trail | every namespace/ACL/revoke action writes one `admin.*` audit row, metadata only (`account/admin.py` module docstring L46-49); erase actions are covered by `erasure_log`/`audit_log` instead (Flow 7 R), deliberately not duplicated (`account/admin.py` module docstring L71-78, "a second, admin-scoped audit row here would only duplicate it") | — | Low |
| I | An admin reads a user's personal namespace without the user's knowledge, or an app role gains it through `Memory.Admin` alone | personal memory, the central enterprise privacy guarantee | `Memory.Admin` "grants no content access" by itself (ADR-0008 Decision, permission matrix note); break-glass is the only path to a personal namespace for an admin, and reading under a grant happens **only** through a read-only viewer in the admin area — never from the MCP surface, so a grant can never be used from Claude or any other client (ADR-0008 addendum 2026-10-08, "Reading under a grant"); every such view is audited; the user is shown a banner on `/account` and gets a `reference` note in their own `me` namespace with the same facts (same addendum) | Implemented on `main` (WP-26): `account/break_glass.py` (request/approve/deny/revoke, each re-checked a second time by the `mm_break_glass_*` `SECURITY DEFINER` functions in `db/migrations/0021_break_glass_workflow.sql`); the four-eyes approver count travels from `config.py::break_glass_approvers_from_env` (`BREAK_GLASS_APPROVERS`) into both `_authorize_break_glass_form`'s Python-side refusal and `mm_break_glass_approve`'s own `p_approver_count` argument, so SQL re-checks the self-approval rule *given* that count (residual risk below); `account/break_glass_viewer.py` (`VIEW_PATH`/`NOTE_PATH`, `GET`-only, reading under `db.rls.request_identity(..., break_glass=grant.id)` and never through `db.rls.request_connection`, the MCP path's own seam); `account/break_glass_notice.py` (the banner and the `reference` note). The RLS session variable `app.break_glass` is set by `db/rls.py::request_identity` (`break_glass` parameter L228, `set_config` call L257-259) | SQL's four-eyes check is not an independent computation of the approver count itself — `mm_break_glass_approve` trusts the `p_approver_count` the app passes in rather than reading `BREAK_GLASS_APPROVERS` on its own; a bug or compromise in `account/break_glass.py` that always passed `1` would let a single admin self-approve, even though the SQL function still refuses a same-admin approval *for the count it was given*. Not the same two-independent-sources shape as the namespace/ACL RLS checks (row E below) | Medium, accepted with that one call-out |
| D | An admin action (erase, create namespace) is used to flood the audit log or exhaust storage | availability | admin routes are gated the same as any other write path: CSRF plus the per-session rate limiting `http.py` already applies to every route class (`security-review.md` LLM10, unchanged) | no admin-specific rate limit beyond the general one — accepted, since `Memory.Admin` is already the most-trusted non-owner role and abuse is attributable (R row above) | Low, accepted |
| E | A non-admin session reaches an admin route or action | admin area | `Memory.Admin in session.roles` is checked in Python *before* any database call (`account/admin.py` module docstring L10-12, `_authorize_admin_form`); every mutating `mm_admin_*` SQL function independently re-checks `'Memory.Admin' = any(app.roles)` under the caller's own switched identity, never the owner's (`account/admin.py` module docstring L21-35) — the same "enforced twice" shape as every other permission check in this project | the "revoke access" and "erase" actions deliberately run against `services.pool`/the owner connection respectively rather than through a `mm_admin_*` function (documented reasons in `account/admin.py` module docstring L51-89) — `_authorize_admin_form` is their *only* gate; a bug there would not be caught by a second SQL-side check the way namespace/ACL actions are | Medium, accepted with the one call-out that these two actions have a single enforcement layer, not two |

## Explicit residual risks

Collected from the tables above, as the issue's Implementation checklist asks:

- **Conditional Access / Entra role removal lag** (Flow 4 E, Flow 5 E):
  effective only at the user's next Entra login, bounded above by
  `ENTRA_MAX_SESSION` (default 12 h). Mitigated by an admin revoking the
  user's sessions and tokens immediately on `/account`
  (`account/admin.py`'s "revoke access", #235).
- **Graph application permissions** `User.Read.All` + `GroupMember.Read.All`
  with tenant-admin consent (ADR-0006 addendum 2026-10-08) — a manual
  operator step this server cannot verify was actually granted beyond the
  calls it makes succeeding or failing at runtime.
- **Graph outage during a refresh** yields a retryable `503
  temporarily_unavailable`; access is never granted without the check
  (`auth/login_entra.py::check_refresh`, Flow 4 D/E).
- **Note content as a prompt-injection vector**: accepted and unchanged from
  [`docs/security-review.md`](../security-review.md) A-03 — the "note
  content is data, not instructions" sentence is the only lever available
  short of refusing to return content at all.
- **Backup horizon for erased data**: default 30 d retention + 7 d
  (ADR-0007 §3 addendum) before an `erasure_log` replay target is the only
  remaining record of an erasure (Flow 7 R).
- **Break-glass's four-eyes approver count is not independently sourced in
  SQL** (Flow 8 I): `mm_break_glass_approve` enforces the self-approval rule
  against the `p_approver_count` it is given, not against its own read of
  `BREAK_GLASS_APPROVERS` — a bug or compromise in `account/break_glass.py`
  (`config.py::break_glass_approvers_from_env`) that passed the wrong count
  would defeat the control despite the SQL-side check still running.

## Supply chain: the published image's attack surface

The published container image installs both the `otel` and the `valkey`
extras unconditionally, so one image serves single- and multi-replica,
observability-enabled and plain deployments alike (ADR-0009 addenda
2026-10-07 and 2026-10-08). **Accepted trade-off:** a single-user deployment
that needs neither Valkey nor OTLP export still ships `redis-py` and the
OpenTelemetry SDK/exporter in its image, a larger dependency surface than
that deployment uses. Both extras are off at runtime unless their respective
environment variable (`VALKEY_URL`, `OTEL_EXPORTER_OTLP_ENDPOINT`) is set, and
both are already covered by Dependabot
(`security-review.md` LLM03, unchanged ecosystem list).

## Out of scope / extension points

This document's structure — one flow, six STRIDE categories, a mitigation
pointer or a residual risk per cell — is meant to extend, not to be rewritten
for, F-02. When [#203](https://github.com/scramb/memory-manager/issues/203)
lands Open WebUI identity ([ADR-0011](../adr/0011-openwebui-identity.md)),
personal tokens ([ADR-0012](../adr/0012-personal-tokens.md)) and agent
runtimes ([ADR-0013](../adr/0013-agent-identity.md),
[ADR-0014](../adr/0014-agent-integration-tier.md)), each adds its own flow(s)
to the diagram above — an Open WebUI filter/tool boundary, a personal-token
self-issuance flow on `/account`, and an agent's own identity/namespace with
its server-side write-approval queue — rather than reopening the flows this
document already covers. Pre-registered OAuth clients for DCR/CIMD-less
clients ([ADR-0015](../adr/0015-preregistered-oauth-clients.md)) extend Flow
1's Spoofing/Tampering rows with one more client-registration path, not a new
flow. The vector index layout ([ADR-0016](../adr/0016-vector-index.md)) is a
performance/correctness decision inside Flow 2, not a new trust boundary.
