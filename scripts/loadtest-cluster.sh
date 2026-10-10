#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Generic Kubernetes load-test runner (#270, WP-32): installs
# charts/memory-manager with the enterprise profile plus loadtest/k8s/
# values-loadtest.yaml (a fixed 3-replica api, no HPA/KEDA, CNPG superuser
# access), generates and bulk-loads a synthetic LOADTEST_NOTES-note vault
# straight into the already-running chart's own database (loadtest/k8s/
# generate-load-job.yaml - see that file for why it needs the CNPG
# superuser Secret, not the owner's own), points EMBEDDING_URL at the
# in-cluster embedding stub (loadtest/k8s/embedding-stub.yaml), runs
# loadtest/k6/smoke.js against the api Service and, if LOADTEST_KILL_AFTER
# is set, force-deletes one api Pod partway through the measured phase -
# mirroring scripts/loadtest-smoke.sh's own LOADTEST_KILL_AFTER, but a
# real Pod loss instead of a local SIGKILL.
#
# Generic by design (CLAUDE.md: a public repository carries no
# operator-specific values) - every cluster-specific fact is an env var,
# nothing operator-specific is ever committed:
#   KUBE_CONTEXT       - an existing cluster's context. Empty (default):
#                        this script bootstraps its own disposable kind
#                        cluster instead (scripts/lib/kind-cluster.sh, the
#                        same mechanics scripts/e2e-kind.sh uses) and
#                        tears it down on exit - the path this task's own
#                        `Fertig wenn` verifies. Given explicitly, this
#                        script assumes the CNPG operator is already
#                        installed (charts/memory-manager/README.md's own
#                        prerequisite) and never touches kind at all.
#   LOADTEST_NAMESPACE - namespace to install into (default
#                        "memory-manager"; created if missing).
#   LOADTEST_REGISTRY  - where this run's own memory-manager/
#                        memory-manager-loadtest images get pushed to and
#                        pulled from. Required with KUBE_CONTEXT set;
#                        defaults to this script's own throwaway local
#                        registry otherwise.
#   LOADTEST_STORAGE_CLASS - the CNPG Cluster's and the shared PVC's own
#                        storageClassName. Empty (default): the cluster's
#                        own default StorageClass applies.
#   LOADTEST_NOTES, LOADTEST_KILL_AFTER, LOADTEST_SHARED_STATE,
#   LOADTEST_RESULTS_DIR - same meaning as scripts/loadtest-smoke.sh's own
#                        (LOADTEST_EMBEDDINGS has no switch here - the
#                        stub always runs, there being no "none" mode
#                        worth exercising against a from-scratch chart
#                        install).
#   LOADTEST_HELM_SET  - extra, space-separated "key=value" pairs appended
#                        to this run's own `helm upgrade --install` as
#                        further `--set` flags, after every flag this
#                        script sets itself - lets a caller reach any
#                        chart value already supported (`database.cnpg.
#                        instances`, `database.cnpg.storage.size`,
#                        `database.cnpg.resources.*`, ...) without this
#                        script growing one dedicated env var per field
#                        (#271's own target-size run: disk-constrained
#                        hardware needs `database.cnpg.instances=1` and a
#                        larger `database.cnpg.storage.size`/`resources`
#                        than this chart's own git-mode-sized defaults).
#                        Never a *new* chart value - CLAUDE.md's "use
#                        chart values... where the chart allows it" is the
#                        boundary; a value the chart does not already
#                        expose (e.g. postgresql.conf's own shared_buffers)
#                        stays untunable through this flag, same as
#                        through a plain `--set`.
#   LOADTEST_REGISTRY_INSECURE - "1" pushes to LOADTEST_REGISTRY the same
#                        plain-HTTP, --tls-verify=false way this script's
#                        own bootstrapped-cluster path always does -
#                        for a LOADTEST_SKIP_GENERATE_LOAD second run
#                        pointed (via KUBE_CONTEXT/LOADTEST_REGISTRY) at a
#                        *first* run's own disposable kind cluster and
#                        throwaway local registry, which is still exactly
#                        that plain-HTTP registry, never a real one.
#                        Default "0".
#   LOADTEST_SKIP_GENERATE_LOAD - "1" skips the generate+load Job
#                        entirely and assumes `LOADTEST_NAMESPACE` already
#                        carries a loaded vault from an earlier run against
#                        the same cluster/database - for a second
#                        LOADTEST_SHARED_STATE run against the *same*
#                        target-size dataset (generating/loading 1M notes
#                        twice costs real time the comparison itself does
#                        not need, #271's own Reuse guidance) - see
#                        docs/benchmarks/cluster-loadtest.md's own "Reusing
#                        a loaded dataset across two shared-state runs"
#                        section for the exact invocation sequence (every
#                        run but the last one needs KEEP_CLUSTER/KEEP_
#                        RELEASE below, or the next run has nothing left to
#                        reuse). Default "0": every run generates and loads
#                        its own vault, same as before this flag existed.
#   KEEP_CLUSTER       - "1": on the bootstrapped path (KUBE_CONTEXT empty),
#                        leaves the kind cluster, registry and installed
#                        release running on exit instead of tearing the
#                        whole cluster down (scripts/e2e-kind.sh's own
#                        escape hatch, same name). On the existing-cluster
#                        path (KUBE_CONTEXT given), this script owns no
#                        cluster to keep - KEEP_CLUSTER=1 there instead
#                        skips the Helm-release/namespace-object teardown
#                        only (same effect KEEP_RELEASE below names more
#                        accurately for that path; either name works on
#                        either path - a caller driving both the first,
#                        bootstrapped run and a later, KUBE_CONTEXT-given
#                        run of a LOADTEST_SKIP_GENERATE_LOAD sequence needs
#                        only one flag to carry through both). Default "0".
#   KEEP_RELEASE       - "1": on the existing-cluster path, same as
#                        KEEP_CLUSTER=1 above - leaves the installed
#                        release and every namespace object as they are,
#                        for a later LOADTEST_SKIP_GENERATE_LOAD run (or a
#                        same-dataset retry) to reuse. Ignored on the
#                        bootstrapped path (that one already has KEEP_
#                        CLUSTER's own, longer-standing name). Default "0".
#
# Helm, not Flux: a generic runner should not require Flux CRDs on every
# "any cluster with the enterprise Helm profile" this is meant to run
# against (#270's own Context) - a plain `helm upgrade --install --wait`
# is simpler and exercises exactly the same rendered chart.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

