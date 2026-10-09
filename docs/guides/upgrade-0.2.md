# Upgrading from 0.1.x to 0.2.0

`v0.2.0` ships F-01 Enterprise Scale: a Postgres-backed storage mode that can be the source of
truth instead of Git, Entra ID sign-in, a `worker` process, quotas, a blocklist, SIEM audit
export, `/account` self-service and admin, erasure/retention/break-glass, and an enterprise Helm
profile. None of this is required to keep running `v0.1.x` the way it already runs - see "Staying
on the Git backend" below. This guide tells an operator what changes, how to stay on Git, how to
move to Postgres, where the enterprise profile's own operator guide picks up, and how to roll
back.

[`scripts/upgrade-smoke.sh`](../../scripts/upgrade-smoke.sh) (`make upgrade-smoke`) exercises the
exact commands this guide uses for the Postgres migration and the rollback export, end to end
against a real `v0.1.4` image, the current image and a real Postgres - every command below either
appears there verbatim (up to its own vault path/`--map`/connection details, which are always
operator-specific) or is marked "(operator-specific)" because it has no single correct form to
test against.

## What changes

- **Storage backend**: `STORAGE_BACKEND` already existed in `0.1.x`; what is new is everything
  built on top of `postgres` mode - a `worker` process, quotas, blocklist, audit export, erasure,
  retention, break-glass, `/account`, and the enterprise Helm profile. `STORAGE_BACKEND` still
  defaults to `git`.
- **MCP tool contract**: additive only. New tool `memory_promote`, new result field
  `namespace_kind`. `path` stays `<namespace>/<type>/<slug>.md`; `if_version` stays the SHA-256
  content hash (`docs/features/F-01-enterprise-scale.md` "Existing users").
