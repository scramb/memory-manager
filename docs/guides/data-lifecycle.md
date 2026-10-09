# Data lifecycle for enterprise accounts

What happens to a user's access and personal memory across the events an operator
actually has to react to: an Entra role removed, a user leaving self-service, an
admin acting on `/account`, and a restore. Facts only, each tied to the ADR or code
that decided it - see [`enterprise-operations.md`](./enterprise-operations.md) for
the surrounding deployment guide this one is linked from.

## Role removed in Entra: effective delay and the immediate remedy

Removing a `Memory.User`/`Memory.Curator`/`Memory.Admin` app role assignment in
Entra does **not** cut the user off immediately. The facade's refresh re-check
only re-reads `accountEnabled`, existence and group membership from Graph - never
`appRoleAssignments`, which would need the broader `Directory.Read.All` permission
([ADR-0006](../adr/0006-enterprise-auth-entra.md) addendum 2026-10-08). A removed
role therefore takes effect **at the user's next Entra login, at the latest after
`ENTRA_MAX_SESSION`** (default 12 h) - the session/refresh-token lifetime from the
same ADR (§5).

When a role must end immediately - an incident, an offboarding that cannot wait
for the session to expire - a `Memory.Admin` uses **"Revoke access"** in the admin
area on `/account` ([ADR-0006](../adr/0006-enterprise-auth-entra.md) addendum
2026-10-08: "a `Memory.Admin` revokes the user's sessions and tokens in the admin
area on `/account`"). Given the user's Entra object id and a mandatory reason, it:

- revokes every OAuth token family and static token the user owns
  (`auth.users.revoke_all_credentials`, reused from the WP-24 deprovisioning flow),
- ends every `/account` browser session the user holds
  (`account.sessions.revoke_all_for_oid`),
- writes one `admin.user.revoke` audit row (actor, target oid, reason, counts) -
  metadata only, never note content.

This does **not** disable the account (`users.disabled_at` stays unset) and does
**not** touch the user's notes - the user can sign in again right away once the
underlying reason is resolved. Disabling the account outright is still Entra's own
job, surfaced to this server only through the Graph delta sync
(`auth.users.disable_user`, WP-24): that is the path that actually happens on its
own, without an admin action, once Entra itself reports the user disabled or
removed.

## Self-service: a user deletes their own memory

On `/account`, a signed-in user can type a confirmation phrase to hard-delete their
own personal namespace (`account.delete`, #232,
[ADR-0008](../adr/0008-namespace-permissions.md) "Self-service"). This erases only
the caller's own `me` namespace - the account and every other credential survive,
and the note count shows `0` afterwards rather than ending the session.

## Admin erasure of a note, a namespace or a user

On `/account`, a `Memory.Admin` hard-deletes a note by its vault path, a
namespace by its alias or a user by their Entra object id
([#236](https://github.com/scramb/memory-manager/issues/236), WP-26), with a
mandatory reason and a typed confirmation of the target identifier itself
(not a fixed phrase, unlike self-service delete below). It reuses the same
`storage.erasure.erase_note`/`erase_namespace`/`erase_user` primitives
self-service delete (#232) already calls, through
`storage.base.StorageBackend.erase` - never a new delete statement of its
own. The admin never sees note content: a note is identified by path,
resolved to its id directly against the owner connection (an id is not
content). The erasure primitive's own `erasure_log`/`audit_log` row (actor,
reason, target kind, target ids, row counts) is the complete record of the
action; no separate admin-scoped audit row is added on top of it.

## Retention after deprovisioning

[ADR-0008](../adr/0008-namespace-permissions.md) "Deprovisioned users": once a
user's personal namespace is frozen (disabled in `users`, which already makes it
unreadable and unwritable - `mm_readable_ns()`/`mm_writable_ns()` treat a disabled
user as no identity), it is hard-deleted after `PERSONAL_RETENTION_DAYS` (default
30) by a worker retention job, with `erasure_log`'s actor recorded as
`system:retention`.

`PERSONAL_RETENTION_DAYS` and `RETENTION_SWEEP_SECONDS` (how often that job
runs, default 86400s/1 day) are `memory-manager worker` environment
variables (`config.py`'s `WorkerConfig`,
[#240](https://github.com/scramb/memory-manager/issues/240)).

## Erasure-log replay after a restore

A backup restore or point-in-time recovery rolls `erasure_log` back along with the
rest of the data, which can bring back content an erasure issued after the backup
had already removed. The fix is documented in full, with the exact operator
steps, in [`enterprise-operations.md`](./enterprise-operations.md#erasure-log-replay-after-a-restore)
(#233, [ADR-0007](../adr/0007-storage-backend.md) §3 addendum): the server replays
an off-database export of `erasure_log` from `ERASURE_LOG_REPLAY_FILE` before
`/readyz` turns ready. The deletion horizon past which a restore can no longer
undo an erasure is backup retention (default 30 d) + 7 d.

## Not included

- The full deletion acceptance test across every target kind (#241).
- Break-glass access to a deprovisioned user's personal namespace (#237-#239).
- Handover of a deprovisioned user's personal namespace to a successor - opt-in,
  needs the break-glass approval flow ([ADR-0008](../adr/0008-namespace-permissions.md)), not covered here.
