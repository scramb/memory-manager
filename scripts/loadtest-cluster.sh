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
# Only the throwaway local registry (this script's own bootstrap path)
# needs --tls-verify=false - an operator's own LOADTEST_REGISTRY is
# expected to be a real, properly-TLS'd registry, same as every other
# push this repository ever does (CLAUDE.md: generic, nothing
# operator-specific assumed broken on purpose).
push_image() {
  if [[ "$BOOTSTRAPPED_OWN_CLUSTER" == "1" && "$ENGINE" == podman ]]; then
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
echo "--- generating ${LOADTEST_NOTES} notes and loading them via the CNPG superuser secret ---"
MCP_BASE_URL="http://${HELM_RELEASE}.${LOADTEST_NAMESPACE}.svc.cluster.local:8080/mcp"
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

# --- embedding stub (#270's own 32c) ------------------------------------------
echo "--- starting the embedding stub ---"
sed "s|__LOADTEST_IMAGE__|${LOADTEST_IMAGE}|g" "${REPO_ROOT}/loadtest/k8s/embedding-stub.yaml" \
  | "${KCTL[@]}" apply -n "$LOADTEST_NAMESPACE" -f -
"${KCTL[@]}" wait --for=condition=Available deployment/mm-loadtest-embedding-stub \
  -n "$LOADTEST_NAMESPACE" --timeout="${STEP_TIMEOUT_SECONDS}s"
echo "OK: embedding stub Available"

# --- k6 (#270's own 32d, the k6 Job) ------------------------------------------
echo "--- creating the k6 scripts ConfigMap from loadtest/k6/*.js ---"
"${KCTL[@]}" create configmap mm-loadtest-k6-scripts -n "$LOADTEST_NAMESPACE" \
  --from-file="${REPO_ROOT}/loadtest/k6" --dry-run=client -o yaml \
  | "${KCTL[@]}" apply -f -

echo "--- running k6 (loadtest/k6/smoke.js) against ${MCP_BASE_URL%/mcp} ---"
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

  jq -n \
    --argjson notes "$LOADTEST_NOTES" \
    --arg shared_state "$LOADTEST_SHARED_STATE" \
    --argjson kill_after "${LOADTEST_KILL_AFTER:-null}" \
    --arg killed_pod "$KILLED_POD" \
    --arg kill_timestamp "$KILL_TIMESTAMP" \
    --argjson generate_load_s "$(awk "BEGIN { print ${GENERATE_LOAD_END:-0} - ${GENERATE_LOAD_START:-0} }")" \
    --argjson k6_exit "$k6_exit" \
    --arg namespace "$LOADTEST_NAMESPACE" \
    '{
      notes: $notes,
      replicas: 3,
      shared_state: $shared_state,
      kill_after_seconds: $kill_after,
      forced_pod_deletion: (if $killed_pod == "" then null else {pod: $killed_pod, at: $kill_timestamp} end),
      step_seconds: {generate_and_load: $generate_load_s},
      namespace: $namespace,
      k6_exit: $k6_exit
    }' > "${LOADTEST_RESULTS_DIR}/timings.json"
fi

if [[ "$k6_exit" != "0" ]]; then
  echo "FAIL: k6 exited ${k6_exit}" >&2
  exit "$k6_exit"
fi

echo "loadtest-cluster OK: 3 api pods, generate+load and k6 Jobs completed"
