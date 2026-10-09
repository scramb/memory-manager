# Data Protection Impact Assessment (Art. 35 GDPR) — template

> **Not legal advice.** This is a template for an operator's own GDPR Art. 35 Data Protection
> Impact Assessment of a `memory-manager` deployment. It describes what this codebase actually
> does and the risks [`docs/security/threat-model.md`](../security/threat-model.md) already found
> for them - verified against the code, the ADRs and that threat model, not invented. It does not
> decide whether a DPIA is *required* for your deployment (Art. 35(1), or your supervisory
> authority's own list under Art. 35(4)), whether this assessment is complete for your
> organisation, or what residual risk is acceptable to you. See [`README.md`](./README.md) for
> how to use this set of templates, and have the filled-in result reviewed by someone qualified to
> give legal advice for your jurisdiction and your deployment before relying on it or submitting
> it to a supervisory authority.

## Scope

One `memory-manager` enterprise deployment (`STORAGE_BACKEND=postgres`, Entra ID login,
[ADR-0006](../adr/0006-enterprise-auth-entra.md)-[ADR-0009](../adr/0009-stateless-replicas.md)).
The default `git` storage backend is out of scope here - it has no central registry, no
break-glass and no server-side erasure (see [`deletion-concept.md`](./deletion-concept.md)
"Git-backend limitation"), so its own risk profile is smaller and mostly operator-local (who has
push access to the Git remote). An operator running only the `git` backend may still find the
"Necessity and proportionality" section below useful, but the risk table is written for the
enterprise profile's own new boundaries (Entra login, Graph, RLS registry, break-glass,
deprovisioning, erasure).

This DPIA follows the four elements Art. 35(7) requires. It builds on, rather than re-derives:

- [`data-flow.md`](./data-flow.md) for *what* personal data is processed, for which purpose, and
  who receives it (feeds (a) below).
- [`docs/security/threat-model.md`](../security/threat-model.md) for *how* each boundary can fail
  (STRIDE per flow, with a mitigation or an accepted residual risk per cell) - this DPIA's risk
  table is that document's own findings, re-framed by risk to the data subject rather than by
  attack category (feeds (c) below).
- [`toms.md`](./toms.md) for the measures already in place (feeds (d) below).

## (a) Systematic description of the processing and its purposes

See [`data-flow.md`](./data-flow.md) in full: "Categories of personal data processed",
"Purposes", "Recipients and processors" and the flow diagram. Do not duplicate that mapping here;
fill in this DPIA's own fields below once that one is filled in for your deployment.

- **Nature of the processing:** storing and retrieving notes on behalf of a user or team
  (personal and shared memory), authenticating users through the operator's own Entra ID tenant,
  authorizing access by role and namespace, deprovisioning departed employees, and (enterprise)
  erasing personal data on request or after retention.
- **Scope:** `<number of employees/data subjects in scope - e.g. the operator's tenant's ~2,000
  users, per ADR-0006 Context>`; `<which of the four namespace kinds your organisation actually
  uses - me/group/project/org, see roles-and-permissions.md>`.
- **Context:** `<is this deployment mandatory for employees, or opt-in? what does an employee
  reasonably expect given how it was introduced - this affects both the risk assessment below and
  the transparency-notice.md content>`.

## (b) Assessment of necessity and proportionality

- **Lawful basis per purpose:** see `data-flow.md`'s "Purposes" table, "Lawful basis" column -
  fill in there, not duplicated here.
