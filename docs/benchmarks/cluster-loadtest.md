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

The k6 latency thresholds stay at the F-01 targets here too, and a kind
cluster nested in rootful podman on one 8-vCPU node sits close to them:
of four runs after #293, three exited 0 and one failed only on
`http_req_duration{scenario:search_vector_only}` (p95 309 ms against
300 ms), with both correctness checks at 100 %. A single red run on kind
is worth repeating before reading anything into it; the target-size
numbers come from #271's report, not from this smoke.

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

## Reusing a loaded dataset across two shared-state runs (and an optional retry)

Generating and loading a 1M-note/~5M-chunk vault is expensive enough that
re-doing it for every `LOADTEST_SHARED_STATE` value - or for a same-dataset
retry after a red run - is wasted time (#271): `LOADTEST_SKIP_GENERATE_LOAD=1`
skips the generate+load Job entirely and reuses whatever vault is already
loaded into `LOADTEST_NAMESPACE`'s database. The full sequence is three
invocations - Postgres run, then Valkey run, then an *optional* retry of
either - and **every run but the last one must set `KEEP_CLUSTER=1` (or
`KEEP_RELEASE=1`, same effect on this, the existing-cluster path)**, or the
next run in the sequence finds nothing left to reuse: on the existing-
cluster path (`KUBE_CONTEXT` given, the second and third invocations below),
this script's own cleanup trap uninstalls the Helm release - and so the CNPG
`Cluster` and the loaded database with it - on every exit unless one of
those two variables says otherwise; that applied even when `KEEP_CLUSTER=1`
was only given to the *first* (bootstrapped) invocation, which is a
different flag scope - #271's own first attempt at this sequence lost its
loaded dataset exactly this way, between the Postgres run and the planned
Valkey run, from a bare second invocation with `KUBE_CONTEXT` set and
neither variable given.

```sh
# Run 1 (postgres shared state): bootstraps its own kind cluster, loads the
# vault, measures - KEEP_CLUSTER=1 leaves the cluster, registry *and* the
# installed release running afterwards (this run owns the cluster, so this
# is the bootstrapped path's own meaning of KEEP_CLUSTER).
LOADTEST_NOTES=1000000 LOADTEST_SHARED_STATE=postgres LOADTEST_KILL_AFTER=30 \
  LOADTEST_HELM_SET="database.cnpg.instances=1 database.cnpg.storage.size=40Gi" \
  LOADTEST_RESULTS_DIR=./results/postgres KEEP_CLUSTER=1 scripts/loadtest-cluster.sh

# Run 2 (valkey shared state): points KUBE_CONTEXT/LOADTEST_REGISTRY back at
# that same cluster/registry, skips generate+load, upgrades the release in
# place (valkey.enabled=true) and measures again. LOADTEST_REGISTRY_
# INSECURE=1 because that registry is still run 1's own plain-HTTP
# throwaway one, never a real operator registry. KEEP_CLUSTER=1 (or
# KEEP_RELEASE=1) here too - this is the existing-cluster path, so it skips
# the release/namespace-object teardown, not a cluster delete (there is no
# cluster for this invocation to delete) - needed again if a retry (below)
# follows, same as after run 1.
LOADTEST_NOTES=1000000 LOADTEST_SHARED_STATE=valkey LOADTEST_KILL_AFTER=30 \
  LOADTEST_SKIP_GENERATE_LOAD=1 LOADTEST_REGISTRY_INSECURE=1 \
  KUBE_CONTEXT=kind-mm-loadtest LOADTEST_REGISTRY=localhost:5002 \
  LOADTEST_HELM_SET="database.cnpg.instances=1 database.cnpg.storage.size=40Gi" \
  LOADTEST_RESULTS_DIR=./results/valkey KEEP_CLUSTER=1 scripts/loadtest-cluster.sh

# Run 3 (optional retry, e.g. of run 1 after a one-off red k6 threshold):
# same shape as run 2 - KUBE_CONTEXT/LOADTEST_REGISTRY/LOADTEST_REGISTRY_
# INSECURE/LOADTEST_SKIP_GENERATE_LOAD unchanged, LOADTEST_SHARED_STATE set
# back to whichever run is being retried. This is the *last* invocation in
# the sequence, so KEEP_CLUSTER/KEEP_RELEASE is intentionally left unset -
# its own normal cleanup tears the release and namespace objects down (but,
# KUBE_CONTEXT given, nothing cluster-level) once this run's own results are
# collected. Delete the kind cluster/registry yourself afterwards
# (`kind delete cluster --name mm-loadtest`, `podman rm -f mm-loadtest-registry`).
LOADTEST_NOTES=1000000 LOADTEST_SHARED_STATE=postgres LOADTEST_KILL_AFTER=30 \
  LOADTEST_SKIP_GENERATE_LOAD=1 LOADTEST_REGISTRY_INSECURE=1 \
  KUBE_CONTEXT=kind-mm-loadtest LOADTEST_REGISTRY=localhost:5002 \
  LOADTEST_HELM_SET="database.cnpg.instances=1 database.cnpg.storage.size=40Gi" \
  LOADTEST_RESULTS_DIR=./results/postgres-retry scripts/loadtest-cluster.sh
```

`pg_stat_user_functions` is reset (`pg_stat_reset()`) right before each
run's own k6 Job starts, so a later run's self-time numbers are never
diluted by an earlier run's own calls against the same, reused database.

## Known limitation

The shared `PersistentVolumeClaim` the generate+load Job, the embedding
stub and the k6 Job all mount (`loadtest/k8s/pvc.yaml`) is
`ReadWriteOnce` — enough for a single-node kind cluster, where every Pod
lands on the one node anyway, but a multi-node operator cluster needs
either a storage class that supports `ReadWriteMany`/`ReadWriteOncePod`
across nodes, or the three Pods pinned to one node. Neither is solved
here — #271's own target-size run is where that choice belongs.