LOADTEST_NOTES="${LOADTEST_NOTES:-10000}"
LOADTEST_KILL_AFTER="${LOADTEST_KILL_AFTER:-}"
LOADTEST_SHARED_STATE="${LOADTEST_SHARED_STATE:-postgres}"
LOADTEST_NAMESPACE="${LOADTEST_NAMESPACE:-memory-manager}"
LOADTEST_STORAGE_CLASS="${LOADTEST_STORAGE_CLASS:-}"
LOADTEST_RESULTS_DIR="${LOADTEST_RESULTS_DIR:-}"
LOADTEST_HELM_SET="${LOADTEST_HELM_SET:-}"
LOADTEST_SKIP_GENERATE_LOAD="${LOADTEST_SKIP_GENERATE_LOAD:-0}"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
LOADTEST_REGISTRY="${LOADTEST_REGISTRY:-}"
LOADTEST_IMAGE_TAG="${LOADTEST_IMAGE_TAG:-loadtest}"
K6_IMAGE="${K6_IMAGE:-docker.io/grafana/k6:2.3.0}"
STEP_TIMEOUT_SECONDS="${STEP_TIMEOUT_SECONDS:-900}"

# Fixed, not an env var - every manifest/values file below assumes this
# exact release name (the chart's own "memory-manager.fullname" helper
# collapses to it when the release and chart names match), same as
# deploy/flux/enterprise and scripts/e2e-kind.sh already do.
HELM_RELEASE="memory-manager"
CHART_DIR="${REPO_ROOT}/charts/memory-manager"

case "$LOADTEST_SHARED_STATE" in
  postgres|valkey) ;;
  *)
    echo "FAIL: LOADTEST_SHARED_STATE must be 'postgres' or 'valkey', got '${LOADTEST_SHARED_STATE}'" >&2
    exit 1
    ;;
esac

if [[ -n "$LOADTEST_KILL_AFTER" ]] && ! [[ "$LOADTEST_KILL_AFTER" =~ ^[0-9]+$ ]]; then
  echo "FAIL: LOADTEST_KILL_AFTER must be a non-negative integer (seconds), got '${LOADTEST_KILL_AFTER}'" >&2
  exit 1
fi

if [[ -n "$KUBE_CONTEXT" && -z "$LOADTEST_REGISTRY" ]]; then
  echo "FAIL: LOADTEST_REGISTRY is required when KUBE_CONTEXT is set (this run's own images need somewhere reachable from that cluster to push to)" >&2
  exit 1
fi

# loadtest/k6/smoke.js's own default (10s), set explicitly here (not left
# implicit) so the kill-delay arithmetic below never silently drifts from
# what the k6 Job actually uses - same reasoning scripts/
# loadtest-smoke.sh's own WARMUP_SECONDS gives.
WARMUP_SECONDS=10
MEASURE_SECONDS="${MM_MEASURE_DURATION_SECONDS:-60}"

TOOLS_DIR="$(mktemp -d)"
BOOTSTRAPPED_OWN_CLUSTER=0
CREATED_NAMESPACE=0

# --- cluster: either an existing one (KUBE_CONTEXT) or a disposable kind
# cluster this script bootstraps itself, reusing the exact mechanics
# scripts/e2e-kind.sh uses (scripts/lib/kind-cluster.sh) --------------------
source "${REPO_ROOT}/scripts/lib/kind-cluster.sh"

