# Write quotas

Two independent, optional budgets on top of the always-on `RATE_LIMIT_*` pairs (every request
to the MCP endpoint, regardless of which tool it calls): how often a caller may write
(`QUOTA_WRITES_*`, #242), and how much a namespace may hold in total (`QUOTA_MAX_*`,
#243, Postgres mode only). Both default to off - a deployment opts in per variable.
A caller over either budget gets a `ToolError` in place of the write; nothing is ever
partially written.

## Write-rate quotas (`QUOTA_WRITES_*`)

Caps how many `memory_write`/`memory_edit`/`memory_supersede`/`memory_archive` calls one
identity may make, in a fixed per-minute and a fixed per-day window, each checked
independently. Three scopes, each its own budget:

| Scope | Keyed by | Applies to |
|---|---|---|
| `user` | the calling principal's `oid` | `"postgres"` mode only - stdio and a static token carry no stable user identity |
| `namespace` | the namespace the write actually targets | both storage backends |
| `token` | a SHA-256 hash of the bearer token | both storage backends, skipped for stdio |

| Variable | Default | Meaning |
|---|---|---|
| `QUOTA_WRITES_PER_MINUTE_USER` / `QUOTA_WRITES_PER_DAY_USER` | `0` (off) | Writes per minute/day for one user |
| `QUOTA_WRITES_PER_MINUTE_NAMESPACE` / `QUOTA_WRITES_PER_DAY_NAMESPACE` | `0` (off) | Writes per minute/day for one namespace |
| `QUOTA_WRITES_PER_MINUTE_TOKEN` / `QUOTA_WRITES_PER_DAY_TOKEN` | `0` (off) | Writes per minute/day for one bearer token |

Held across replicas on the same shared state the `RATE_LIMIT_*` limiters already use
(Valkey once `VALKEY_URL` is set, Postgres otherwise). A counter backend outage fails
open (the write proceeds, logged as a warning) rather than rejecting every write.

## Storage quotas (`QUOTA_MAX_*`)

Caps how many notes and how many total bytes a namespace may hold, Postgres mode only
(the Git backend's `vault_notes` stays empty, so a budget against it would be
meaningless). Two kinds of namespace, each its own pair of limits:

| Variable | Default | Meaning |
|---|---|---|
| `QUOTA_MAX_NOTES_PERSONAL` | `0` (off) | Notes in the caller's own personal namespace (`me`) |
| `QUOTA_MAX_BYTES_PERSONAL` | `0` (off) | Total note bytes in the caller's own personal namespace |
| `QUOTA_MAX_NOTES_SHARED` | `0` (off) | Notes in one group/project/org namespace |
| `QUOTA_MAX_BYTES_SHARED` | `0` (off) | Total note bytes in one group/project/org namespace |

Checked for `memory_write`, `memory_edit` and `memory_supersede` - never
`memory_archive`, which only ever moves a note to `_archive/...` in place, freeing a
note-count slot rather than consuming one. An archived note still counts toward the
byte-size budget (it still holds content), just not toward the note-count one.

This is a **soft** limit: the usage check and the write it gates are two separate
round trips, not one transaction, so two concurrent writes against a namespace already
one note or one byte under its cap can both pass the check and both land - a namespace
may briefly overshoot by at most the number of writes racing at that instant. A
database outage fails open the same way the write-rate quotas above do.

## Rejections

Every rejected write - either kind of quota - is recorded in the audit log
(`outcome: "rejected_quota"`), with the quota's scope/resource, limit and the value
that would have been over it, but never the quota key itself (a user's `oid`, the
namespace string or a token hash) and never the note's content.
