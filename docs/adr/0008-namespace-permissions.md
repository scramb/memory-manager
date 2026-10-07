# ADR-0008 — Namespaces and permissions: personal, group, project and org memory, enforced twice

Status: Accepted · Date: 2026-10-07
Relates to: auth, mcp, index, F-xx Enterprise Scale; [ADR-0005](./0005-note-format.md), [ADR-0006](./0006-enterprise-auth-entra.md), [ADR-0007](./0007-storage-backend.md)

## Context

Enterprise mode needs four kinds of namespace with different owners: personal (one Entra user), group (one Entra group), project (an explicit member list) and org (everyone). Today a namespace is a flat string from `^[a-z0-9][a-z0-9-]{0,39}$`. It is the first segment of every note path `<namespace>/<type>/<slug>.md` in the MCP tool contract. A token carries one flat list of namespaces that gates read and write alike (`mcp/authz.py`).

Constraints:
- The tool contract must not break. `path` stays `<namespace>/<type>/<slug>.md`; only additive parameters, fields or tools are allowed.
- An Entra `oid` is 36 characters. `user:<oid>` fits neither the namespace charset nor a path a human or Claude can read.
- Admins must not read personal memory by default. Break-glass access requires a reason and an audit entry, and optionally a second approver.
- Enforcement must happen in the database (RLS) **and** in application code, with tests showing that a forgotten filter leaks nothing.
- Entra app roles are tenant-wide (`Memory.User`, `Memory.Curator`, `Memory.Admin`), not per group.

## Options — addressing

### A1 — Type-prefixed internal IDs in paths (`user:<oid>/fact/x.md`)
Pro: self-describing · Con: breaks the namespace charset and every path regex; leaks object IDs to Claude; unreadable.

### A2 — Registry with readable aliases; `me` resolves per caller
A `namespaces` table holds `(id, kind, external_key, alias)`. `kind` ∈ `user|group|project|org`. `external_key` is the oid, the Entra group ID or the project slug.
- The path segment stays a charset-conformant alias.
- `me` always means "the caller's personal namespace".
- Group and project namespaces get an alias when an admin creates them (`payments`, `proj-atlas`). `org` is reserved.

Pro: no contract change; paths stay readable; personal paths never contain another user's identity · Con: one registry table and an admin step to create group and project namespaces. Groups are not auto-mirrored, which is intended: thousands of Entra groups should not become namespaces.

## Options — enforcement in the database

### R1 — App passes the computed namespace set (`SET LOCAL app.read_ns = '{…}'`), the policy checks `namespace_id = ANY(...)`
Pro: fastest plan · Con: a bug in computing the set is not caught. Only forgotten `WHERE` clauses are.

### R2 — App passes only the identity; the policy derives access from membership tables
Per transaction the app sets `SET LOCAL app.oid`, `app.roles` and `app.break_glass`. A `STABLE SECURITY DEFINER` function resolves the readable and writable namespace IDs from `namespaces`, `user_groups` (ADR-0006 cache), `project_members` and the break-glass grants. Policies compare against it.
Pro: **two independent computations** of access (Python and SQL) that must agree; a wrong set in Python is still stopped · Con: the policy function must stay fast. Policies call it as `(select mm_readable_ns())` so it runs once per statement as an InitPlan, not once per row. It is security-critical SQL (pinned `search_path`, `REVOKE … FROM PUBLIC`). Research §3 measured R1 under HNSW; R2's cost is not yet measured, and the load test decides. If R2 misses the budget, R1 is the fallback, with the Python computation tested against the membership tables.

## Decision

**A2 + R2**, accepted by the owner on 2026-10-07. The owner approved `memory_promote`, `namespace_kind` and the `/account` page, set the break-glass default to two admins, and decided that administration happens in an admin area on `/account`, not through a CLI. Permission matrix:

