# Running the enterprise profile end to end

The enterprise profile (`charts/memory-manager/values-enterprise.yaml`, WP-29/WP-30) is
`storage` → `backend` `postgres` instead of the default single-writer `git` Deployment: Entra
login ([ADR-0006](../adr/0006-enterprise-auth-entra.md)), a stateless `api` Deployment and a
`worker` Deployment behind their own `HorizontalPodAutoscaler`s
([ADR-0009](../adr/0009-stateless-replicas.md)), a 3-instance CloudNativePG `Cluster` with
Barman Cloud plugin backups, and per-component `NetworkPolicy` objects. This guide walks one
path through all of it: registering the Entra app, rolling the profile out with Flux, checking
it came up healthy, scaling it, turning Valkey on or off, running a backup/restore drill, and
upgrading it afterwards. It assumes the Flux example
([`deploy/flux/enterprise/`](../../deploy/flux/enterprise/)); the same values work installed
directly with `helm` (`charts/memory-manager/README.md`'s own "Enterprise profile" section).

Every command below either runs verbatim from `deploy/entra/README.md` or
`scripts/e2e-kind.sh`, or reuses a shape this repository already renders and validates
(the chart's own templates, `kubeconform` against the real CRDs) — nothing here is invented
syntax. One exception is called out explicitly where it happens: the restore drill in step 7
describes a CNPG/Barman Cloud plugin procedure this repository does not yet exercise in CI
(neither the kind E2E nor any other test takes or restores a backup today).

## Prerequisites

| Who/what | Needed for |
|---|---|
| A tenant admin on the target Microsoft Entra ID tenant | one manual consent step in "1. Register the Entra app" below ([ADR-0006](../adr/0006-enterprise-auth-entra.md) §9, Decision: "Admin consent stays a manual operator step") |
| A cluster operator with `kubectl`/`flux` access to the target cluster | steps 3-9 |
| `tofu`/`terraform` (`azuread` provider 3.x) on the machine running step 1 | `deploy/entra/README.md` |
| `kubectl`, the `flux` CLI, `helm`, `kubeconform` | steps 3-4, 7-8 |

Cluster-side prerequisites, from [`deploy/flux/enterprise/README.md`](../../deploy/flux/enterprise/README.md#prerequisites-not-installed-by-this-example) (not installed by this profile — an operator step, same as the Entra app registration):

| Component | Tested version | Needed for |
|---|---|---|
| CloudNativePG operator | >= 1.26 | the 3-instance `Cluster` (CNPG-I plugin protocol) |
| Barman Cloud CNPG-I plugin | 0.15.1 | the `ObjectStore`/`ScheduledBackup` objects (step 7) |
| cert-manager | any current release | the Barman Cloud plugin's own `Issuer`/`Certificate`s |
| Flux (`source-controller`, `helm-controller`) | pinned per `scripts/e2e-kind.sh` (`FLUX_VERSION`) | step 3 |
| KEDA | 2.17 | optional, only if you use request-rate scaling instead of the CPU HPA (step 5) |
| Prometheus Operator | any current release shipping `monitoring.coreos.com` CRDs | optional, `serviceMonitor` and the KEDA Prometheus trigger (steps 5, 9) |

The enterprise profile ships from chart version `0.2.0` onward
(`deploy/flux/enterprise/helmrelease.yaml`'s own comment); pick a released image/chart tag at or
above that (`docs/releasing.md`).

## 1. Register the Entra app

Run [`deploy/entra/`](../../deploy/entra/)'s OpenTofu module once per tenant, exactly as its own
README documents:

```sh
tofu -chdir=deploy/entra init
tofu -chdir=deploy/entra plan -var public_url=https://memory.example.com
tofu -chdir=deploy/entra apply -var public_url=https://memory.example.com
```

Then, as a tenant admin, open the `admin_consent_url` output once — this is the one step that
cannot be scripted (Microsoft Graph application permissions need interactive or
`az ad app permission admin-consent` consent).

Capture three outputs for the next steps: `tofu -chdir=deploy/entra output tenant_id`,
`... output client_id`, and `... output -raw client_secret` (sensitive — pipe it straight into
your secret store, never into a file). `service_principal_object_id` is what you assign
`Memory.User`/`Memory.Curator`/`Memory.Admin` to afterwards (Entra admin center → Enterprise
applications → this app → "Users and groups").

## 2. Populate secrets

[`deploy/flux/enterprise/externalsecret.yaml`](../../deploy/flux/enterprise/externalsecret.yaml)
pulls two secrets from your `ClusterSecretStore`: `memory-manager-secrets` (app secrets) and
`memory-manager-backup-credentials` (CNPG object-store credentials). The full key table is in
[`deploy/flux/enterprise/README.md`](../../deploy/flux/enterprise/README.md#required-secrets) —
four keys, generated with:

- `VAULT_WEBHOOK_SECRET`: `openssl rand -hex 32`
- `OAUTH_CLIENT_SECRET_KEY`: the one-liner in `src/memory_manager/auth/store.py`'s own
  `ValueError` message (generates a Fernet key)
- `ENTRA_CLIENT_SECRET`: step 1's `client_secret` output
- `ACCESS_KEY_ID`/`ACCESS_SECRET_KEY`: issued by your own S3-compatible object store

Put all four under the keys your `ExternalSecret`'s `remoteRef`s point at before step 3 — the
whole sync fails if even one key is missing.

## 3. Roll out with Flux

A `GitRepository` plus a `Kustomization` pointing at `./deploy/flux/enterprise`, with the
operator-specific values patched in (publicUrl, hostnames, gateway, Entra tenant/client ID,
backup destination, secret store) — the full example, including every patch target, is in
[`deploy/flux/enterprise/README.md`](../../deploy/flux/enterprise/README.md#applying-this).

Validate the render offline before committing it, same command that README documents:

```sh
kubectl kustomize deploy/flux/enterprise | kubeconform -strict -summary \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
```

Commit and push your overlay; Flux reconciles on its own `interval`, or immediately
(same pattern as `deploy/README.md`'s own rollout step, against this `Kustomization`'s name
instead):

```sh
flux reconcile kustomization memory-manager -n memory-manager --with-source
```

## 4. Verify readiness

```sh
kubectl -n memory-manager get pods,cluster,externalsecret,helmrelease
kubectl -n memory-manager get pods -l app.kubernetes.io/instance=memory-manager,app.kubernetes.io/component=api
```

Each `api` pod answers `/readyz` with `{"ready": true}` once Postgres (and, in `entra` mode,
Entra discovery) is reachable — the same check `scripts/e2e-kind.sh` runs against every `api`
pod before calling a kind rollout green:

```sh
kubectl -n memory-manager get --raw "/api/v1/namespaces/memory-manager/pods/<api-pod-name>:8080/proxy/readyz"
```

Once an `HTTPRoute`/`Ingress` is wired to a real hostname, the same check from outside the
cluster (`deploy/README.md`'s own "Checks" section):

```sh
curl -s https://memory.example.com/healthz
curl -s -o /dev/null -w '%{http_code}\n' https://memory.example.com/mcp   # 401 without a token
```

The `worker` Deployment serves its own `/healthz`/`/readyz`/`/metrics` on `WORKER_PORT`
(default `8090`) — the same `kubectl get --raw .../proxy/readyz` pattern above, against a
`component=worker` pod and port `8090` instead of `8080`.

## 5. Scaling: HPA or KEDA

`values-enterprise.yaml` turns on a CPU `HorizontalPodAutoscaler` for both `api` and `worker`
(`api.autoscaling.enabled`/`worker.autoscaling.enabled`, owner decision O27,
[ADR-0009](../adr/0009-stateless-replicas.md) addendum 2026-10-08) — 3/10 replicas at 70% CPU
for `api`, 2/6 at 70% for `worker`. A `PodDisruptionBudget` (`pdb.api.minAvailable: 2`) keeps at
least two `api` pods up during a voluntary disruption; `worker` has none, since its jobs are
best-effort background work.

Request-rate scaling through KEDA is optional and `api`-only (`docs/research/kubernetes-scaling.md`):
turn `api.keda.enabled` on and `api.autoscaling.enabled` off in your own overlay — the chart's
own `validateApiAutoscaling` helper refuses a render with both on at once. KEDA itself must
already be installed in the cluster; the chart only renders the `ScaledObject`. There is no KEDA
option for `worker` — only the CPU HPA.

## 6. Valkey: optional shared state

`values-enterprise.yaml` leaves `valkey.enabled` off: the 3-instance CNPG `Cluster` already
carries the same rate-limit/login state at far more than the expected load
([ADR-0009](../adr/0009-stateless-replicas.md) §2). Turn `valkey.enabled` on in your own overlay
for Valkey's sub-millisecond counters instead — it runs without persistence (losing it means only
reset counters and aborted logins in progress, never durable data) and needs no migration either
way, since both implementations sit behind the same `SharedState` interface.

## 7. Backups and restore drill

### Scheduled backups

`database.cnpg.backup.enabled: true` (set in `values-enterprise.yaml`) renders a daily
`ScheduledBackup` (`schedule: "0 0 2 * * *"`, 02:00) through the Barman Cloud CNPG-I plugin, with
a 30-day `ObjectStore` retention policy. Set your own `destinationPath`/`endpointURL` and point
`existingSecret` at the credentials `ExternalSecret` from step 2 — every value
`values-enterprise.yaml`/`helmrelease.yaml` ship for these is a placeholder.

### Taking an on-demand backup

The same shape the `ScheduledBackup` above renders (`charts/memory-manager/templates/cnpg-scheduledbackup.yaml`),
as a one-off `Backup` instead of a schedule:

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata:
  name: memory-manager-manual-<date>
  namespace: memory-manager
spec:
  cluster:
    name: memory-manager-db
  method: plugin
  pluginConfiguration:
    name: barman-cloud.cloudnative-pg.io
```

```sh
kubectl apply -f manual-backup.yaml
kubectl -n memory-manager get backup memory-manager-manual-<date> -w
```

### Restoring from a backup

Not exercised by this repository's own tests today (same "Not included" line as
`docs/research/cnpg-backups.md` and `charts/memory-manager/README.md`: no backups run in the
kind E2E). CloudNativePG recovers into a **new** `Cluster` that bootstraps from the same
`ObjectStore` (`memory-manager-db-backup`) the backups above wrote to, through an
`externalClusters` entry naming the same Barman Cloud plugin:

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: memory-manager-db-restored
  namespace: memory-manager
spec:
  instances: 3
  imageName: ghcr.io/cloudnative-pg/postgresql:16-standard-trixie
  bootstrap:
    recovery:
      source: memory-manager-db
  externalClusters:
    - name: memory-manager-db
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: memory-manager-db-backup
```

Verified against the Barman Cloud plugin's own "Using the Barman Cloud Plugin" page
(https://cloudnative-pg.io/plugin-barman-cloud/docs/0.15.0/usage/) and the current CNPG
`Cluster` CRD, retrieved 2026-10-08 — `kubeconform -strict` accepts this shape. It is still not
exercised by this repository's own CI the way the chart's rendered templates are: no test here
ever takes and restores a backup.

Once the restored `Cluster` is ready, point `DATABASE_URL`/`database.url` (or a fresh
`database.cnpg` block targeting it) at it and roll `api`/`worker` out against it. The schema
migrates forward on its own at process start, same as every other rollout
(`deploy/README.md`'s own "Rollout" section).

### Erasure-log replay after a restore

A restore or PITR rolls `erasure_log` back along with the rest of the data — any erasure that
happened after the backup being restored from comes back too, since every `erase_note`/
`erase_namespace`/`erase_user` call is also emitted through the audit/SIEM export (`detail`
carries only the IDs, `docs/guides/audit-export.md`'s own "Erasure records and restores"
section). After pointing `DATABASE_URL`/`database.url` at the restored cluster (previous
section), before rolling `api` out against it:

1. From the SIEM, extract every `erasure` record since the backup's point in time into a JSONL
   file, one exported record per line.
2. Set `ERASURE_LOG_REPLAY_FILE` on the `api` deployment to that file's path (a mounted
   `Secret`/`ConfigMap`, not a literal value in the chart's own values — public-repository rule,
   no operator-specific paths in this guide).
3. Roll `api` out. Each replica replays the file under a Postgres advisory lock before it reports
   ready (ADR-0007 §3 addendum) — `/readyz` stays 503 until replay finishes, and a malformed file
   refuses startup outright rather than serving traffic against a half-restored erasure state.
4. Once every replica is ready, unset `ERASURE_LOG_REPLAY_FILE` again (or drop it from the next
   rollout) — replay is idempotent, but there is nothing left for it to do once this restore's
   replicas have all gone through it once.

### Deletion horizon

Documented at backup retention (default 30 d, `database.cnpg.backup.retentionPolicy`) + 7 d
([ADR-0007](../adr/0007-storage-backend.md) §3) — an erasure older than that horizon can no
longer be un-done by a restore from this profile's own default retention window.

## 8. Upgrades

`deploy/flux/enterprise/helmrelease.yaml` pins the chart to a semver range
(`">=0.2.0 <0.3.0"` today). A new chart version inside that range rolls out on its own at the
`HelmRepository`'s next `interval`, or immediately:

```sh
flux reconcile helmrelease memory-manager -n memory-manager --with-source
```

Crossing a minor version boundary is a deliberate step, not automatic: bump the range through
the same JSON-patch mechanism `deploy/flux/enterprise/README.md`'s own "Applying this" example
already uses, targeting the `HelmRelease` instead:

```yaml
    - target:
        kind: HelmRelease
        name: memory-manager
      patch: |
        - op: replace
          path: /spec/chart/spec/version
          value: ">=0.3.0 <0.4.0"
```

The schema migrates forward on its own at process start either way
(`deploy/README.md`'s own "Rollout" note); a release that needs a manual step says so in its own
release notes. A dedicated guide for the v0.2.0 upgrade itself is tracked separately (WP-34), not
covered here.

## 9. Observability

`/metrics` is on by default (`metrics.enabled`); turning on `serviceMonitor.enabled` needs the
Prometheus Operator CRDs from the prerequisites table above and also renders a `PodMonitor` for
the CNPG `Cluster`'s own instance pods (#265) - not the `Cluster`'s own `monitoring.enablePodMonitor`
flag, which the CNPG project itself has already started deprecating (`docs/research/cnpg-backups.md`
records exactly which released patch).

Turn `grafanaDashboard.enabled` and `prometheusRule.enabled` on for a Grafana dashboard and a
`PrometheusRule` (#265, `charts/memory-manager/README.md`'s own "Values" table documents every
key): per-tool call rate/error rate/p95 latency, search p95 by mode, rate-limit hits by limiter,
the embedding job backlog/lag, and - this profile only - fewer than `prometheusRule.thresholds.minReadyApiReplicas`
ready `api` pods and a failed CNPG object-store backup. Every threshold lives under
`prometheusRule.thresholds`, tunable without forking the chart. The backup alert needs the CNPG
operator itself on CNPG 1.27 or later to actually see the Barman Cloud plugin's own backup
timestamps - it stays dormant, not wrong, on the CNPG 1.26 line this profile's own prerequisites
table above still allows (`docs/research/cnpg-backups.md` has the verified source); every other
alert works regardless of the operator's own version.

`grafanaDashboard.enabled` only renders a `ConfigMap` carrying the dashboard JSON, labeled for a
Grafana sidecar to pick up - Grafana and its sidecar are an operator's own install, same as
Prometheus itself.

## See also

[`data-lifecycle.md`](./data-lifecycle.md) covers the account-level operator
questions this guide does not: what happens (and how fast) when a role is removed
in Entra, the admin "revoke access" remedy, self-service and admin erasure, and
retention after deprovisioning.

## Not included

- The v0.2.0 upgrade guide (WP-34).
- Compliance templates for data flow, records of processing and TOMs (WP-33).
- Alertmanager routing for the `PrometheusRule` alerts above (operator-specific, #265).
- Installing Grafana, its sidecar, Prometheus or the Prometheus Operator itself.
