# CloudNativePG object-store backups: the Barman Cloud plugin (#252)

Retrieved: 2026-10-08 · Feeds: [ADR-0007](../adr/0007-storage-backend.md), WP-29
(`templates/cnpg-cluster.yaml`, `templates/cnpg-objectstore.yaml`,
`templates/cnpg-scheduledbackup.yaml`)

Checked against the Barman Cloud CNPG-I plugin docs, version 0.15.1 (the version
published at retrieval time; `0.15.1` is also the manifest release tag referenced below),
and the CloudNativePG 1.26 `ScheduledBackup` CRD:

- [Using the Barman Cloud Plugin](https://cloudnative-pg.io/plugin-barman-cloud/docs/usage/)
- [Installation](https://cloudnative-pg.io/plugin-barman-cloud/docs/installation/)
- [Retention Policies](https://cloudnative-pg.io/plugin-barman-cloud/docs/retention-policies/)
- [API Reference](https://cloudnative-pg.io/plugin-barman-cloud/docs/plugin-barman-cloud.v1/)
  (`barmancloud.cnpg.io/v1`, `ObjectStore`)
- `postgresql.cnpg.io_scheduledbackups.yaml` CRD (CloudNativePG `release-1.26` branch,
  `config/crd/bases/`) for the `ScheduledBackup` fields (`method`, `pluginConfiguration`,
  `backupOwnerReference`, `schedule`)
- [datreeio/CRDs-catalog](https://github.com/datreeio/CRDs-catalog) schemas
  `barmancloud.cnpg.io/objectstore_v1.json` and `postgresql.cnpg.io/scheduledbackup_v1.json` -
  the same catalog `.github/workflows/validate.yml`'s `deploy` job already points kubeconform
  at; both exist there, so the new templates validate the same way the existing `Cluster`
  template does.

## Why the plugin, not the in-tree `barmanObjectStore`

CloudNativePG moved object-store backups out of the operator and into a separate CNPG-I
plugin (`barman-cloud.cloudnative-pg.io`); the in-tree `.spec.backup.barmanObjectStore`
field still exists but is the deprecated path going forward, and a plugin-based
`isWALArchiver` entry on `.spec.plugins` cannot coexist with it on the same `Cluster`
(confirmed by the `Cluster` CRD's own `isWALArchiver` field description: "This cannot be
enabled if the `.spec.backup.barmanObjectStore` configuration is present"). This chart
only ever renders the plugin-based shape.

## Prerequisites (not this chart's job, #252's own "Not included")

- CloudNativePG **>= 1.26** (older operator versions do not implement the CNPG-I plugin
  protocol the Barman Cloud plugin needs).
- **cert-manager** installed in the cluster - the plugin's own installation manifest
  provisions a self-signed `Issuer` and client/server `Certificate`s through it.
- The plugin itself installed **in the same namespace as the CNPG operator** (typically
  `cnpg-system`), via its Helm chart (`cnpg/plugin-barman-cloud`) or
  `kubectl apply -f https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/v0.15.1/manifest.yaml`.

## Shape this chart renders

`ObjectStore` (`barmancloud.cnpg.io/v1`, `templates/cnpg-objectstore.yaml`) - one per
backup destination; `.spec.configuration` follows the same schema the in-tree
`barmanObjectStore` always used (`destinationPath`, `endpointURL`, `s3Credentials.accessKeyId`/
`secretAccessKey` as `{name, key}` secret references, `wal.compression`).
`.spec.retentionPolicy` is a top-level field on `ObjectStore` itself (not under
`configuration`), pattern `^[1-9][0-9]*[dwm]$` - `"30d"` here, matching ADR-0007's documented
30 d + 7 d erasure-replay horizon. The important deviation from the in-tree shape: the
`ObjectStore`'s own `serverName` field must stay empty - it exists only for API compatibility
with `barmanObjectStore` - and the WAL-archiver's server identity instead comes from the
`Cluster`'s own name via the plugin parameter below.

`Cluster.spec.plugins` entry (`templates/cnpg-cluster.yaml`):

```yaml
plugins:
  - name: barman-cloud.cloudnative-pg.io
    isWALArchiver: true
    parameters:
      barmanObjectName: <ObjectStore name>
```

`ScheduledBackup` (`postgresql.cnpg.io/v1`, `templates/cnpg-scheduledbackup.yaml`) - CNPG's
own existing CRD, not plugin-specific; `method: plugin` + `pluginConfiguration.name:
barman-cloud.cloudnative-pg.io` replace the deprecated `method: barmanObjectStore`.
`backupOwnerReference: cluster` ties every `Backup` it creates to the `Cluster`'s lifecycle.
`schedule` is **not** the Kubernetes CronJob format - it carries an extra, leading seconds
field (`robfig/cron`'s format) - this chart's default (`"0 0 2 * * *"`) is daily at 02:00.

## Deliberately left out of this task (per the issue's own "Not included")

- Installing the CNPG operator, the Barman Cloud plugin or cert-manager.
- The restore runbook and `erasure_log` replay procedure after a restore (WP-26, the
  30 d + 7 d horizon ADR-0007 already documents).
- PgBouncer/CNPG Pooler.
- Backups in the kind E2E.

## Instance pod labels and the operator's status port (#255)

Retrieved: 2026-10-08 · Feeds: WP-29 (`templates/networkpolicy.yaml`) - the per-component
`NetworkPolicy` objects storage's own backend `postgres` renders need to select the CNPG
Cluster's own instance pods by label, and to scope the operator's own ingress to its status
port rather than Postgres itself.

Checked directly against the CloudNativePG source, `release-1.26` branch:

- [`pkg/utils/labels_annotations.go`](https://github.com/cloudnative-pg/cloudnative-pg/blob/release-1.26/pkg/utils/labels_annotations.go)
- [`pkg/specs/pods.go`](https://github.com/cloudnative-pg/cloudnative-pg/blob/release-1.26/pkg/specs/pods.go)
- [`pkg/management/url/url.go`](https://github.com/cloudnative-pg/cloudnative-pg/blob/release-1.26/pkg/management/url/url.go)

Every instance pod the operator creates for a `Cluster` carries `cnpg.io/cluster: <cluster
name>` and `cnpg.io/podRole: instance` (`pkg/specs/pods.go`'s own pod-template labels, using
the constants `utils.ClusterLabelName`/`utils.PodRoleLabelName`/`utils.PodRoleInstance`) - a
job pod (`initdb`, `join`, ...) carries `cnpg.io/jobRole` instead, so `podRole: instance`
excludes those. `cnpg.io/instanceRole` (`primary`/`replica`) and the deprecated `role` label
carry the same value but are not needed here - the `NetworkPolicy` targets every instance
alike, primary or replica, since replication traffic flows both ways during a failover.

The instance manager's own HTTP API - the one the operator's reconciler calls for
`pg/status`, `pg/backup`, etc., not Postgres' own `5432` - listens on `8000`
(`pkg/management/url/url.go`'s own `StatusPort`). `PostgresMetricsPort` (`9187`, the
Prometheus exporter) and `LocalPort` (`8010`, loopback-only) are both unrelated to the
operator's own ingress and out of scope for #255's `NetworkPolicy` objects.

## Backup and replication metric names (#265)

Retrieved: 2026-10-09 · Feeds: WP-31 (`templates/prometheusrule.yaml`) - the
`MemoryManagerCnpgBackupFailed` alert needs the metric name CNPG (or its Barman Cloud
plugin) actually exports under this chart's own plugin-based backup shape, not a name
that merely sounds right.

Checked directly against source, since the two are easy to conflate:

- [`pkg/management/postgres/webserver/metricserver/pg_collector.go`](https://github.com/cloudnative-pg/cloudnative-pg/blob/release-1.26/pkg/management/postgres/webserver/metricserver/pg_collector.go)
  (`release-1.26`) and [`api/v1/cluster_types.go`](https://github.com/cloudnative-pg/cloudnative-pg/blob/release-1.26/api/v1/cluster_types.go#L889-L907):
  CNPG's own core exporter has `cnpg_collector_last_failed_backup_timestamp`/
  `cnpg_collector_last_available_backup_timestamp`, both namespaced `cnpg`, subsystem
  `collector` - but `Cluster.Status.LastFailedBackup`/`LastSuccessfulBackup`, the fields
  they read, are documented **"Deprecated: the field is not set for backup plugins"** -
  i.e. never populated for this chart's plugin-based (`barman-cloud.cloudnative-pg.io`)
  backups, only for the deprecated in-tree `barmanObjectStore` shape this chart never
  renders (the module's own "Why the plugin" section above). Using these two names in
  an alert here would silently never fire.
- [`internal/cnpi/plugin/client/metrics.go`](https://github.com/cloudnative-pg/cloudnative-pg/blob/main/internal/cnpi/plugin/client/metrics.go)
  and [`pkg/management/postgres/metrics/collector.go`](https://github.com/cloudnative-pg/cloudnative-pg/blob/main/pkg/management/postgres/metrics/collector.go#L727)
  (`main`, commit `9ed54f3`): the core exporter's own `pluginCollector` instead asks
  every CNPG-I plugin configured on the `Cluster` for its own metric definitions and
  values (`GetMetricsDefinitions`/`CollectMetrics`) and re-exposes each one verbatim
  under the plugin's own fully-qualified name - no CNPG-side renaming.
- [`internal/cnpgi/instance/metrics.go`](https://github.com/cloudnative-pg/plugin-barman-cloud/blob/main/internal/cnpgi/instance/metrics.go)
  and [`internal/cnpgi/metadata/constants.go`](https://github.com/cloudnative-pg/plugin-barman-cloud/blob/main/internal/cnpgi/metadata/constants.go)
  (`plugin-barman-cloud`, `main`): the Barman Cloud plugin's own `metricsDomain` is its
  plugin name (`barman-cloud.cloudnative-pg.io`) with every `.`/`-` replaced by `_`,
  giving the fully-qualified metric names this chart's alert actually uses:
  `barman_cloud_cloudnative_pg_io_last_available_backup_timestamp` and
  `barman_cloud_cloudnative_pg_io_last_failed_backup_timestamp` (both Unix-timestamp
  gauges, `0` until the `ObjectStore`'s own `status.serverRecoveryWindow` for this
  `Cluster`'s server name has an entry). `MemoryManagerCnpgBackupFailed` fires once the
  failed timestamp is more recent than the available one.
- [`docs/src/samples/monitoring/alerts.yaml`](https://github.com/cloudnative-pg/cloudnative-pg/blob/release-1.26/docs/src/samples/monitoring/alerts.yaml)
  and [`docs/src/monitoring.md`](https://github.com/cloudnative-pg/cloudnative-pg/blob/release-1.26/docs/src/monitoring.md#L770-L772)
  (`release-1.26`): `cnpg_pg_replication_lag` ("Replication lag behind primary in
  seconds") is CNPG's own documented default metric and the one its own sample
  `PrometheusRule` alerts on (`cnpg_pg_replication_lag > 300`) - unaffected by the
  plugin-vs-in-tree backup distinction above, since it comes from CNPG's default
  Postgres metric queries, not a backup status field. Not currently used by this
  chart's own `PrometheusRule` (#265's own scope is latency/errors/rate
  limits/embedding lag plus the two CNPG alerts above) - noted here for whichever WP
  adds a replication-lag alert next, so it does not have to re-derive this.

## Scraping the CNPG Cluster: PodMonitor, and when the plugin bridge actually exists (#265)

Retrieved: 2026-10-09 · Feeds: WP-31 (`templates/cnpg-podmonitor.yaml`, `templates/prometheusrule.yaml`'s
own `MemoryManagerCnpgBackupFailed`) - the alert above is only as good as whatever actually
scrapes the metrics it reads; this records what does.

### `enablePodMonitor` is already on its way out

The Cluster's own monitoring sub-block's own `enablePodMonitor` field (default `false`) makes the
operator render a matching `PodMonitor` itself. Diffing `cluster_types.go` at the three released
patches of the 1.26 line (the kind E2E's own pinned operator version, `docs/research/kind-e2e.md`)
shows exactly when a deprecation notice was added to it:

- `cluster_types.go` at the first patch, 1.26.1
  (https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.26.1/api/v1/cluster_types.go#L2070-L2073):
  plain `// Enable or disable the PodMonitor` + `// +kubebuilder:default:=false`, no deprecation.
- `cluster_types.go` at the next two patches, 1.26.2
  (https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.26.2/api/v1/cluster_types.go#L2070-L2078)
  and 1.26.3
  (https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.26.3/api/v1/cluster_types.go):
  the same field now carries `// Deprecated: This feature will be removed in an upcoming release.
  If you need this functionality, you can create a PodMonitor manually.`

So the exact operator patch this chart's own kind E2E pins (the first one) still has the field
working, undeprecated - but every later patch in the same 1.26 line already marks it for removal.
Depending on it chart-wide would mean starting from a flag already deprecated for most of this
chart's own supported operator range, not just a hypothetical future one - the chart instead
renders its own `PodMonitor`, the exact shape CNPG's own monitoring guide documents under "To
deploy a PodMonitor for a specific Cluster manually"
(https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.26.1/docs/src/monitoring.md#L71-L84):
`selector.matchLabels` on `cnpg.io/cluster`, one `podMetricsEndpoints` entry on the `metrics` port -
works identically whether or not the operator's own flag still exists.

### The Barman Cloud plugin's own metrics need CNPG 1.27, not just 1.26

The plugin-metrics bridge this document's own previous section describes
(`internal/cnpi/plugin/client/metrics.go`, the `pluginCollector` that calls into the plugin's own
CNPG-I `Metrics` service and re-exposes the result on the instance manager's existing `metrics`
port - no separate port of its own) does not exist at all in the CNPG core at any released patch
of the 1.26 line: a direct fetch of `internal/cnpi/plugin/client/metrics.go` 404s at the three
1.26 patch tags and at the tip of the `release-1.26` branch itself (checked 2026-10-09), and first
exists at the tag for CNPG 1.27.0 (`200`, confirmed the same way, and at every later minor
checked: 1.27.4, 1.28.0). The plugin side of the bridge (`internal/cnpgi/instance/metrics.go`,
`plugin-barman-cloud`) has existed since that project's own 0.15.1 release - the CNPG-core side is
the newer of the two.

Net effect: `barman_cloud_cloudnative_pg_io_last_available_backup_timestamp`/
`_last_failed_backup_timestamp` are not exposed anywhere a `PodMonitor` could ever scrape them
while the CNPG operator itself is still on the 1.26 line (this chart's own documented minimum,
`charts/memory-manager/templates/cnpg-cluster.yaml`'s own comment, the backup sub-block's own
prerequisites) - `MemoryManagerCnpgBackupFailed` simply never fires there, the same as any
Prometheus alert over an absent series, not a misconfiguration. It starts working with no chart
change needed once the operator is upgraded to CNPG 1.27 or later. Confirmed directly: the plugin
itself opens no HTTP port of its own either (`internal/cmd/instance/main.go`, `plugin-barman-cloud`,
checked at the 0.15.1 tag) - every metric it reports is only ever reachable through the CNPG-core
bridge above, never through a port on the plugin's own sidecar container.