- **Data minimisation already built in** (cite, don't re-derive - see `toms.md` "Art. 25"):
  default write target is the caller's own `me` namespace (ADR-0008); `Memory.Admin` grants no
  content access by itself; quotas, the content blocklist, OTel export and the SIEM export are
  off unless an operator opts in; audit/erasure log entries never carry note content
  (`audit.DETAIL_ALLOWLIST`).
- **Storage limitation:** a deprovisioned user's personal namespace is hard-deleted after
  `PERSONAL_RETENTION_DAYS` (default 30 days) without further action - see
  `deletion-concept.md`. `<your own retention decision for active users' note content, which this
  server does not time-box on its own - no automatic deletion while an account is active, by
  design (the memory is the point)>`.
- **Could the purpose be achieved with less data?** `<operator's own assessment - e.g. could
  group-level memory substitute for some personal-namespace use; is the embedding provider
  necessary or does `EMBEDDING_PROVIDER=ollama`/`none` suffice for your retrieval-quality
  requirements, keeping chunk text server-side instead of sending it to an external API>`.
- **Alternatives considered:** `<e.g. the git backend instead of postgres, if Entra integration
  and break-glass are not needed for your deployment size - see ADR-0007>`.

## (c) Risks to the rights and freedoms of data subjects

Pre-filled from [`docs/security/threat-model.md`](../security/threat-model.md): every row below
cites the flow and STRIDE category it comes from. **Likelihood** and **Severity** repeat that
document's own qualitative rating; **Risk to the data subject** re-frames the threat-model's
"Asset"/"Residual risk" columns in terms of the right or interest at stake, which the threat model
itself does not phrase in GDPR terms. Add rows for anything specific to your own deployment (a
custom embedding provider, a non-default `BLOCKLIST_FILE`, a lowered `BREAK_GLASS_APPROVERS`,
...).

| # | Risk to the data subject | Source (threat model) | Likelihood | Severity | Mitigation (toms.md) | Residual risk | Accepted by / date |
|---|---|---|---|---|---|---|---|
| 1 | A disabled or deprovisioned employee's account keeps access to their own and others' memory for up to `ENTRA_MAX_SESSION` (default 12 h) after an Entra role/Conditional-Access change, because the per-refresh check deliberately does not re-read `appRoleAssignments` | Flow 4 E / Flow 5 E, "Explicit residual risks" | Low–Medium (requires a role change during an active session) | Medium | "Revoke access" lets a `Memory.Admin` end sessions/tokens immediately (`account/admin.py::_revoke_user`, WP-26); `ENTRA_MAX_SESSION` configurable | Accepted by the project; an operator who needs a tighter bound lowers `ENTRA_MAX_SESSION` and trains admins to use "Revoke access" for urgent offboarding | `<your sign-off>` |
| 2 | An admin reads an employee's personal memory without their prior knowledge via break-glass | Flow 8 I | Low (gated by request + a second admin's approval by default) | Medium (personal namespace is the most sensitive asset - "may include personal facts the user chose to record") | Four-eyes approval by default (`BREAK_GLASS_APPROVERS=2`), 1 h expiry, read-only, never reachable from the MCP surface, every request/approval/read audited, the affected user shown a banner and a `reference` note (ADR-0008 addendum "Break-glass notification") | Implemented on `main` (WP-26); the approver count itself is passed from the app into `mm_break_glass_approve` rather than read independently by SQL (threat model Flow 8 I's own call-out); an operator who lowers `BREAK_GLASS_APPROVERS` to 1 removes the four-eyes control and should record why | `<your sign-off>` |
| 3 | The "revoke access"/"erase" admin actions are gated by a single Python-side authorization check (`_authorize_admin_form`), not independently re-checked in SQL the way namespace/ACL actions are | Flow 8 E | Low | Medium | Same CSRF + session-role check every mutating `/account` route uses; attributable via the `admin.*` audit row | Accepted - a bug in that one gate would not be caught by a second layer the way other admin actions are; the threat model names this explicitly rather than claiming parity it does not have | `<your sign-off>` |
| 4 | Note content (which may include personal facts) is sent to an external, operator-chosen embedding API as part of indexing | Flow 6 I, `docs/security-review.md` A-02 | High, if `EMBEDDING_PROVIDER` is set to an external API | Informational–Low per occurrence, but the recurring exposure is the point of the risk | Chunk text only, batched, never a full note in one call; `ollama`/`none` keep every byte on the operator's own infrastructure | Accepted by whichever operator chooses an external `EMBEDDING_PROVIDER` - this is an explicit, documented choice (`SECURITY.md` "Security properties"), not a default | `<name your provider and whether you accept this risk, or set EMBEDDING_PROVIDER=ollama/none>` |
| 5 | A departed employee's personal data outlives their employment for up to `PERSONAL_RETENTION_DAYS` (default 30 days) plus the backup horizon (default 37 days: retention + 7 days) before it is unrecoverable even via a backup restore | Flow 2 I, `deletion-concept.md` "Backup horizon" | Low (requires both a retention window and a restore within it) | Low | Deprovisioning is detected automatically (Graph delta sync, WP-24); the retention job (WP-26) then hard-deletes without further admin action; every erasure is exported for SIEM-based replay after a restore | Accepted; an operator who needs a shorter window lowers `PERSONAL_RETENTION_DAYS` and/or the backup retention policy | `<your sign-off>` |
| 6 | Microsoft Graph application permissions (`User.Read.All`, `GroupMember.Read.All`, tenant-admin consent) let this server's app registration read every tenant user's basic profile and group membership, not only this deployment's own users | Flow 5 T, "Explicit residual risks" | Low (requires compromise of the app registration or its client secret) | Medium (scope of data reachable if compromised exceeds this deployment's own user base) | Least-privileged pair chosen deliberately over the broader `Directory.Read.All`; client secret handling per `toms.md` "Pseudonymisation and encryption" | Accepted - this is inherent to app-only Graph access for a tenant-wide deprovisioning/group check; an operator records who granted consent and when (`toms.md` "Microsoft Graph application permissions") | `<your sign-off>` |
| 7 | An operator holding direct database credentials (the owner role) can read any employee's personal memory, bypassing every RLS policy and break-glass control above | `roles-and-permissions.md` "Operator access to the database", threat model trust boundary 4 | Depends entirely on the operator's own key management | High if it happens (full bypass of every content control in this document) | None from this server - RLS protects the application's own request path, not a database session that already holds the credentials | Accepted as an inherent limit, same class as any self-hosted database; operator records who holds this credential and whether its use is audited outside this server (`roles-and-permissions.md`'s own fill-in) | `<your sign-off>` |
| 8 | `<your own additional risk>` | `<...>` | `<...>` | `<...>` | `<...>` | `<...>` | `<...>` |

