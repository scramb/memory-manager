# Technical and organisational measures (Art. 32 GDPR) and privacy-by-default (Art. 25)

> **Not legal advice.** This is a template mapping of Art. 32 "appropriate technical and
> organisational measures" and Art. 25 "data protection by design and by default" to the
> controls this codebase actually implements - each row names the config key or the file/line it
> lives in, so it can be checked against the running deployment rather than trusted on its own.
> It does not decide whether these measures are *sufficient* for your risk assessment; that is
> the controller's own responsibility. See [`README.md`](./README.md) for how to use it, and have
> the filled-in result reviewed by someone qualified to give legal advice for your jurisdiction.

Every control below is unchanged from, or an enterprise-only addition to,
[`docs/security-review.md`](../security-review.md) (code-level review of v0.1) and
[`docs/security/threat-model.md`](../security/threat-model.md) (STRIDE review of the enterprise
additions). Where a row is marked **(residual risk, accepted)**, the linked document explains the
trade-off and why it was accepted rather than closed further.

## Art. 32(1)(a) - Pseudonymisation and encryption

| Measure | Control | Config key / code path |
|---|---|---|
| Bearer tokens stored as a hash, never plaintext | Static and OAuth tokens: SHA-256 of 256 bits of entropy, plaintext returned once at creation and never again | `auth/tokens.py` (`create_token`), `docs/security-review.md` ASVS "Authentication" |
| `/account` session ids stored as a hash | SHA-256 of the session id; the raw id lives only in the `HttpOnly`/`Secure`/`SameSite=Strict` cookie | `account/sessions.py::_hash_session_id` |
| OAuth client secrets encrypted at rest | Registered OAuth clients' `client_info` encrypted with Fernet (authenticated symmetric encryption), keyed by an operator-supplied key | `OAUTH_CLIENT_SECRET_KEY`, `auth/store.py::ClientSecretCipher` |
| Admin password hashed, not stored in clear | argon2id, verified in constant time | `auth/login_password.py` |
| Transport encryption | This process does not terminate TLS itself; every documented deployment shape (Compose/Helm) puts a reverse proxy or ingress in front that does | `docs/security-review.md` ASVS "TLS/transport" (explicitly out of that review's scope - operator-provided); `<name your own TLS termination point and certificate issuer>` |
| Secrets never logged or echoed back | No log line or audit `detail` ever carries a token, password, secret or note content; a rejected secret-scan hit names only the rule and line, never the matched text | `vault/secrets.py`, `observability/logging.py`, `audit.py`, `docs/security-review.md` LLM02 |

## Art. 32(1)(b) - Confidentiality, integrity, availability and resilience of processing systems

| Measure | Control | Config key / code path |
|---|---|---|
| Namespace access control, enforced twice | Independent computation in application code (`mcp/authz.py`) and in Postgres Row-Level Security (`mm_readable_ns()`/`mm_writable_ns()`); disagreement fails closed | ADR-0008 R2; `db/rls.py`, migration `0005_rls.sql` |
| `FORCE ROW LEVEL SECURITY` on every content table | The application connects as a non-owner, non-`BYPASSRLS` role; the owner role is the only trusted "system identity" | `db/rls.py::grant_app_role`, ADR-0008 addendum "system identity under FORCE RLS" |
| Secret scanning before every write | Every write is scanned for private keys, cloud credentials, tokens, JWTs, IBANs, high-entropy secrets before it is committed | `vault/secrets.py::check`, CLAUDE.md "secret scan before every commit" |
| Audit log for every write | Actor, client, operation, path, outcome recorded for every write - success, conflict, rejection and failure alike, metadata only | `audit.py::AuditWriter.record`, CLAUDE.md "audit log for every write" |
| Webhook authenticity | The vault webhook requires a valid GitHub/Gitea HMAC signature, compared in constant time; an unsigned request is rejected the same as a wrong one | `docs/security-review.md` ASVS "Webhook authenticity" |
| Origin / DNS-rebinding protection | A dedicated middleware checks `Origin` against an exact allowlist, independent of the SDK's own host check | `docs/security-review.md` ASVS "Origin validation" |
| Rate limiting and abuse protection | Per-token/per-write-scoped limits on every route class, shared across replicas via Postgres or Valkey | `http.py`, `auth/ratelimit.py`, `docs/security-review.md` LLM10 |
| Operator content blocklist (optional) | Rejects a write outright if it matches an operator-defined category of regexes/keywords; off by default | `BLOCKLIST_FILE`, `docs/guides/blocklist.md` |
| Write-rate and storage quotas (optional, enterprise) | Per-user/namespace/token write-rate budgets and per-namespace note-count/byte budgets; off by default | `QUOTA_WRITES_*`/`QUOTA_MAX_*`, `docs/guides/quotas.md` |
| High availability | Several stateless `api`/`worker` replicas behind a `PodDisruptionBudget`, no sticky sessions, no in-process state that breaks with more than one replica | ADR-0009; `charts/memory-manager/values-enterprise.yaml` (`api.autoscaling`, `pdb.api.minAvailable`) |
| SQL injection protection | Every query is parameterised; RLS is explicitly not a substitute for this | `docs/security-review.md` ASVS "Injection"; ADR-0008 Consequences **(residual risk, accepted: RLS protects against a missing filter, not against session-variable injection - parameterised queries stay mandatory)** |

## Art. 32(1)(c) - Ability to restore availability and access to personal data after an incident

| Measure | Control | Config key / code path |
|---|---|---|
| Scheduled backups (enterprise) | Daily `ScheduledBackup` via the Barman Cloud CNPG-I plugin, 30-day `ObjectStore` retention policy by default | `database.cnpg.backup`, `docs/guides/enterprise-operations.md` "Backups and restore drill" |
| Restore procedure (enterprise) | CloudNativePG recovers into a new `Cluster` bootstrapped from the same `ObjectStore` | `docs/guides/enterprise-operations.md` "Restoring from a backup" - **not yet exercised in this repository's own CI** (documented there as a known gap) |
| Erasure survives a restore (enterprise) | Every erasure is also exported through the audit/SIEM pipeline (`AUDIT_EXPORT`), so a restore that rolls back `erasure_log` itself does not resurrect erased data once the exported copy is replayed | `ERASURE_LOG_REPLAY_FILE`, ADR-0007 §3 addendum, `docs/guides/audit-export.md` "Erasure records and restores" - **landing with WP-26, not yet merged to `main` as of this document** |
| Index is fully rebuildable from source (git backend) | `reindex --full` rebuilds the derived Postgres index from the vault; nothing lives only in the database that is not derivable from Git | CLAUDE.md "Postgres must be fully rebuildable from the vault" |

## Art. 32(1)(d) - Regular testing, assessing and evaluating effectiveness

| Measure | Control |
|---|---|
| Code-level security review | `docs/security-review.md` - OWASP Top 10 for LLM Applications 2025 plus an ASVS-style pass, with every finding fixed or explicitly accepted with a rationale |
| Threat model and pen-test checklist | `docs/security/threat-model.md` (STRIDE per trust boundary) and `docs/security/pentest-checklist.md` (one executable test case per medium-or-higher threat) |
| Automated testing and supply-chain checks | CI runs build, lint/vet, format check, tests and a container build on every change; `uv.lock` is committed and installed with `--frozen`; Dependabot watches every ecosystem weekly; every release ships a signed image and chart plus an SBOM |
| `<...>` | `<add your own periodic review cadence, e.g. an annual re-read of this document and the threat model>` |

## Art. 25 - Data protection by design and by default

- **Default write target is the caller's own personal namespace (`me`).** A shared namespace is
  written only when the caller names it explicitly and has write access there (ADR-0008
  "Default write target is `me`").
- **`Memory.Admin` grants no content access by itself.** The admin role manages namespaces and
  ACLs; reading a user's personal memory is meant to require a separate, audited break-glass
  grant with a reason, by default a second admin's approval, a 1-hour expiry, and a read-only
  viewer that is never reachable from the MCP surface - so a grant can never be used from Claude
  or any other MCP client. This is ADR-0008's decision ("Break-glass" and its 2026-10-08
  addendum) and the approver count and config key it names there are not yet an implemented
  control - the request/approval/viewer workflow and its config key **land with #237-#239
  (ADR-0008) and are not committed code as of this document**. The RLS column it will rely on,
  `app.break_glass`, already exists on `main` (`db/rls.py`).