- **Login modes**: `oidc` and `password` are unchanged. `entra` is a third mode
  ([ADR-0006](../adr/0006-enterprise-auth-entra.md)) - **`LOGIN_MODE=entra` requires
  `STORAGE_BACKEND=postgres`** (`http.py`'s `build_authenticator`: "LOGIN_MODE=entra requires
  STORAGE_BACKEND=postgres"). Static tokens are unchanged outside enterprise mode; in enterprise
  mode new tokens require scopes, an expiry and an owner.
- **Embedding dimension**: `EMBEDDING_DIMENSIONS` already existed. Its behaviour changes once a
  vault is migrated into the `postgres` backend: the dimension in effect at the **first**
  `migrate git-to-postgres` run is pinned to that backend and immutable afterwards
  (`db/migrate.py`, `EmbeddingDimensionPinError`) - a model with a different dimension needs a
  reindex into a new column/table, not a config change.
- **Container image**: the published image now ships the `otel` and `valkey` extras built in
  (both still inert unless you set `OTEL_EXPORTER_OTLP_ENDPOINT` or `VALKEY_URL`). The image is
  correspondingly larger; nothing else about running it changes.
- **Migration path**: `memory-manager migrate git-to-postgres` (below). Rollback means `export`
  from Postgres back to a Git vault.

## Staying on the Git backend (no action)

If `STORAGE_BACKEND` is unset or `git` today, it still defaults to `git` in `0.2.0`, and the chart
keeps `replicaCount: 1` (0 to scale down deliberately) with no autoscaler for it -
`values.schema.json` and the chart's own `validate` helper both refuse more than one replica or
an HPA/`ScaledObject` on the Git backend. Pulling the new image/chart tag and rolling it out the
same way as any other `0.1.x` patch (`deploy/README.md`'s own rollout step, operator-specific
to your own overlay) is the entire upgrade. None of the settings in "Breaking and changed
settings" below take effect without also setting `STORAGE_BACKEND=postgres`.

## Moving to Postgres

Full reference: [`migrate-git-to-postgres.md`](./migrate-git-to-postgres.md). This section is the
short path, with the exact commands `scripts/upgrade-smoke.sh` runs against a real `v0.1.4` vault.

### Prerequisites

- A Git vault checked out locally as a working tree (`git clone` it first if the server normally
  pulls it from a remote).
- A reachable Postgres 16+ with `pgvector` - the CLI applies the schema migration itself.
- `DATABASE_URL` for the role that will own the schema.
- One `--map <git-namespace>=<kind>:<key>[:<alias>]` per top-level namespace the vault has,
  including any that exist only under `_archive/` - the dry run below reports anything unmapped.

### Dry run

```sh
memory-manager migrate git-to-postgres --vault /path/to/vault \
  --map personal=user:oid-1234 \
  --map work=project:work \
  --map shared=org:org \
  --dry-run
```

(`--vault`'s path and every `--map` value are operator-specific - the flags and subcommand above
are exactly `scripts/upgrade-smoke.sh`'s own `migrate git-to-postgres ... --dry-run` call.) Reports
every discovered namespace, its mapped target (or `UNMAPPED`), note/revision counts, and any
problem that would block the import. Nothing is written.

### Import

```sh
memory-manager migrate git-to-postgres --vault /path/to/vault \
  --map personal=user:oid-1234 \
  --map work=project:work \
  --map shared=org:org
```

The command refuses to touch the database at all if its own preflight dry run is not clean. Each
mapped namespace imports in its own transaction; the search index is rebuilt once every namespace
is done.

### Verify

`scripts/upgrade-smoke.sh` compares every note's `version` (`vault.note.version`, the SHA-256
content hash) before the migration (on disk) against `vault_notes.version` after it - identical
for every note is the proof the import did not touch content, only each note's namespace mapping.
The operator-facing equivalent is reading a note back through a real token - but
`STORAGE_BACKEND=postgres` refuses to create a token for an owner with no row in `users`
(ADR-0006 §7, `auth/tokens.py`: "the owner must sign in at least once"), and `migrate
git-to-postgres` never writes that table itself, only a completed Entra login does
(`auth/login_entra.py` calls `auth/users.py`'s `upsert_user`; the `oidc` and `password` login
modes do not). So with `LOGIN_MODE=entra`, have the owner you mapped (`oid-1234` above) sign in
once before creating their token:

```sh
memory-manager token create check --scope memory:read --owner oid-1234 --role Memory.User \
  --expires-days 1
```

(operator-specific: pick the oid you just imported *and already signed in*, and an
`--expires-days` of your own choosing within `STATIC_TOKEN_MAX_DAYS`, default 90) - `memory_read`
on a path under the mapped namespace should return the same `version` the note had on disk.

### Cut-over

Point `memory-manager serve`/the chart's `storage.backend` at `postgres` and roll out
(operator-specific - no single command fits every deployment shape). The Git vault is never
written to again by this server once the cut-over is live; it stays the rollback target until you
are confident in the Postgres side.

## Enterprise profile

Running the full enterprise profile (Entra login, the `worker` Deployment, CNPG, autoscaling,
NetworkPolicies, observability) end to end, including registering the Entra app and the Flux
rollout, is covered in [`enterprise-operations.md`](./enterprise-operations.md), not repeated
here.

## Rollback via export

`migrate git-to-postgres` only ever reads the source vault - nothing about it is modified. The
simplest rollback, before any cut-over, is to keep pointing `memory-manager serve` at the
untouched Git vault (`STORAGE_BACKEND` unset or `git`).

Once Postgres is the live source of truth, `export` reads its current state back out as a Git-shaped
tar.gz + manifest archive - one-way, never read back ([ADR-0007](../adr/0007-storage-backend.md)):
it exports current content only, not revision history.

```sh
STORAGE_BACKEND=postgres DATABASE_URL=... memory-manager export --out rollback.tar.gz --force
```

(`DATABASE_URL`'s value is operator-specific; `--out`/`--force` are exactly
`scripts/upgrade-smoke.sh`'s own `export` call.) `export` writes every note under its *stored*
alias (the `user` kind's is a generated `u-<id>`; `org` is always `org`) - rename a namespace
directory back to its original Git-side name before comparing against an older Git vault, the same
rename `scripts/upgrade-smoke.sh` performs before its own `diff -r`.

## Breaking and changed settings

Everything below is new, or changed its meaning, since `0.1.4`. Unless noted, each is read by the
`api` process; "worker" means `memory-manager worker` only.

### What turns on by default once you adopt `STORAGE_BACKEND=postgres`

| Setting | Default | Effect on upgrade |
|---|---|---|
| `PERSONAL_RETENTION_DAYS` (worker) | `30` | **On by default.** Once you run the `worker` process against `postgres` mode, it hard-erases the personal namespace and identity of any user that has been disabled for at least this many days (`erasure_log`, actor `system:retention`) - relevant once users can be disabled at all, i.e. once Entra deprovisioning is in play. Set it higher, or do not run `worker` yet, if this is not wanted immediately. |
| `BREAK_GLASS_APPROVERS` | `2` | Fixed at 1 or 2. Default requires a second `Memory.Admin`, distinct from the requester, to approve a break-glass grant in the admin area. Only reachable through `/account`'s admin area, `postgres` mode only. |
| `RETENTION_SWEEP_SECONDS` (worker) | `86400` (1 day) | How often the retention job above checks for a user past its horizon. |

### Off by default - no action needed to keep current behaviour

| Setting | Default | Notes |
|---|---|---|
| `QUOTA_WRITES_PER_MINUTE_USER` / `QUOTA_WRITES_PER_DAY_USER` | `0` (off) | Per-user write-rate quota, `postgres` mode only ([`quotas.md`](./quotas.md)). |
| `QUOTA_WRITES_PER_MINUTE_NAMESPACE` / `QUOTA_WRITES_PER_DAY_NAMESPACE` | `0` (off) | Per-namespace write-rate quota, both backends. |
| `QUOTA_WRITES_PER_MINUTE_TOKEN` / `QUOTA_WRITES_PER_DAY_TOKEN` | `0` (off) | Per-token write-rate quota, both backends. |
| `QUOTA_MAX_NOTES_PERSONAL` / `QUOTA_MAX_BYTES_PERSONAL` | `0` (off) | Storage quota on the caller's own namespace, `postgres` mode only. |
| `QUOTA_MAX_NOTES_SHARED` / `QUOTA_MAX_BYTES_SHARED` | `0` (off) | Storage quota on a group/project/org namespace, `postgres` mode only. |
| `BLOCKLIST_FILE` | unset (off) | Operator-defined regex/keyword blocklist ([`blocklist.md`](./blocklist.md)). |
| `AUDIT_EXPORT` | `off` | `stdout`/`otlp`/both, exports every audit row to a SIEM ([`audit-export.md`](./audit-export.md)). |
| `ERASURE_LOG_REPLAY_FILE` | unset (off) | Only ever set right after a backup restore/PITR, to replay `erasure_log` rows the restore rolled back (`enterprise-operations.md` "Erasure-log replay after a restore"). |
| `WORKER_HOST` / `WORKER_PORT` | `127.0.0.1` / `8090` | Where `memory-manager worker`'s own `/healthz`/`/readyz`/`/metrics` bind. Irrelevant unless you run `worker` at all. |
| `JOBS_POLL_SECONDS` (worker) | `5` | `jobs` outbox poll fallback behind `LISTEN/NOTIFY`. |
| `ENTRA_DELTA_SYNC_SECONDS` (worker) | `300` | How often the Graph delta sync job runs - registered only once Entra is configured. |

### Entra login (`LOGIN_MODE=entra`)

All of the following are inert unless `LOGIN_MODE=entra`, which itself **requires
`STORAGE_BACKEND=postgres`**:

| Setting | Default | Notes |
|---|---|---|
| `ENTRA_TENANT_ID` / `ENTRA_CLIENT_ID` / `ENTRA_CLIENT_SECRET` | required | App registration from `deploy/entra/`'s OpenTofu module. |
| `ENTRA_ALLOWED_TENANTS` | the home tenant | Comma-separated tenant allowlist, for guest-tenant access. |
| `ENTRA_AUTHORITY` / `ENTRA_GRAPH_URL` | `https://login.microsoftonline.com` / `https://graph.microsoft.com/v1.0` | Override only for sovereign clouds/test doubles. |
| `ENTRA_ALLOW_INSECURE_AUTHORITY` | `false` | Lets `ENTRA_AUTHORITY`/`ENTRA_GRAPH_URL` be non-`https://` (test doubles only). |
| `ENTRA_GROUPS_TTL_SECONDS` | `3600` (1 h) | How long a cached `groups` claim is trusted before a Graph re-check. |
| `ENTRA_ACCESS_TOKEN_MINUTES` | `15` | Access-token lifetime. |
| `ENTRA_MAX_SESSION` | `43200` (12 h) | The outer session bound a removed role takes effect by at the latest. |

### Helm chart

| Value | Default | Effect on upgrade |
|---|---|---|
| `storage.backend` | `git` | New top-level switch; unset means no change to the rendered chart. |
| `api.*` (`replicaCount`, `autoscaling`, `keda`, `topologySpreadConstraints`) | postgres-only | Only rendered once `storage.backend` is `postgres` - the `git`-mode single `Deployment` still comes from `replicaCount` at the top level, unchanged. |
| `worker.*` | postgres-only | New `worker` `Deployment`, only rendered in `postgres` mode. |
| `shutdown.*` / `pdb.api.minAvailable` | postgres-only | Graceful-shutdown and `PodDisruptionBudget` settings for the `postgres`-mode Deployments. |
| `valkey.enabled` | `false` | Optional shared-state Deployment; `postgres` mode already covers the same state without it. |
| `login.entra.*` / `secrets.values.entraClientSecret` | empty/placeholders | Inert unless `login.mode` is `entra`. |
| `database.cnpg.instances` | `1` | More than 1 requires `storage.backend: postgres`. |
| `database.cnpg.backup.enabled` | `false` | Barman Cloud plugin scheduled backups; needs the plugin and cert-manager installed separately. |
| `database.appRole` | `memory_manager_app` | `DATABASE_APP_ROLE`, `postgres` mode only - the non-owner role every request transaction switches to under RLS. |
| `networkPolicy.prometheus` / `networkPolicy.cnpgOperator` | `{}` (matches everything) | Only used once `networkPolicy.enabled` and `storage.backend: postgres`. |
| `grafanaDashboard.enabled` / `prometheusRule.enabled` | `false` | Dashboard ConfigMap and alert rules; need a Grafana sidecar/Prometheus Operator installed separately. |

## See also

- [`migrate-git-to-postgres.md`](./migrate-git-to-postgres.md) - the full migration reference.
- [`enterprise-operations.md`](./enterprise-operations.md) - running the enterprise profile end
  to end (Entra app registration, Flux rollout, scaling, backups, observability).
- [`../releasing.md`](../releasing.md) - what a `v0.2.0` tag publishes and how to verify it.

## Not included

- Upgrading to `1.0` (F-02, [#202](https://github.com/scramb/memory-manager/issues/202)).
- The README enterprise section
  ([#279](https://github.com/scramb/memory-manager/issues/279)).