## (d) Measures to address the risks, and overall assessment

- **Measures already in place:** see [`toms.md`](./toms.md) in full - every row there maps to a
  concrete control and its config key or code path, so a reviewer can check it against the running
  deployment.
- **Residual risk after measures:** see the "Residual risk" column above; none is rated higher
  than Medium in the project's own threat model, and each Medium row has either an immediate
  manual remedy (row 1), a re-verification obligation (row 2), or is an inherent, accepted
  limitation of self-hosted operation (row 6, row 7).
- **Overall conclusion:** `<operator's own conclusion - does this deployment proceed as planned,
  with additional safeguards, or not at all? Record the reasoning, not only the outcome>`.
- **Consultation:** `<did you consult your Data Protection Officer, your works council (see
  germany.md if applicable), or data subjects/their representatives? Record who and when>`.
- **Prior consultation with the supervisory authority (Art. 36):** required only if a residual
  risk above remains high after mitigation and you cannot otherwise reduce it - `<your own
  assessment; none of the pre-filled rows above are rated High residual, but your own additions
  might be>`.

## Sign-off

| Field | Value |
|---|---|
| Completed by | `<name, role>` |
| Date completed | `<YYYY-MM-DD>` |
| Reviewed by (DPO / legal) | `<name, role, date>` |
| Approved by (controller) | `<name, role, date>` |
| Next scheduled review | `<date - e.g. on the next major version, or annually, whichever comes first>` |

## Not included

- A pen test of the risks above - [`docs/security/pentest-checklist.md`](../security/pentest-checklist.md)
  turns every threat-model threat of severity medium or higher into an executable test case;
  running it is tracked separately from this assessment.
- Legal review of this template, or a determination of whether Art. 35(1) requires a DPIA for your
  specific deployment.