- **No MCP tool hard-deletes a note.** Every "delete" a client can trigger moves the note to
  `_archive/`; it is never gone (CLAUDE.md "No MCP tool hard-deletes notes"). Erasure (true
  deletion, GDPR Art. 17) is a separate, non-MCP, audited operation (`/account` self-service,
  the admin area, and the retention job) - never reachable from a write.
- **Deprovisioned users' personal data does not linger indefinitely.** A disabled or departed
  user's personal namespace is frozen and hard-deleted after `PERSONAL_RETENTION_DAYS` (default
  30 days) - **this retention job is implemented in WP-26, not yet merged to `main` as of this
  document** (ADR-0008 "Deprovisioned users").
- **Erasing a user removes personal data, not shared knowledge the team owns.** The personal
  namespace, every revision, chunk and job, and the user's identity rows are hard-deleted; notes
  the user authored in a *shared* namespace stay, with the authorship field redacted to
  `"erased"` rather than the user's `oid` being kept as a re-linkable pseudonym (ADR-0007
  addendum 2026-10-08 "erasure scope").
- **Every content-producing write is capped and scanned by default**, with no opt-out: a 16 KiB
  size cap per note, secret scanning, and (if `BLOCKLIST_FILE` is set) the operator's own content
  blocklist - all before anything is committed.
