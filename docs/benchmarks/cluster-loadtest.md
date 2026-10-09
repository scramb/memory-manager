# Running the load test against the enterprise Helm profile (#270)

Feeds: [#270](https://github.com/scramb/memory-manager/issues/270) · Work package: WP-32

Method note only — `scripts/loadtest-cluster.sh` is the generic Kubernetes
runner itself; this records how to point it at a verification kind cluster
or an operator's own cluster, and what it measures. The actual
target-size run and its report are [#271](https://github.com/scramb/memory-manager/issues/271)
(not this task) — nothing in this file is a benchmark result.

## What it does

`scripts/loadtest-cluster.sh`:

1. Installs `charts/memory-manager` with `values-enterprise.yaml` plus
   `loadtest/k8s/values-loadtest.yaml` (a fixed 3-replica `api`, no HPA/
   KEDA during the measured run, CNPG superuser access) via a plain
   `helm upgrade --install --wait` — no Flux required, so this runs
   against any cluster the enterprise profile supports, not only one
   already running this project's own Flux setup.
2. Generates a synthetic `LOADTEST_NOTES`-note vault and bulk-loads it —
   chunks and the ADR-0016 HNSW index included — straight into the
   already-running chart's own database, using the CNPG superuser Secret
   (`database.cnpg.enableSuperuserAccess`, off everywhere else in this
   chart): `COPY` has no row-security evaluation path at all, so neither
   the owner role nor any policy can bulk-load into a `FORCE ROW LEVEL
   SECURITY` table (`migrations/0005_rls.sql`) — only a superuser can.
   `loadtest.load --use-existing-database` is what keeps this step from
   ever dropping/recreating that database out from under the three
   already-ready, already-serving `api` replicas.
3. Starts the embedding stub (`loadtest/embedding_stub.py`) in-cluster and
   points `EMBEDDING_URL` at it, so query-time embedding is real (deterministic,
   not a real model) rather than skipped.
4. Runs `loadtest/k6/smoke.js` in-cluster against the chart's own `api`
   Service — kube-proxy already spreads requests across all three ready
   Pods, no load balancer of its own needed.
5. If `LOADTEST_KILL_AFTER` is set, force-deletes one `api` Pod
   (`kubectl delete pod --grace-period=0 --force`) partway through the
   measured phase — a real Pod loss, not a local `SIGKILL`
   (`scripts/loadtest-smoke.sh`'s own equivalent).
6. Collects the k6 summary export, `pg_stat_user_functions`, pod/node
   facts and a record of the forced Pod deletion into
   `LOADTEST_RESULTS_DIR`, then tears every object (and, on the
   verification path below, the whole kind cluster) down.

## Verifying locally (kind)

No extra setup needed — `KUBE_CONTEXT` unset makes the script bootstrap
its own disposable kind cluster (the exact mechanics `scripts/e2e-kind.sh`
uses, `scripts/lib/kind-cluster.sh`), build and push this run's own
`memory-manager`/`memory-manager-loadtest` images to a throwaway local
registry, install the CNPG operator, run the steps above, then tear the
whole cluster down:

```sh
LOADTEST_NOTES=10000 LOADTEST_KILL_AFTER=30 scripts/loadtest-cluster.sh
```

`KEEP_CLUSTER=1` leaves the kind cluster and registry running afterwards
(the same escape hatch `scripts/e2e-kind.sh` has) for inspecting a failure.

## Running against an operator's own cluster

Set `KUBE_CONTEXT` to an existing context and `LOADTEST_REGISTRY` to a
registry reachable from it — the script then skips kind/CNPG-operator
setup entirely (the chart's own prerequisite: the CNPG operator is
already installed) and only builds, pushes and installs into that
cluster:

```sh
KUBE_CONTEXT=my-cluster LOADTEST_REGISTRY=registry.example.com/memory-manager \
  LOADTEST_NAMESPACE=mm-loadtest LOADTEST_STORAGE_CLASS=fast-ssd \
  LOADTEST_NOTES=1000000 LOADTEST_KILL_AFTER=60 LOADTEST_RESULTS_DIR=./results \
  scripts/loadtest-cluster.sh
```

Every other env var (`LOADTEST_SHARED_STATE`, `K6_IMAGE`, ...) matches
`scripts/loadtest-smoke.sh`'s own meaning where both exist — see
`scripts/loadtest-cluster.sh`'s own module docstring for the full list.

## Known limitation

The shared `PersistentVolumeClaim` the generate+load Job, the embedding
stub and the k6 Job all mount (`loadtest/k8s/pvc.yaml`) is
`ReadWriteOnce` — enough for a single-node kind cluster, where every Pod
lands on the one node anyway, but a multi-node operator cluster needs
either a storage class that supports `ReadWriteMany`/`ReadWriteOncePod`
across nodes, or the three Pods pinned to one node. Neither is solved
here — #271's own target-size run is where that choice belongs.
