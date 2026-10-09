# Roles and permissions (template)

> **Not legal advice.** This is a template mapping of who may read, write, curate or administer
> which memory in a `memory-manager` deployment - verified against the code, the ADRs and
> [`docs/security/threat-model.md`](../security/threat-model.md), not invented. It does not
> decide whether this access model satisfies your own organisation's data-minimisation or
> least-privilege policy; that is the controller's own responsibility. See
> [`README.md`](./README.md) for how to use this set of templates, and have the filled-in result
> reviewed by someone qualified to give legal advice for your jurisdiction and your deployment.

## Role matrix: namespace kind × action

Enterprise deployment only (`STORAGE_BACKEND=postgres`, Entra ID login,
[ADR-0008](../adr/0008-namespace-permissions.md) "A2 + R2"). Independently computed and enforced
twice - once in application code (`mcp/namespaces.py::resolve`, a `Resolution`'s own
`readable`/`writable`/`can_curate`) and once in Postgres Row-Level Security
(`db/migrations/0005_rls.sql`'s `mm_readable_ns()`/`mm_writable_ns()`) - so a bug on either side
is caught by the other; the two disagreeing fails closed (CLAUDE.md "enforced twice... fails
closed").

| Namespace kind | Read | Write (create/edit/supersede) | Curate (archive, move, edit others' notes structurally) | Admin (create/rename the namespace, manage its ACL) |
|---|---|---|---|---|
| `me` (personal) | the user themselves | the user themselves | the user themselves | n/a - has no admin surface of its own |
| `group` | every member of the backing Entra group | members (default, `group_write='members'`) or `Memory.Curator` members only, per the namespace's own setting (`namespace_settings.group_write`) | a `Memory.Curator` **and** a member (ADR-0008 addendum "curate requires write") | `Memory.Admin`, via the admin area |
| `project` | the namespace's own listed `project_members` (user or group principals) | principals with role `writer`/`owner` (default, `project_write='writers'`), or every listed reader when the setting is `'readers'` | a project `owner`, or a `Memory.Curator` who is also a listed member | `Memory.Admin`, via the admin area |
| `org` | every user who holds any `Memory.*` app role at all | `Memory.Curator` or `Memory.Admin` | `Memory.Curator` or `Memory.Admin` (curate requires write, and both already have it) | `Memory.Admin`, via the admin area |

`Memory.Admin` grants **no content access by itself** - it manages namespaces and ACLs
(`account/admin.py`'s six `mm_admin_*`-backed routes: create/rename a namespace, add/remove a
project member, update group/project write settings, list namespaces), and reading a user's
`me` namespace as an admin requires the separate, audited break-glass grant below - never a
namespace/ACL admin action, and never an MCP call (ADR-0008 Decision, "`Memory.Admin`... grants
no content access by itself").

## How roles are assigned and removed

- **Assignment**: `Memory.User`, `Memory.Curator` and `Memory.Admin` are Entra **app roles**,
  assigned through the Entra app role assignment UI (or, for a disposable test tenant,
  `deploy/entra/`'s OpenTofu module provisions the app registration and its roles; granting the
  Microsoft Graph application permissions the facade needs still requires a one-time, manual
  tenant-admin consent click, `docs/guides/enterprise-operations.md` "1. Register the Entra app").
  Group membership (for the `group`/`project` rows above) comes from the user's **Entra group**
  membership, resolved server-side through `getMemberGroups` (ADR-0006 §4) and cached in
  `user_groups` with a TTL - never trusted from a client-supplied claim.
- **Removal, normal path**: removing an app role assignment or a group membership in Entra does
  **not** cut the user off immediately. The facade's refresh re-check only re-reads
  `accountEnabled`, existence and group membership from Graph - never `appRoleAssignments`, which
  would need the broader `Directory.Read.All` permission this project deliberately does not
  request (ADR-0006 addendum 2026-10-08). A removed role or group therefore takes effect at the
  user's **next Entra login**, bounded above by `ENTRA_MAX_SESSION` (default 12 h,
  `auth/login_entra.py::check_refresh`). Operators must align `ENTRA_MAX_SESSION` with their own
  tenant's Conditional Access sign-in frequency policy.
- **Removal, immediate**: when access must end right away rather than waiting for the next
  login - an incident, an offboarding that cannot wait - a `Memory.Admin` uses **"Revoke
  access"** in the admin area on `/account` (`account/admin.py::_revoke_user`,
  `REVOKE_USER_PATH = "/account/admin/users/revoke"`, **WP-26**): it revokes every OAuth token
  family and static token the user owns (`auth/users.py::revoke_all_credentials`, reused from the
  WP-24 deprovisioning flow) and ends every `/account` browser session of theirs
  (`account/sessions.py::revoke_all_for_oid`), in one audited `admin.user.revoke` call. This does
  **not** disable the account or touch the user's notes - the user can sign back in right away
  once the underlying reason is resolved. Actually disabling the account (`users.disabled_at`) is
  Entra's own job, detected by `worker.py::_entra_delta_sync_job` → `auth/users.py::disable_user`
  (ADR-0006 §6, WP-24, already on `main`), independent of any admin action.

## Break-glass: an admin reading a user's personal namespace

The **only** path for a `Memory.Admin` to read another user's `me` namespace, implemented on
`wp/26-admin-erasure` **(WP-26, PR open, not yet merged to `main`)**:

1. **Request**: an admin names the target user's Entra `oid` and a reason
   (`account/break_glass.py`'s `REQUEST_PATH`, backed by `mm_break_glass_request` -
   `db/migrations/0021_break_glass_workflow.sql` - which resolves the `oid` to a personal
   namespace itself and refuses if the person has never used `memory-manager` at all).
2. **Approval (four-eyes by default)**: a **second**, different `Memory.Admin` approves
   (`APPROVE_PATH`, `mm_break_glass_approve`); the approver count
   (`BREAK_GLASS_APPROVERS`, default 2, `config.py::break_glass_approvers_from_env`) is checked
   independently in Python (`_authorize_break_glass_form`, before any database call) and again by
   the SQL function itself, so a bug in either alone cannot let a self-approval through. An
   operator may lower `BREAK_GLASS_APPROVERS` to 1.
3. **Expiry**: an approved grant is valid for **1 hour** from approval
   (`break_glass_grants.expires_at`); after that, every read is refused the same as a revoked or
   denied one.
4. **Reading under the grant**: only through `account/break_glass_viewer.py`'s two `GET`-only
   routes (`VIEW_PATH` lists the namespace's notes by path/type/title/updated,
   `NOTE_PATH` renders one note's body, `html.escape`d, never interpreted) - **never** from the
   MCP surface, so a grant can never be used from Claude or any other MCP client
   (`db.rls.request_identity(..., break_glass=grant.id)` is called directly here; the MCP request
   path's own seam, `db.rls.request_connection`, never passes a `break_glass` value at all,
   `tests/account/test_break_glass_viewer.py::test_mcp_read_path_never_passes_break_glass`). The
   viewer has no write route at all (`test_no_write_route_exists`) - break-glass never grants
   write, only read.
5. **Audit trail**: the request, the approval/denial and every individual list/note view are
   each written as one `admin.break_glass.*`/`break_glass.read` audit row, metadata only (grant
   id, target oid, reason, and for a note view its path - never its content).
6. **User notification**: once approved, `account/break_glass_notice.py` writes a `reference`
   note with the facts (requester, approver, reason, time, expiry) into the target user's own
   `me` namespace, as the system identity, and shows the same facts as a banner on `/account`
   until the user acknowledges it - so the affected user always finds out, even if they never
   visit `/account` proactively.
7. **Revocation**: any admin can end a live grant early (`REVOKE_PATH`, `mm_break_glass_revoke`),
   audited the same way.

The RLS column these functions key off, `app.break_glass`, already exists on `main`
(`db/rls.py:166,219-220`); the request/approval/viewer/notification workflow itself must be
re-verified against the merged implementation once `wp/26-admin-erasure` lands
(cross-reference: [`docs/security/pentest-checklist.md`](../security/pentest-checklist.md) area E,
cases BG-1 through BG-5).

## Operator access to the database

An operator with direct `DATABASE_URL`/superuser access to the Postgres cluster (the owner role,
or a genuine superuser) bypasses every row-level-security policy above entirely - RLS protects
the application's own request path, not someone who already holds the database credentials
outright (`docs/security/threat-model.md` trust boundary 4; `docs/compliance/toms.md` "SQL
injection protection" residual-risk note, same class of risk). This server does not and cannot
restrict what a trusted operator role can see once connected directly; the migrations themselves
run as that owner role (`db/rls.py::grant_app_role`'s own docstring, "Migrations run as the owner
role"). `<name who on your side holds this credential, how it is stored/rotated, and whether its
use is itself audited outside this server (e.g. your own Postgres `pgaudit`/cloud-provider audit
log) - this server's own `audit_log` table covers only requests that went through the
application, never a direct database session>`.

## Not included

- A Data Protection Impact Assessment, a transparency notice and a Germany-specific section
  (#276).
- The `agent` principal kind ([ADR-0013](../adr/0013-agent-identity.md)) - not yet part of the
  role matrix above; this document covers `Memory.User`/`Memory.Curator`/`Memory.Admin` and the
  namespace kinds `user`/`group`/`project`/`org` only.
- Legal review of this template.