- **Collection of further personal data is opt-in, not default.** Write-rate/storage quotas, the
  operator content blocklist, OTel tracing/metrics export and the SIEM audit export are all off
  unless an operator explicitly configures them (`QUOTA_*`, `BLOCKLIST_FILE`,
  `OTEL_EXPORTER_OTLP_ENDPOINT`, `AUDIT_EXPORT`).
- **Namespace access fails closed.** A token's readable/writable namespace set is computed
  independently in Python and in SQL; if the two disagree, or if neither grants access, the
  request is denied rather than defaulting to visibility (ADR-0008 addendum "identity sources and
  curate").

## Conditional Access and Entra role-removal timing (enterprise)

Conditional Access is enforced by Entra **only at login, and again every `ENTRA_MAX_SESSION`**
(default 12 hours) when the facade's session is renewed - **not on every individual MCP call**.
A removed Entra app role takes effect only at the user's **next Entra login**, bounded above by
`ENTRA_MAX_SESSION` (ADR-0006 §5 and its 2026-10-08 addendum; `auth/login_entra.py::check_refresh`;
`docs/security/threat-model.md` Flow 4 "E"). Operators must align `ENTRA_MAX_SESSION` with their
own tenant's Conditional Access sign-in frequency policy. When a role or a user's access must be
cut off **immediately**, rather than waiting for the next login, a `Memory.Admin` can revoke that
user's sessions and tokens right away from the admin area on `/account` (`account/admin.py`'s
"revoke access" action - **implemented on `wp/26-admin-erasure`, not yet merged to `main` as of
this document**) - this is the server's mitigation for the Conditional-Access/role-removal lag,
not a substitute for it. **(Residual risk, accepted - see the threat model's Flow 4 "E" and
"Explicit residual risks" for the full reasoning.)**

## Microsoft Graph application permissions require tenant-admin consent (enterprise)

The Entra facade (ADR-0006) requires the Microsoft Graph **application permissions**
`User.Read.All` and `GroupMember.Read.All` - the least-privileged pair that still covers both the
users delta query (deprovisioning) and `getMemberGroups` (group-overage resolution); the broader
`Directory.Read.All` is deliberately not requested (ADR-0006 addendum 2026-10-08). Granting these
application permissions requires **tenant-wide admin consent, a manual step the operator must
perform once**: `deploy/entra/`'s OpenTofu module provisions the app registration and its app
roles, but the admin-consent click itself is not scripted
(`docs/guides/enterprise-operations.md` "1. Register the Entra app"). This server cannot verify
that consent was actually granted beyond its own Graph calls succeeding or failing at runtime
(`docs/security/threat-model.md` Flow 5 "T", "Explicit residual risks"). `<record the tenant admin
who granted consent, and the date, for your own audit trail>`.
