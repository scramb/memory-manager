# Deletion concept (template)

> **Not legal advice.** This is a template for an operator's own GDPR Art. 17 ("right to
> erasure") and retention assessment of a `memory-manager` deployment. It describes what this
> codebase actually deletes, pseudonymizes or redacts, and when - verified against the code, the
> ADRs and [`docs/security/threat-model.md`](../security/threat-model.md), not invented - but it
> does not decide whether this scope is *sufficient* for your retention obligations, your
> jurisdiction's own deletion deadlines, or a data subject's own erasure request beyond what this
> server automates. See [`README.md`](./README.md) for how to use this set of templates, and
> have the filled-in result reviewed by someone qualified to give legal advice for your
> jurisdiction and your deployment before relying on it.

## Scope

Erasure (true, hard deletion, as opposed to the MCP-level "archive" every `_archive/` note
already supports) exists **only** with `STORAGE_BACKEND=postgres` and **only** outside the MCP
surface: through a signed-in user's own `/account` self-service page, the `Memory.Admin` area on
`/account`, and a worker retention job - never through a tool a client (Claude) can call
(CLAUDE.md "No MCP tool hard-deletes notes"; "erasure... exists only with the `postgres` backend,
outside MCP"). The default `git` storage backend has no erasure of any kind - see "Git-backend
limitation" below.

## Erasure scope: what is deleted, pseudonymized or redacted

| Data category | Deletion trigger | Mechanism | Deadline |
|---|---|---|---|
| Personal namespace (`me`): every note, revision, chunk and embedding job | user self-service delete (`/account/delete`); admin erase, target kind `user` or `namespace`; retention job after deprovisioning | **Hard-delete** - `storage/erasure.py::erase_user`/`erase_namespace` deletes the rows outright, in one transaction (ADR-0007 §3 addendum "the personal namespace... is hard-deleted") | Immediate on a self-service or admin request; for a deprovisioned user, `PERSONAL_RETENTION_DAYS` (default 30 d) after `disabled_at` is set (`worker.py::_retention_job`) |
| The user's identity rows (`users`, `user_groups`, OAuth/static tokens, `/account` sessions) | admin erase, target kind `user`; retention job | **Hard-delete** - same `erase_user` call; a subsequent Entra login for that `oid` is then treated as unknown | Same as above |
| Authorship of notes the erased user wrote in a **shared** namespace (group/project/org) | erasure of that user | **Pseudonymization** - the note and its content stay (the team owns it); every `vault_revisions` row with `author_oid = oid` gets `author`/`author_oid` rewritten to the literal string `"erased"`, never kept as a re-linkable pseudonym (ADR-0007 addendum 2026-10-08 "erasure scope"; `erase_user`) | Immediate, same transaction as the identity deletion above |
| `audit_log` rows for the erased personal namespace | same erasure | **Redaction, not deletion** - `path` is rewritten to the literal `[erased]`, `detail` is cut down to `audit.DETAIL_ALLOWLIST` keys only; the row itself (actor, outcome, timestamp) survives for incident review (`storage/erasure.py::_redact_audit_for_namespace`/`_redact_audit_for_paths`) | Immediate, same transaction |
| `audit_log` rows for a **shared**-namespace note the erased user merely co-authored | — | **Not redacted** - the row describes the note, not the erased identity, and the note itself still exists | n/a |
| `erasure_log` (the erasure's own metadata row: target kind, target ids, actor, reason, row counts - never content) | — | **Never erased itself** - it is the record an audit review and a post-restore replay (below) both depend on | Survives at least the backup horizon (below); beyond that, the operator's own audit-log/SIEM retention policy |
| Backup copies of every row above | the backup object store's own retention policy expiry | No server-side action - CNPG/Barman Cloud deletes the backup object itself once its `ObjectStore` retention policy expires (`docs/guides/enterprise-operations.md` "Backups and restore drill") | Operator's own `database.cnpg.backup.retentionPolicy` (chart default 30 d) |
| Markdown export produced by `/account/export` (a ZIP of the caller's own `me` namespace) | — | Not an erasure target at all - a point-in-time copy the caller downloaded themselves; deleting it is the caller's own responsibility once downloaded | n/a |
| `<...>` | `<any further personal data your own deployment adds - e.g. a custom embedding provider's own logs>` | `<...>` | `<...>` |

## Backup horizon

An erasure removes the row(s) from the live database immediately, but a backup taken **before**
the erasure still contains the pre-erasure data until that backup itself expires. The documented
horizon past which a restore can no longer bring erased data back is:

```
backup retention + 7 days
```

where **backup retention** is the operator's own `database.cnpg.backup.retentionPolicy` (CNPG
`ScheduledBackup`/`ObjectStore` retention; chart default 30 d - fill in your own value if you
have changed it: `<your retentionPolicy value>`) and the **+ 7 days** is ADR-0007's own margin
for backup-window slack (ADR-0007 §3 addendum; `docs/guides/enterprise-operations.md` "Deletion
horizon"). With the chart default this is **37 days**. An erasure older than this horizon is
final even across a restore from this profile's own default retention window; an erasure younger
than it depends on the replay below having actually been run after any restore that could have
rolled it back.

## Git-backend limitation: no erasure of history

The default `git` storage backend has **no erasure mechanism of any kind** - this is a deliberate
limitation, not an oversight. Git history is designed to be permanent: erasing a commit means
rewriting history in every clone and every remote, which cannot be proven across mirrors the way
a database `DELETE` can (ADR-0007 Context, "Git history is designed to be permanent"). An
operator who needs GDPR Art. 17 erasure for personal data in notes must use the `postgres`
backend (ADR-0007), where it is append-only revisions with the hard-delete/pseudonymization
scope documented above, not Git. `<document your own policy for a `git`-backend deployment that
nonetheless receives an erasure request - e.g. a documented manual history rewrite with every
clone/mirror operator notified, outside this server's own tooling>`.

## Deprovisioning via Graph delta sync (WP-24)

Before a user's personal data is ever hard-deleted by the retention job, the `worker` process's
own `entra_delta_sync` job (`worker.py::_entra_delta_sync_job`, ADR-0006 §6, default interval
`ENTRA_DELTA_SYNC_SECONDS=300`) polls Microsoft Graph's `users/delta` endpoint and, for a user
Graph reports disabled or removed, calls `auth/users.py::disable_user` - setting `users.disabled_at`
(which already makes the user's namespace unreadable and unwritable: `mm_readable_ns()`/
`mm_writable_ns()` treat a disabled user as no identity at all) and revoking every OAuth token
family, static token and `/account` session the user holds (`auth/users.py::revoke_all_credentials`).
This runs **without** any admin action - it is the path that detects deprovisioning on its own.
Only once a user has been disabled for at least `PERSONAL_RETENTION_DAYS` does the separate
retention job (`worker.py::_retention_job`, WP-26) call the same `erase_user` primitive described
above, as the `system:retention` actor.

## Restore runbook: replaying the erasure log after a restore

A backup restore or point-in-time recovery rolls `erasure_log` back along with the rest of the
data, which can resurrect content (and un-redact authorship/audit paths) that an erasure removed
after the backup being restored from was taken. Every erasure is therefore also exported through
the audit/SIEM pipeline the moment its own transaction commits (`storage/erasure.py::_export_erasure`,
`AUDIT_EXPORT`) - an off-database copy a restore's rollback cannot touch. After restoring (see
[`docs/guides/enterprise-operations.md`](../guides/enterprise-operations.md)'s "Restoring from a
backup" for the CNPG `Cluster` recovery itself), **before** rolling the `api` deployment out
against the restored cluster:

1. From the SIEM/log collector, extract every exported `erasure` record **since the backup's own
   point in time** into a JSONL file, one exported record per line.
2. Set `ERASURE_LOG_REPLAY_FILE` on the `api` deployment to that file's path (a mounted
   `Secret`/`ConfigMap` - never a literal operator-specific path committed anywhere public).
3. Roll `api` out. Each replica replays the file (`storage/erasure_replay.py::replay`) under a
   Postgres advisory lock **before** it reports ready: `http.py`'s `lifespan` holds `/readyz` at
   `503` until the replay finishes (ADR-0007 §3 addendum); a malformed line (bad JSON, an unknown
   op, a missing target kind, an empty target-ids list) refuses startup outright, naming the bad
   line, rather than serving traffic against a half-restored erasure state.
4. **Verify**: once every replica reports ready, confirm that every target named in the replay
   file is in fact gone (or, for a shared-namespace author, pseudonymized to `"erased"`) by
   re-reading it the same way the erasure's own acceptance test does
   (`tests/db/test_erasure_replay.py::test_erases_a_user_that_reappeared_and_re_applies_pseudonymization`).
   Replay is idempotent - running it again (e.g. against a second replica, or a repeated rollout)
   changes nothing further (`test_replay_is_a_no_op_for_a_target_already_gone`).
5. Once every replica is ready and verified, unset `ERASURE_LOG_REPLAY_FILE` again (or drop it
   from the next rollout) - there is nothing left for it to do once this restore's replicas have
   all gone through it once.

(Restore runbook and replay mechanism: ADR-0007 §3 addendum; `docs/guides/enterprise-operations.md`
"Erasure-log replay after a restore"/"Deletion horizon" (committed on `main`);
`storage/erasure_replay.py`, `http.py`'s `lifespan` readiness gate and `config.py::erasure_log_replay_file`
**(WP-26, on branch `wp/26-admin-erasure`, not yet merged to `main`)**.)

## Not included

- A Data Protection Impact Assessment, a transparency notice to data subjects, and a
  Germany-specific section (#276).
- Erasure or retention beyond what the code cited above actually implements - this document
  describes it, [WP-26](https://github.com/scramb/memory-manager/issues/231) builds it.
- The deletion acceptance test across every target kind in full
  (`tests/e2e/test_erasure.py`, #241) - cited above only where it verifies a specific claim.
- Legal review of this template.