| Namespace | Read | Write (create/edit/supersede) | Curate (archive, move, edit others' notes structurally) |
|---|---|---|---|
| `me` (user) | the user | the user | the user |
| group | members of the Entra group | members (default) or curators only, per-namespace setting | `Memory.Curator` **and** member |
| project | members: listed groups and users | `readers` or `writers` list, per-namespace setting | project owners, or `Memory.Curator` who is a member |
| `org` | every user with a memory role | `Memory.Curator`, `Memory.Admin` | `Memory.Admin` |

- **`Memory.Admin` manages namespaces and ACLs. It grants no content access** to personal namespaces, and none to group or project namespaces without membership.
- **Break-glass:** an admin requests read-only access to one personal namespace with a reason. By default (`BREAK_GLASS_APPROVERS=2`) a second admin must approve; operators may lower it to 1. The grant expires after 1 h. Request, approval and every read are audited, and the user is notified on their next session (instructions note).
- **Default write target is `me`.** Server instructions and the `memory_guide` prompt say so. A path in a shared namespace is written only when the caller names it explicitly and has write access.
- **`memory_promote(path, target_namespace, if_version, keep_original=false)`** is a new tool, which is additive.
  - It copies a note from `me` into a shared namespace with a new `id` and `supersedes` set to the original.
  - By default it archives the personal original.
  - It needs write access to the target and records an audit entry with both paths.
- **Search** covers every readable namespace by default, as today. Every result item gets a new field `namespace_kind` (`personal|group|project|org`), which is additive. The path already carries the alias.
- **Git backend:** unchanged. Namespaces stay plain strings and the token namespace list from ADR-0004 keeps working. `me` and the registry exist only with `storage.backend=postgres`.
- **Self-service:** a small server-rendered `/account` page (login via the facade, same templates as the login interstitial). It shows a note count, "export my memory" (Markdown ZIP of `me`) and "delete my memory" (typed confirmation, hard delete per [ADR-0007](./0007-storage-backend.md)).
- **Deprovisioned users:** their personal namespace is frozen. It is hard-deleted after `PERSONAL_RETENTION_DAYS` (default 30). Handover to a successor is opt-in and needs the break-glass approval flow.
- **Tests:**
  - A generated matrix of role × namespace kind × action, including negative cases.
  - An RLS suite that runs raw `SELECT`, `UPDATE` and `DELETE` without any `WHERE` as the application role and expects only the caller's rows.
  - A test that the application role has neither `BYPASSRLS` nor table ownership.

Checked against the guardrails:
- Few dependencies: none new.
- OSS first: yes.
- Container: no extra service.
- Technology pool: SQL and Python per ADR-0001.

## Consequences

- New schema: `namespaces`, `project_members`, `namespace_settings`, `break_glass_grants`. The application connects as a non-owner role. Migrations run as the owner role.
- New tool `memory_promote`, new result field `namespace_kind`, new page `/account` with an admin area (only `Memory.Admin`) for namespaces, ACLs, erasure and break-glass requests and approvals. All additive and audited.
- The RLS function is on the hot path of every query. Its cost is measured in the load test, and the indexes on membership tables are part of the migration.
- RLS protects against missing filters, not against SQL injection that sets session variables. Parameterised queries stay mandatory (they already are, via asyncpg).

## Reversibility

Expensive once data exists. Aliases and the registry end up in exports, audit entries and users' habits. RLS policies themselves are cheap to change.

## Addendum 2026-10-07 — system identity under FORCE RLS (#100)

ADR-0008 did not say how trusted cross-namespace paths reach the data once every content table has `ENABLE` + `FORCE ROW LEVEL SECURITY`: the Git-mode index, `reindex --full`, and later the worker, erasure and the Git-to-Postgres import. The owner decided on 2026-10-07:

- **The owner role is the system identity.** Migration `0005_rls.sql` adds an explicit owner-only policy (`TO` the migrating role, `USING`/`WITH CHECK` true) on each content table, next to the identity policies. Git mode and system jobs keep connecting as the owner and are unchanged.
- **Request transactions in Postgres mode switch roles.** Each one switches to a non-owner, non-`BYPASSRLS` `NOLOGIN` role with `set_config('role', …, true)` and sets the identity in the same transaction. Both happen in one helper. The operator creates that role and grants it to the owner; there is still a single `DATABASE_URL`.
- **The bypass is tied to the owner credential, not to a setting any code can flip.** Request code must never use a connection that has not switched roles. #101 closes this structurally by pinning the request path to the helper, and a test enforces it.

Rejected: requiring a session marker such as `app.system = 'on'` in addition to ownership. A forgotten marker fails closed, but the bypass becomes a settable GUC, every Git-mode pool creation site has to change, and a missing marker silently empties search in Git mode.
