# KEDA ScaledObject: fields used by the api autoscaler (#251)

Retrieved: 2026-10-08 · Feeds: [ADR-0009](../adr/0009-stateless-replicas.md) (addendum 2026-10-08)

Checked against the KEDA v2.17 docs (the version pinned at retrieval time; the API itself,
`keda.sh/v1alpha1`, has been stable since KEDA 2.0):

- [ScaledObject specification](https://keda.sh/docs/2.17/reference/scaledobject-spec/)
- [CPU scaler](https://keda.sh/docs/2.17/scalers/cpu/)
- [Prometheus scaler](https://keda.sh/docs/2.17/scalers/prometheus/)

## `ScaledObject` shape

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
spec:
  scaleTargetRef:
    apiVersion: apps/v1      # optional, default apps/v1
    kind: Deployment          # optional, default Deployment
    name: <deployment-name>   # mandatory, same namespace
  minReplicaCount: 1          # optional, default 0
  maxReplicaCount: 100        # optional, default 100
  triggers: [...]
```

`minReplicaCount`/`maxReplicaCount` feed the HPA KEDA creates behind the scenes (named
`keda-hpa-<scaledobject-name>` unless overridden). This chart sets `api.keda.minReplicaCount` to
`3`, matching `pdb.api.minAvailable` (`2`) + 1, so KEDA never scales below what the PDB allows to
be evicted at once (`values.yaml`'s own comment).

## `cpu` trigger

```yaml
triggers:
  - type: cpu
    metricType: Utilization   # or AverageValue; Utilization matches the plain HPA's own metric type
    metadata:
      value: "70"              # target percentage, as a string
      containerName: ""        # optional, defaults to the whole pod
```

Requires the Kubernetes Metrics Server and a `resources.requests.cpu` (or `limits.cpu`) on the
target container — both already true for the api Deployment (`values.yaml`'s own `api.resources`).
The chart reuses `api.autoscaling.targetCPUUtilizationPercentage` for this trigger's `value`, so the
CPU target stays the same whichever of the HPA or the ScaledObject is active.

## `prometheus` trigger

```yaml
triggers:
  - type: prometheus
    metadata:
      serverAddress: http://<prometheus-host>:9090   # mandatory
      query: sum(rate(http_requests_total[2m]))        # mandatory, must return a scalar/vector of one element
      threshold: "100"                                 # mandatory, can be a float, as a string
```

The chart's own default query sums `mm_tool_calls_total`
(`src/memory_manager/observability/metrics.py`) across every `tool`/`outcome` label as a
requests-per-second rate over a 2-minute window: `sum(rate(mm_tool_calls_total[2m]))`. Both
`serverAddress` and `query` are overridable (`api.keda.prometheus`), since the actual Prometheus
endpoint and the request-rate signal that matters are operator-specific.

## HPA/ScaledObject mutual exclusivity

KEDA creates and owns its own `HorizontalPodAutoscaler` for every `ScaledObject`; running a
chart-rendered HPA against the same Deployment at the same time would mean two controllers racing
to set `spec.replicas`. The ScaledObject spec documentation does not special-case this — it is a
general Kubernetes HPA constraint (two HPAs, or an HPA and KEDA, must never target the same scale
subresource) — so the chart enforces it itself
(`templates/_helpers.tpl`'s `memory-manager.validateApiAutoscaling`, included by
`templates/api-deployment.yaml`): `api.autoscaling.enabled` and `api.keda.enabled` fail the render
if both are `true`.

## `kubeconform` coverage

The `datreeio/CRDs-catalog` schema source already used by this repository's `kubeconform` steps
(`.github/workflows/validate.yml`) ships a schema for `keda.sh/v1alpha1` `ScaledObject`
(`keda.sh/scaledobject_v1alpha1.json`, retrieved 2026-10-08, HTTP 200). A render with
`api.keda.enabled: true` validates against it with the existing `-schema-location` flags, with no
extra `-ignore-missing-schemas` flag needed.
