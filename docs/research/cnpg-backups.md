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