if [[ -z "$KUBE_CONTEXT" ]]; then
  BOOTSTRAPPED_OWN_CLUSTER=1
  KIND_VERSION="${KIND_VERSION:-v0.30.0}"
  KIND_NODE_IMAGE="${KIND_NODE_IMAGE:-kindest/node:v1.33.4@sha256:25a6018e48dfcaee478f4a59af81157a437f15e6e140bf103f85a2e7cd0cbbf2}"
  CNPG_OPERATOR_VERSION="${CNPG_OPERATOR_VERSION:-1.26.1}"
  CLUSTER_NAME="${CLUSTER_NAME:-mm-loadtest}"
  # A different host port than scripts/e2e-kind.sh's own default (5001) -
  # both can run side by side without colliding.
  REGISTRY_NAME="${REGISTRY_NAME:-mm-loadtest-registry}"
  REGISTRY_HOST_PORT="${REGISTRY_HOST_PORT:-5002}"
  KEEP_CLUSTER="${KEEP_CLUSTER:-0}"

  mm_detect_container_engine
  mm_install_pinned_kind "$KIND_VERSION" "$TOOLS_DIR"
  export PATH="${TOOLS_DIR}:${PATH}"

  KUBE_CONTEXT="kind-${CLUSTER_NAME}"
  LOADTEST_REGISTRY="localhost:${REGISTRY_HOST_PORT}"
else
  mm_detect_container_engine
fi

KCTL=(kubectl --context "$KUBE_CONTEXT")

dump_diagnostics() {
  echo "--- diagnostics: pods ---" >&2
  "${KCTL[@]}" get pods -n "$LOADTEST_NAMESPACE" -o wide >&2 2>&1 || true
  echo "--- diagnostics: cluster (CNPG) ---" >&2
  "${KCTL[@]}" get cluster -n "$LOADTEST_NAMESPACE" -o yaml >&2 2>&1 || true
  echo "--- diagnostics: jobs ---" >&2
  "${KCTL[@]}" get jobs -n "$LOADTEST_NAMESPACE" -o wide >&2 2>&1 || true
  echo "--- diagnostics: pod logs (current + previous) ---" >&2
  for pod in $("${KCTL[@]}" get pods -n "$LOADTEST_NAMESPACE" -o name 2>/dev/null); do
    echo "== ${pod} ==" >&2
    "${KCTL[@]}" logs -n "$LOADTEST_NAMESPACE" "$pod" --all-containers >&2 2>&1 || true
    "${KCTL[@]}" logs -n "$LOADTEST_NAMESPACE" "$pod" --all-containers --previous >&2 2>&1 || true
  done
  echo "--- diagnostics: events ---" >&2
  "${KCTL[@]}" get events -n "$LOADTEST_NAMESPACE" --sort-by=.lastTimestamp >&2 2>&1 || true
}

cleanup() {
  local status=$?
  if [[ $status -ne 0 ]]; then
    echo "FAIL: loadtest-cluster.sh exiting with status ${status} - dumping diagnostics" >&2
    dump_diagnostics
  fi
  # KEEP_CLUSTER=1 (bootstrapped path only) leaves everything exactly as
  # it was at failure - same promise scripts/e2e-kind.sh's own
  # KEEP_CLUSTER=1 makes: the whole point is inspecting the *installed*
  # release, not just a bare, already-uninstalled cluster.
  if [[ "$BOOTSTRAPPED_OWN_CLUSTER" == "1" && "${KEEP_CLUSTER:-0}" == "1" ]]; then
    echo "KEEP_CLUSTER=1 set - leaving cluster '${CLUSTER_NAME}', registry '${REGISTRY_NAME}' and the installed release running" >&2
    rm -rf "$TOOLS_DIR"
    exit "$status"
  fi

  # Deleting the whole kind cluster below makes tearing down individual
  # namespace objects first pure wasted time (and, worse, a namespace
  # delete with a CNPG Cluster finalizer still in it can hang for
  # minutes) - skip straight to the cluster delete whenever this run
  # bootstrapped its own.
  if [[ "$BOOTSTRAPPED_OWN_CLUSTER" == "1" ]]; then
    echo "cleaning up: kind cluster '${CLUSTER_NAME}', registry '${REGISTRY_NAME}'" >&2
    "${KIND[@]}" delete cluster --name "$CLUSTER_NAME" >/dev/null 2>&1 || true
    "${CTR[@]}" rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
    rm -rf "$TOOLS_DIR"
    exit "$status"
  fi

  # KEEP_CLUSTER=1 or KEEP_RELEASE=1 (the existing-cluster path, KUBE_CONTEXT
  # given - #271's own Postgres-run -> Valkey-run -> optional-retry reuse
  # sequence, docs/benchmarks/cluster-loadtest.md's own "Reusing a loaded
  # dataset" section): leave the installed release, the loaded database and
  # every namespace object exactly as they are, same promise the bootstrapped
  # path's own KEEP_CLUSTER=1 already makes - this is the one thing that was
  # missing for that reuse sequence to survive a run that KUBE_CONTEXT (not
  # a self-bootstrapped kind cluster) pointed at: every earlier invocation of
  # this script against an existing cluster tore the release down on exit
  # unconditionally, destroying the just-loaded 1M-note/5M-chunk database
  # before a planned second (`LOADTEST_SKIP_GENERATE_LOAD=1`) run or retry
  # ever got to reuse it. Either variable name works here (KEEP_CLUSTER
  # because that is what the bootstrapped path already calls it and callers
  # running both paths in the same session need only set one flag; KEEP_
  # RELEASE because there is no "cluster" this script owns on this path to
  # keep - it never created one).
  if [[ "${KEEP_CLUSTER:-0}" == "1" || "${KEEP_RELEASE:-0}" == "1" ]]; then
    echo "KEEP_CLUSTER/KEEP_RELEASE=1 set - leaving release '${HELM_RELEASE}' and namespace '${LOADTEST_NAMESPACE}' as they are" >&2
    rm -rf "$TOOLS_DIR"
    exit "$status"
  fi

  echo "tearing down: helm release, namespace objects" >&2
  helm uninstall "$HELM_RELEASE" --kube-context "$KUBE_CONTEXT" -n "$LOADTEST_NAMESPACE" >/dev/null 2>&1 || true
  "${KCTL[@]}" delete job mm-loadtest-generate-load mm-loadtest-k6 -n "$LOADTEST_NAMESPACE" \
    --ignore-not-found >/dev/null 2>&1 || true
  "${KCTL[@]}" delete deployment mm-loadtest-embedding-stub -n "$LOADTEST_NAMESPACE" \
    --ignore-not-found >/dev/null 2>&1 || true
  "${KCTL[@]}" delete service embedding-stub -n "$LOADTEST_NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
  "${KCTL[@]}" delete configmap mm-loadtest-k6-scripts -n "$LOADTEST_NAMESPACE" \
    --ignore-not-found >/dev/null 2>&1 || true
  "${KCTL[@]}" delete pvc mm-loadtest-data -n "$LOADTEST_NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
  if [[ "$CREATED_NAMESPACE" == "1" ]]; then
    # --wait=false: this script's own exit must never hang on a
    # namespace stuck in "Terminating" (a CNPG Cluster finalizer can take
    # a while) - best-effort, same as every other delete above.
    "${KCTL[@]}" delete namespace "$LOADTEST_NAMESPACE" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  rm -rf "$TOOLS_DIR"
  exit "$status"
}
trap cleanup EXIT

if [[ "$BOOTSTRAPPED_OWN_CLUSTER" == "1" ]]; then
  mm_start_local_registry "$REGISTRY_NAME" "$REGISTRY_HOST_PORT"
  mm_create_kind_cluster "$CLUSTER_NAME" "$KIND_NODE_IMAGE" "$TOOLS_DIR"
  mm_wire_registry_into_kind "$CLUSTER_NAME" "$REGISTRY_NAME" "$REGISTRY_HOST_PORT"
  mm_install_cnpg_operator "$CNPG_OPERATOR_VERSION" "$KUBE_CONTEXT"
fi

# --- namespace ---------------------------------------------------------------
if "${KCTL[@]}" get namespace "$LOADTEST_NAMESPACE" >/dev/null 2>&1; then
  echo "namespace '${LOADTEST_NAMESPACE}' already exists - reusing it"
else
  "${KCTL[@]}" create namespace "$LOADTEST_NAMESPACE"
  CREATED_NAMESPACE=1
fi

# --- build + push this run's own images --------------------------------------
# Only the throwaway local registry (this script's own bootstrap path, or
# LOADTEST_REGISTRY_INSECURE=1 - a second, KUBE_CONTEXT-given invocation
# deliberately pointed back at a *first* invocation's own already-running
# throwaway registry, #271's own LOADTEST_SKIP_GENERATE_LOAD reuse recipe)
# needs --tls-verify=false - an operator's own LOADTEST_REGISTRY is
# expected to be a real, properly-TLS'd registry, same as every other
# push this repository ever does (CLAUDE.md: generic, nothing
# operator-specific assumed broken on purpose).
LOADTEST_REGISTRY_INSECURE="${LOADTEST_REGISTRY_INSECURE:-0}"
push_image() {
  if [[ "$ENGINE" == podman && ( "$BOOTSTRAPPED_OWN_CLUSTER" == "1" || "$LOADTEST_REGISTRY_INSECURE" == "1" ) ]]; then
    "${CTR[@]}" push --tls-verify=false "$1"
  else
    "${CTR[@]}" push "$1"
  fi
}

MM_IMAGE="${LOADTEST_REGISTRY}/memory-manager:${LOADTEST_IMAGE_TAG}"
LOADTEST_IMAGE="${LOADTEST_REGISTRY}/memory-manager-loadtest:${LOADTEST_IMAGE_TAG}"

echo "--- building memory-manager:${LOADTEST_IMAGE_TAG} ---"
MM_GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
"${CTR[@]}" build -t "$MM_IMAGE" --build-arg "MM_GIT_SHA=${MM_GIT_SHA}" -f "${REPO_ROOT}/Dockerfile" "$REPO_ROOT"
push_image "$MM_IMAGE"

echo "--- building memory-manager-loadtest:${LOADTEST_IMAGE_TAG} ---"
"${CTR[@]}" build -t "$LOADTEST_IMAGE" --build-arg "BASE_IMAGE=${MM_IMAGE}" \
  -f "${REPO_ROOT}/loadtest/k8s/Containerfile" "$REPO_ROOT"
push_image "$LOADTEST_IMAGE"

# --- install the chart (enterprise profile + the loadtest overlay) -----------
HELM_ARGS=(
  upgrade --install "$HELM_RELEASE" "$CHART_DIR"
  --kube-context "$KUBE_CONTEXT" --namespace "$LOADTEST_NAMESPACE"
  -f "${CHART_DIR}/values-enterprise.yaml"
  -f "${REPO_ROOT}/loadtest/k8s/values-loadtest.yaml"
  --set "image.repository=${LOADTEST_REGISTRY}/memory-manager"
  --set "image.tag=${LOADTEST_IMAGE_TAG}"
  # LOADTEST_IMAGE_TAG is fixed (default "loadtest"), not unique per run -
  # a repeated run must never serve a kubelet-cached image from an
  # earlier one.
  --set image.pullPolicy=Always
  --set embedding.provider=openai
  --set "embedding.url=http://embedding-stub.${LOADTEST_NAMESPACE}.svc.cluster.local:8081/v1"
  --set embedding.model=loadtest-stub
  --set embedding.dimensions=1024
  --wait --timeout "${STEP_TIMEOUT_SECONDS}s"
)
if [[ -n "$LOADTEST_STORAGE_CLASS" ]]; then
  HELM_ARGS+=(--set "database.cnpg.storage.storageClassName=${LOADTEST_STORAGE_CLASS}")
fi
if [[ "$LOADTEST_SHARED_STATE" == "valkey" ]]; then
  HELM_ARGS+=(--set valkey.enabled=true)
fi
if [[ -n "$LOADTEST_HELM_SET" ]]; then
  for kv in $LOADTEST_HELM_SET; do
    HELM_ARGS+=(--set "$kv")
  done
fi

echo "--- installing ${HELM_RELEASE} (enterprise + loadtest overlay) into ${LOADTEST_NAMESPACE} ---"
helm "${HELM_ARGS[@]}"

echo "--- waiting for 3 api pods ---"
api_ready_deadline=$((SECONDS + STEP_TIMEOUT_SECONDS))
while true; do
  mapfile -t api_pods < <("${KCTL[@]}" get pods -n "$LOADTEST_NAMESPACE" \
    -l "app.kubernetes.io/instance=${HELM_RELEASE},app.kubernetes.io/component=api" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
  [[ "${#api_pods[@]}" -eq 3 ]] && break
  if (( SECONDS > api_ready_deadline )); then
    echo "FAIL: never saw exactly 3 api pods (currently ${#api_pods[@]})" >&2
    exit 1
  fi
  sleep 5
done
echo "OK: 3 api pods (${api_pods[*]})"

# --- track_functions (#271: pg_stat_user_functions self-time needs this -
# off by default, and this chart exposes no postgresql.conf override) ------
# ALTER DATABASE, not ALTER SYSTEM: CNPG's own instance manager owns
# postgresql.auto.conf (confirmed: a plain psql ALTER SYSTEM against the
# superuser Secret fails "could not open file \"postgresql.auto.conf\":
# Permission denied" on a CNPG-managed instance) - ALTER DATABASE instead
# writes into pg_db_role_setting (a catalog row, no file access needed)
# and takes effect for every *new* connection to this database from the
# moment it commits. The already-running 3 api + 2 worker Pods' own
# connection pools opened theirs before this point, though, so a rollout
# restart of both right after is what actually gets every in-flight
# backend to reconnect and pick it up - cheap and safe (ADR-0009: both
# Deployments are stateless by design) this early, before any load has
# run.
echo "--- enabling pg_stat_user_functions (track_functions = all) ---"
cnpg_cluster_name="$("${KCTL[@]}" get cluster -n "$LOADTEST_NAMESPACE" -o jsonpath='{.items[0].metadata.name}')"
cnpg_su_uri="$("${KCTL[@]}" get secret "${cnpg_cluster_name}-superuser" -n "$LOADTEST_NAMESPACE" \
  -o jsonpath='{.data.uri}' | base64 -d)"
"${KCTL[@]}" exec -n "$LOADTEST_NAMESPACE" "${cnpg_cluster_name}-1" -c postgres -- \
  psql "${cnpg_su_uri%/*}/memory_manager" -v ON_ERROR_STOP=1 \
  -c "alter database memory_manager set track_functions = 'all';"
"${KCTL[@]}" rollout restart "deployment/${HELM_RELEASE}-api" "deployment/${HELM_RELEASE}-worker" \
  -n "$LOADTEST_NAMESPACE"
"${KCTL[@]}" rollout status "deployment/${HELM_RELEASE}-api" -n "$LOADTEST_NAMESPACE" \
  --timeout="${STEP_TIMEOUT_SECONDS}s"
"${KCTL[@]}" rollout status "deployment/${HELM_RELEASE}-worker" -n "$LOADTEST_NAMESPACE" \
  --timeout="${STEP_TIMEOUT_SECONDS}s"
echo "OK: track_functions = all (api/worker restarted onto it)"

# --- shared PVC (generate+load Job, embedding stub, k6 Job) ------------------
echo "--- creating the shared PVC ---"
PVC_YAML="$(sed \
  -e "s/__LOADTEST_PVC_SIZE__/${LOADTEST_PVC_SIZE:-20Gi}/" \
  "${REPO_ROOT}/loadtest/k8s/pvc.yaml")"
if [[ -n "$LOADTEST_STORAGE_CLASS" ]]; then
  PVC_YAML="$(echo "$PVC_YAML" | sed "s/__LOADTEST_STORAGE_CLASS__/${LOADTEST_STORAGE_CLASS}/")"
else
  PVC_YAML="$(echo "$PVC_YAML" | grep -v '__LOADTEST_STORAGE_CLASS__')"
fi
echo "$PVC_YAML" | "${KCTL[@]}" apply -n "$LOADTEST_NAMESPACE" -f -

# --- generate + load (#270's own 32a/32b) -------------------------------------
MCP_BASE_URL="http://${HELM_RELEASE}.${LOADTEST_NAMESPACE}.svc.cluster.local:8080/mcp"
GENERATE_LOAD_START=""
GENERATE_LOAD_END=""
if [[ "$LOADTEST_SKIP_GENERATE_LOAD" == "1" ]]; then
  echo "--- LOADTEST_SKIP_GENERATE_LOAD=1: reusing the already-loaded vault in ${LOADTEST_NAMESPACE} ---"
  "${KCTL[@]}" get job mm-loadtest-generate-load -n "$LOADTEST_NAMESPACE" \
    -o jsonpath='{.status.succeeded}' | grep -q '^1$' \
    || {
      echo "FAIL: LOADTEST_SKIP_GENERATE_LOAD=1 but mm-loadtest-generate-load has not succeeded in ${LOADTEST_NAMESPACE} - nothing to reuse" >&2
      exit 1
    }
  echo "OK: mm-loadtest-generate-load already succeeded - skipping"
else
  echo "--- generating ${LOADTEST_NOTES} notes and loading them via the CNPG superuser secret ---"
  sed \
    -e "s|__LOADTEST_IMAGE__|${LOADTEST_IMAGE}|g" \
    -e "s/__LOADTEST_NOTES__/${LOADTEST_NOTES}/" \
    -e "s/__LOADTEST_APP_ROLE__/memory_manager_app/" \
    -e "s|__LOADTEST_MCP_BASE_URL__|${MCP_BASE_URL}|" \
    "${REPO_ROOT}/loadtest/k8s/generate-load-job.yaml" \
    | "${KCTL[@]}" apply -n "$LOADTEST_NAMESPACE" -f -

  GENERATE_LOAD_START=$(date +%s.%N)
  generate_load_deadline=$((SECONDS + STEP_TIMEOUT_SECONDS))
  while true; do
    succeeded="$("${KCTL[@]}" get job mm-loadtest-generate-load -n "$LOADTEST_NAMESPACE" \
      -o jsonpath='{.status.succeeded}' 2>/dev/null || echo 0)"
    failed="$("${KCTL[@]}" get job mm-loadtest-generate-load -n "$LOADTEST_NAMESPACE" \
      -o jsonpath='{.status.failed}' 2>/dev/null || echo 0)"
    [[ "$succeeded" == "1" ]] && break
    if [[ "$failed" == "1" ]]; then
      echo "FAIL: mm-loadtest-generate-load Job failed" >&2
      exit 1
    fi
    if (( SECONDS > generate_load_deadline )); then
      echo "FAIL: mm-loadtest-generate-load Job did not complete within ${STEP_TIMEOUT_SECONDS}s" >&2
      exit 1
    fi
    sleep 5
  done
  GENERATE_LOAD_END=$(date +%s.%N)
  echo "OK: generate+load Job completed"
fi

# --- embedding stub (#270's own 32c) ------------------------------------------
echo "--- starting the embedding stub ---"
sed "s|__LOADTEST_IMAGE__|${LOADTEST_IMAGE}|g" "${REPO_ROOT}/loadtest/k8s/embedding-stub.yaml" \
  | "${KCTL[@]}" apply -n "$LOADTEST_NAMESPACE" -f -
"${KCTL[@]}" wait --for=condition=Available deployment/mm-loadtest-embedding-stub \
  -n "$LOADTEST_NAMESPACE" --timeout="${STEP_TIMEOUT_SECONDS}s"
echo "OK: embedding stub Available"

# --- reset pg_stat_user_functions right before the measured run (#271) ------
# `pg_stat_reset()`, after generate+load (whose own heavy COPY/HNSW-build
# queries are not the request path #271 measures) and before k6 (so this
# run's own self-time numbers are never diluted by an earlier run's calls
# against the same, reused database - LOADTEST_SKIP_GENERATE_LOAD's whole
# point).
echo "--- resetting pg_stat_user_functions before the measured run ---"
"${KCTL[@]}" exec -n "$LOADTEST_NAMESPACE" "${cnpg_cluster_name}-1" -c postgres -- \
  psql "${cnpg_su_uri%/*}/memory_manager" -v ON_ERROR_STOP=1 -c "select pg_stat_reset();" >/dev/null

# --- k6 (#270's own 32d, the k6 Job) ------------------------------------------
echo "--- creating the k6 scripts ConfigMap from loadtest/k6/*.js ---"
"${KCTL[@]}" create configmap mm-loadtest-k6-scripts -n "$LOADTEST_NAMESPACE" \
  --from-file="${REPO_ROOT}/loadtest/k6" --dry-run=client -o yaml \
  | "${KCTL[@]}" apply -f -

echo "--- running k6 (loadtest/k6/smoke.js) against ${MCP_BASE_URL%/mcp} ---"
# Delete any earlier mm-loadtest-k6 Job first (--wait: its Pod has to be
# gone, not just the Job object, before the apply below creates a new one
# under the same name) - a plain re-apply of an unchanged Job spec is a
# no-op against an already-Completed Job (#271's own reuse path: a second
# LOADTEST_SHARED_STATE run against the same namespace must still measure
# its own, fresh k6 run, never read back the first run's result).
"${KCTL[@]}" delete job mm-loadtest-k6 -n "$LOADTEST_NAMESPACE" --ignore-not-found --wait=true \
  >/dev/null 2>&1 || true
sed \
  -e "s/__LOADTEST_WARMUP_SECONDS__/${WARMUP_SECONDS}/" \
  -e "s/__LOADTEST_MEASURE_SECONDS__/${MEASURE_SECONDS}/" \
  -e "s|__K6_IMAGE__|${K6_IMAGE}|" \
  "${REPO_ROOT}/loadtest/k8s/k6-job.yaml" \
  | "${KCTL[@]}" apply -n "$LOADTEST_NAMESPACE" -f -

KILLED_POD=""
KILL_TIMESTAMP=""
if [[ -n "$LOADTEST_KILL_AFTER" ]]; then
  (
    sleep "$((WARMUP_SECONDS + LOADTEST_KILL_AFTER))"
    victim="$("${KCTL[@]}" get pods -n "$LOADTEST_NAMESPACE" \
      -l "app.kubernetes.io/instance=${HELM_RELEASE},app.kubernetes.io/component=api" \
      -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    if [[ -n "$victim" ]]; then
      "${KCTL[@]}" delete pod "$victim" -n "$LOADTEST_NAMESPACE" --grace-period=0 --force \
        >/dev/null 2>&1 || true
      echo "killed api pod ${victim} (force, grace-period=0) $((WARMUP_SECONDS + LOADTEST_KILL_AFTER))s into the k6 run (#270)"
      date -u +%Y-%m-%dT%H:%M:%SZ > "${TOOLS_DIR}/kill-timestamp"
      echo "$victim" > "${TOOLS_DIR}/killed-pod"
    fi
  ) &
  killer_bg_pid=$!
fi

k6_deadline=$((SECONDS + STEP_TIMEOUT_SECONDS))
while true; do
  succeeded="$("${KCTL[@]}" get job mm-loadtest-k6 -n "$LOADTEST_NAMESPACE" \
    -o jsonpath='{.status.succeeded}' 2>/dev/null || echo 0)"
  failed="$("${KCTL[@]}" get job mm-loadtest-k6 -n "$LOADTEST_NAMESPACE" \
    -o jsonpath='{.status.failed}' 2>/dev/null || echo 0)"
  [[ "$succeeded" == "1" || "$failed" == "1" ]] && break
  if (( SECONDS > k6_deadline )); then
    echo "FAIL: mm-loadtest-k6 Job did not finish within ${STEP_TIMEOUT_SECONDS}s" >&2
    exit 1
  fi
  sleep 5
done
if [[ -n "${killer_bg_pid:-}" ]]; then
  wait "$killer_bg_pid" 2>/dev/null || true
  [[ -f "${TOOLS_DIR}/killed-pod" ]] && KILLED_POD="$(cat "${TOOLS_DIR}/killed-pod")"
  [[ -f "${TOOLS_DIR}/kill-timestamp" ]] && KILL_TIMESTAMP="$(cat "${TOOLS_DIR}/kill-timestamp")"
fi

k6_pod="$("${KCTL[@]}" get pods -n "$LOADTEST_NAMESPACE" -l job-name=mm-loadtest-k6 \
  -o jsonpath='{.items[0].metadata.name}')"
k6_exit="$("${KCTL[@]}" get pod "$k6_pod" -n "$LOADTEST_NAMESPACE" \
  -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}')"
echo "k6 Job pod ${k6_pod} exited ${k6_exit}"

# --- collect results (#270's own Guidelines: k6 summary, pg_stat_user_functions,
# pod restarts, node/resource facts) -------------------------------------------
if [[ -n "$LOADTEST_RESULTS_DIR" ]]; then
  mkdir -p "$LOADTEST_RESULTS_DIR"
  echo "--- collecting results into ${LOADTEST_RESULTS_DIR} ---"
  stub_pod="$("${KCTL[@]}" get pods -n "$LOADTEST_NAMESPACE" \
    -l app.kubernetes.io/name=mm-loadtest-embedding-stub \
    -o jsonpath='{.items[0].metadata.name}')"
  "${KCTL[@]}" exec -n "$LOADTEST_NAMESPACE" "$stub_pod" -- \
    cat /data/results/summary.json > "${LOADTEST_RESULTS_DIR}/summary.json" 2>/dev/null || true
  "${KCTL[@]}" exec -n "$LOADTEST_NAMESPACE" "$stub_pod" -- \
    cat /data/vault-out/chunk-results.json > "${LOADTEST_RESULTS_DIR}/chunk-results.json" 2>/dev/null || true

  cnpg_cluster="$("${KCTL[@]}" get cluster -n "$LOADTEST_NAMESPACE" -o jsonpath='{.items[0].metadata.name}')"
  su_uri="$("${KCTL[@]}" get secret "${cnpg_cluster}-superuser" -n "$LOADTEST_NAMESPACE" \
    -o jsonpath='{.data.uri}' | base64 -d)"
  su_target="${su_uri%/*}/memory_manager"
  "${KCTL[@]}" exec -n "$LOADTEST_NAMESPACE" "${cnpg_cluster}-1" -c postgres -- \
    psql "$su_target" -v ON_ERROR_STOP=1 \
    -c "copy (select funcname, calls, total_time, self_time from pg_stat_user_functions order by funcname) to stdout with csv header" \
    > "${LOADTEST_RESULTS_DIR}/pg_stat_user_functions.csv" 2>/dev/null || true

  "${KCTL[@]}" get pods -n "$LOADTEST_NAMESPACE" -o wide > "${LOADTEST_RESULTS_DIR}/pods.txt" 2>/dev/null || true
  "${KCTL[@]}" get nodes -o json > "${LOADTEST_RESULTS_DIR}/nodes.json" 2>/dev/null || true
  "${KCTL[@]}" get storageclass -o wide > "${LOADTEST_RESULTS_DIR}/storageclasses.txt" 2>/dev/null || true

  if [[ "$LOADTEST_SKIP_GENERATE_LOAD" == "1" ]]; then
    generate_load_s="null"
  else
    generate_load_s="$(awk "BEGIN { print ${GENERATE_LOAD_END:-0} - ${GENERATE_LOAD_START:-0} }")"
  fi
  jq -n \
    --argjson notes "$LOADTEST_NOTES" \
    --arg shared_state "$LOADTEST_SHARED_STATE" \
    --argjson kill_after "${LOADTEST_KILL_AFTER:-null}" \
    --arg killed_pod "$KILLED_POD" \
    --arg kill_timestamp "$KILL_TIMESTAMP" \
    --argjson generate_load_s "$generate_load_s" \
    --argjson reused_vault "$([[ "$LOADTEST_SKIP_GENERATE_LOAD" == "1" ]] && echo true || echo false)" \
    --argjson k6_exit "$k6_exit" \
    --arg namespace "$LOADTEST_NAMESPACE" \
    '{
      notes: $notes,
      replicas: 3,
      shared_state: $shared_state,
      kill_after_seconds: $kill_after,
      forced_pod_deletion: (if $killed_pod == "" then null else {pod: $killed_pod, at: $kill_timestamp} end),
      step_seconds: {generate_and_load: $generate_load_s},
      reused_vault: $reused_vault,
      namespace: $namespace,
      k6_exit: $k6_exit
    }' > "${LOADTEST_RESULTS_DIR}/timings.json"
fi

if [[ "$k6_exit" != "0" ]]; then
  echo "FAIL: k6 exited ${k6_exit}" >&2
  exit "$k6_exit"
fi

echo "loadtest-cluster OK: 3 api pods, generate+load and k6 Jobs completed"
