#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# kind E2E (#258, WP-30): creates a disposable kind cluster plus a throwaway
# local registry, builds and pushes the memory-manager + WP-22 mock-idp
# images and the Helm chart into it, installs the CNPG operator and Flux
# (pinned versions - docs/research/kind-e2e.md), applies tests/e2e/kind/ (a
# Kustomize overlay on deploy/flux/enterprise/) and asserts GET /readyz is
# 200 on exactly 3 api pods.
#
# Works with docker (GitHub runners) or podman (CLAUDE.md: this is the
# engine on a local dev node) - set CONTAINER_ENGINE to override detection.
# podman needs the rootful instance (`sudo podman`), not a user's rootless
# one: KIND_EXPERIMENTAL_PROVIDER=podman's node containers and the registry
# container below must share one engine-level network ("kind"), and only
# rootful podman and `sudo kind` end up looking at the same container
# storage/networks here. A kind node's own outbound/DNS traffic additionally
# needs `ufw`'s forward policy open on a host that runs one - see
# docs/research/kind-e2e.md's own "Local prerequisite" section; this script
# does not touch the host firewall itself.
#
# MM_E2E_IMAGE_SRC overrides where the memory-manager:e2e image is built
# from (default: this repo's own root). The worker Deployment this overlay
# installs runs `memory-manager worker`, which is not on main yet (WP-23,
# `wp/23-worker-embeddings`) - a local run of this script is expected to
# point MM_E2E_IMAGE_SRC at a throwaway worktree that already merges it, per
# this task's own contract; the mock-idp image always builds from this
# checkout (tests/mock_idp/ needs no such merge).
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

# --- pinned tool versions (docs/research/kind-e2e.md) ----------------------
KIND_VERSION="${KIND_VERSION:-v0.30.0}"
KIND_NODE_IMAGE="${KIND_NODE_IMAGE:-kindest/node:v1.33.4@sha256:25a6018e48dfcaee478f4a59af81157a437f15e6e140bf103f85a2e7cd0cbbf2}"
FLUX_VERSION="${FLUX_VERSION:-2.9.6}"
CNPG_OPERATOR_VERSION="${CNPG_OPERATOR_VERSION:-1.26.1}"

# --- run configuration ------------------------------------------------------
CLUSTER_NAME="${CLUSTER_NAME:-mm-e2e}"
NAMESPACE=memory-manager
CHART_VERSION=0.2.0
IMAGE_TAG=e2e
REGISTRY_NAME="${REGISTRY_NAME:-mm-e2e-registry}"
REGISTRY_HOST_PORT="${REGISTRY_HOST_PORT:-5001}"
MM_E2E_IMAGE_SRC="${MM_E2E_IMAGE_SRC:-$REPO_ROOT}"
# 900s, not 600s: the HelmRelease itself retries its own 5m install action
# up to 3 times on a timeout (helmrelease.yaml's own
# install.remediation.retries) - a slow first attempt (the 3-instance CNPG
# Cluster converging from a cold start, confirmed to take noticeably longer
# with networkPolicy.enabled: true than without it locally) can burn a full
# 5m before even starting its retry's own fresh CNPG bootstrap, so this
# loop's own budget has to outlast more than one such attempt, not just one.
STEP_TIMEOUT_SECONDS="${STEP_TIMEOUT_SECONDS:-900}"
KEEP_CLUSTER="${KEEP_CLUSTER:-0}"

TOOLS_DIR="$(mktemp -d)"
trap 'rm -rf "$TOOLS_DIR"' EXIT

# --- container engine (docker on GitHub runners, podman locally) -----------
if [[ -n "${CONTAINER_ENGINE:-}" ]]; then
  ENGINE="$CONTAINER_ENGINE"
elif command -v docker >/dev/null 2>&1; then
  ENGINE=docker
elif command -v podman >/dev/null 2>&1; then
  ENGINE=podman
else
  echo "FAIL: neither docker nor podman found on PATH" >&2
  exit 1
fi
echo "using container engine: ${ENGINE}"

if [[ "$ENGINE" == podman ]]; then
  # Rootful podman (see module docstring) - kind's own podman provider needs
  # it, and the registry container below must land in the same, rootful
  # container storage/network as the kind nodes it shares the "kind"
  # network with.
  CTR=(sudo podman)
  KIND=(sudo -E env "KIND_EXPERIMENTAL_PROVIDER=podman" kind)
else
  CTR=(docker)
  KIND=(kind)
fi

# --- pinned kind/flux CLIs, fetched fresh every run (validate.yml's own
# kubeconform/tofu pattern) - never whatever happens to be on PATH ----------
echo "installing kind ${KIND_VERSION} and flux ${FLUX_VERSION} into ${TOOLS_DIR}"
curl -sL "https://github.com/kubernetes-sigs/kind/releases/download/${KIND_VERSION}/kind-linux-amd64" \
  -o "${TOOLS_DIR}/kind"
chmod +x "${TOOLS_DIR}/kind"
curl -sL "https://github.com/fluxcd/flux2/releases/download/v${FLUX_VERSION}/flux_${FLUX_VERSION}_linux_amd64.tar.gz" \
  | tar xz -C "${TOOLS_DIR}" flux
chmod +x "${TOOLS_DIR}/flux"
export PATH="${TOOLS_DIR}:${PATH}"
# KIND above only names the binary "kind" - PATH is resolved per call, so
# exporting it here (before the first "${KIND[@]}" call below) is enough.

KCTL=(kubectl --context "kind-${CLUSTER_NAME}")

# --- failure diagnostics (CLAUDE.md: dump HelmRelease, pod logs, events) ---
dump_diagnostics() {
  echo "--- diagnostics: HelmRelease ---" >&2
  "${KCTL[@]}" get helmrelease -n "$NAMESPACE" -o yaml >&2 2>&1 || true
  echo "--- diagnostics: HelmChart ---" >&2
  "${KCTL[@]}" get helmchart -n flux-system -o yaml >&2 2>&1 || true
  echo "--- diagnostics: pods ---" >&2
  "${KCTL[@]}" get pods -n "$NAMESPACE" -o wide >&2 2>&1 || true
  echo "--- diagnostics: cluster (CNPG) ---" >&2
  "${KCTL[@]}" get cluster -n "$NAMESPACE" -o yaml >&2 2>&1 || true
  echo "--- diagnostics: pod logs (current + previous) ---" >&2
  for pod in $("${KCTL[@]}" get pods -n "$NAMESPACE" -o name 2>/dev/null); do
    echo "== ${pod} ==" >&2
    "${KCTL[@]}" logs -n "$NAMESPACE" "$pod" --all-containers >&2 2>&1 || true
    "${KCTL[@]}" logs -n "$NAMESPACE" "$pod" --all-containers --previous >&2 2>&1 || true
  done
  echo "--- diagnostics: events ---" >&2
  "${KCTL[@]}" get events -n "$NAMESPACE" --sort-by=.lastTimestamp >&2 2>&1 || true
  "${KCTL[@]}" get events -n flux-system --sort-by=.lastTimestamp >&2 2>&1 || true
}

cleanup() {
  local status=$?
  if [[ $status -ne 0 ]]; then
    echo "FAIL: e2e-kind.sh exiting with status ${status} - dumping diagnostics" >&2
    dump_diagnostics
  fi
  if [[ "$KEEP_CLUSTER" == "1" ]]; then
    echo "KEEP_CLUSTER=1 set - leaving cluster '${CLUSTER_NAME}' and registry '${REGISTRY_NAME}' running"
  else
    echo "cleaning up: kind cluster '${CLUSTER_NAME}', registry '${REGISTRY_NAME}'"
    "${KIND[@]}" delete cluster --name "$CLUSTER_NAME" >/dev/null 2>&1 || true
    "${CTR[@]}" rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
  fi
  rm -rf "$TOOLS_DIR"
  exit "$status"
}
trap cleanup EXIT

# --- 1. local registry ------------------------------------------------------
echo "--- creating local registry ---"
"${CTR[@]}" rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
"${CTR[@]}" run -d --restart=always -p "127.0.0.1:${REGISTRY_HOST_PORT}:5000" \
  --network bridge --name "$REGISTRY_NAME" docker.io/library/registry:3

# --- 2. kind cluster ---------------------------------------------------------
echo "--- creating kind cluster '${CLUSTER_NAME}' ---"
"${KIND[@]}" delete cluster --name "$CLUSTER_NAME" >/dev/null 2>&1 || true
KIND_CONFIG="${TOOLS_DIR}/kind-config.yaml"
cat >"$KIND_CONFIG" <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
containerdConfigPatches:
- |-
  [plugins."io.containerd.grpc.v1.cri".registry]
    config_path = "/etc/containerd/certs.d"
EOF
"${KIND[@]}" create cluster --name "$CLUSTER_NAME" --image "$KIND_NODE_IMAGE" --config "$KIND_CONFIG"
"${KCTL[@]}" wait --for=condition=Ready node --all --timeout=120s

# --- 3. wire the registry into the cluster's network -----------------------
# Both engines name the network a kind cluster's nodes land on "kind"
# (confirmed locally for podman; the same name docker's own kind provider
# has always used) - this connects the registry container onto it too.
KIND_NETWORK="kind"
"${CTR[@]}" network connect "$KIND_NETWORK" "$REGISTRY_NAME" 2>/dev/null || true
REGISTRY_IP="$("${CTR[@]}" inspect "$REGISTRY_NAME" --format "{{(index .NetworkSettings.Networks \"${KIND_NETWORK}\").IPAddress}}")"
if [[ -z "$REGISTRY_IP" ]]; then
  echo "FAIL: could not determine the registry container's IP on the '${KIND_NETWORK}' network" >&2
  exit 1
fi
echo "registry '${REGISTRY_NAME}' reachable in-cluster at ${REGISTRY_IP}:5000, from the host at localhost:${REGISTRY_HOST_PORT}"

# containerd on each node aliases "localhost:${REGISTRY_HOST_PORT}" (the
# image refs built/pushed below) to the registry's raw IP - a pod's own
# network stack (Flux's source-controller) cannot resolve either the
# registry's engine-level name or "localhost:<port>" the way node-level
# containerd can (docs/research/kind-e2e.md), which is why the
# HelmRepository below is pointed at this same IP directly instead.
for node in $("${KIND[@]}" get nodes --name "$CLUSTER_NAME"); do
  "${CTR[@]}" exec "$node" mkdir -p "/etc/containerd/certs.d/localhost:${REGISTRY_HOST_PORT}"
  printf '[host."http://%s:5000"]\n' "$REGISTRY_IP" \
    | "${CTR[@]}" exec -i "$node" cp /dev/stdin "/etc/containerd/certs.d/localhost:${REGISTRY_HOST_PORT}/hosts.toml"
done

# --- 4. build + push the memory-manager and mock-idp images ----------------
# podman needs --tls-verify=false against the plain-HTTP local registry;
# docker treats "localhost:<port>" as insecure automatically (both verified
# locally/against validate.yml's own docker job), so the flag is podman-only.
push_image() {
  if [[ "$ENGINE" == podman ]]; then
    "${CTR[@]}" push --tls-verify=false "$1"
  else
    "${CTR[@]}" push "$1"
  fi
}

echo "--- building memory-manager:${IMAGE_TAG} from ${MM_E2E_IMAGE_SRC} ---"
MM_GIT_SHA="$(git -C "$MM_E2E_IMAGE_SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)"
"${CTR[@]}" build -t "localhost:${REGISTRY_HOST_PORT}/memory-manager:${IMAGE_TAG}" \
  --build-arg "MM_GIT_SHA=${MM_GIT_SHA}" \
  -f "${MM_E2E_IMAGE_SRC}/Dockerfile" "$MM_E2E_IMAGE_SRC"
push_image "localhost:${REGISTRY_HOST_PORT}/memory-manager:${IMAGE_TAG}"

# tests/mock_idp/Containerfile needs the root .dockerignore's own excluded
# "tests" directory (its own top comment: podman's --ignorefile swaps in
# tests/mock_idp/.containerignore instead, but plain `docker build` has no
# such per-build override) - built from a throwaway context assembled here
# instead of the repo root, with exactly the paths that Containerfile's own
# COPY instructions need, so the build works the same way under both
# engines without a second ignorefile living next to it in tests/mock_idp/.
echo "--- building mock-idp:${IMAGE_TAG} ---"
MOCK_IDP_CTX="${TOOLS_DIR}/mock-idp-ctx"
mkdir -p "${MOCK_IDP_CTX}/tests"
cp "${REPO_ROOT}/pyproject.toml" "${REPO_ROOT}/uv.lock" "${REPO_ROOT}/README.md" "$MOCK_IDP_CTX/"
cp -r "${REPO_ROOT}/src" "${MOCK_IDP_CTX}/src"
cp -r "${REPO_ROOT}/tests/mock_idp" "${MOCK_IDP_CTX}/tests/mock_idp"
"${CTR[@]}" build -t "localhost:${REGISTRY_HOST_PORT}/mock-idp:${IMAGE_TAG}" \
  -f "${REPO_ROOT}/tests/mock_idp/Containerfile" "$MOCK_IDP_CTX"
push_image "localhost:${REGISTRY_HOST_PORT}/mock-idp:${IMAGE_TAG}"

# --- 5. package + push the Helm chart as an OCI artifact --------------------
echo "--- packaging and pushing the Helm chart ---"
helm package "${REPO_ROOT}/charts/memory-manager" --version "$CHART_VERSION" --app-version "$CHART_VERSION" -d "$TOOLS_DIR"
helm push "${TOOLS_DIR}/memory-manager-${CHART_VERSION}.tgz" "oci://localhost:${REGISTRY_HOST_PORT}/charts" --plain-http

# --- 6. CNPG operator (pinned release manifest) -----------------------------
echo "--- installing the CNPG operator ${CNPG_OPERATOR_VERSION} ---"
"${KCTL[@]}" apply --server-side -f \
  "https://github.com/cloudnative-pg/cloudnative-pg/releases/download/v${CNPG_OPERATOR_VERSION}/cnpg-${CNPG_OPERATOR_VERSION}.yaml"
"${KCTL[@]}" wait --for=condition=Available deployment/cnpg-controller-manager -n cnpg-system --timeout=180s

# --- 7. Flux (pinned version, only the controllers this E2E needs) ---------
echo "--- installing Flux ${FLUX_VERSION} (source-controller, helm-controller) ---"
flux install --context "kind-${CLUSTER_NAME}" --version "v${FLUX_VERSION}" \
  --components source-controller,helm-controller
"${KCTL[@]}" wait --for=condition=Available deployment --all -n flux-system --timeout=180s

# --- 8. apply the kind E2E overlay ------------------------------------------
echo "--- applying tests/e2e/kind (local registry at ${REGISTRY_IP}:5000) ---"
"${KCTL[@]}" kustomize "${REPO_ROOT}/tests/e2e/kind" \
  | sed "s|__LOCAL_REGISTRY__|${REGISTRY_IP}:5000|g" \
  | "${KCTL[@]}" apply -f -

# --- 9. wait for the CNPG cluster and the HelmRelease -----------------------
echo "--- waiting for the CNPG cluster to reach 3 ready instances ---"
cnpg_deadline=$((SECONDS + STEP_TIMEOUT_SECONDS))
while true; do
  ready="$("${KCTL[@]}" get cluster -n "$NAMESPACE" -o jsonpath='{.items[0].status.readyInstances}' 2>/dev/null || echo 0)"
  [[ "$ready" == "3" ]] && break
  if (( SECONDS > cnpg_deadline )); then
    echo "FAIL: CNPG cluster never reached 3 ready instances (last seen: ${ready:-<none>})" >&2
    exit 1
  fi
  sleep 5
done
echo "OK: CNPG cluster has 3 ready instances"

echo "--- waiting for the HelmRelease to become Ready ---"
if ! "${KCTL[@]}" wait --for=condition=Ready "helmrelease/memory-manager" -n "$NAMESPACE" \
    --timeout="${STEP_TIMEOUT_SECONDS}s"; then
  echo "FAIL: HelmRelease/memory-manager never became Ready" >&2
  exit 1
fi
echo "OK: HelmRelease/memory-manager is Ready"

# --- 10. assert /readyz is 200 on exactly 3 api pods ------------------------
echo "--- waiting for 3 api pods to report /readyz 200 ---"
readyz_deadline=$((SECONDS + STEP_TIMEOUT_SECONDS))
while true; do
  mapfile -t api_pods < <("${KCTL[@]}" get pods -n "$NAMESPACE" \
    -l "app.kubernetes.io/instance=memory-manager,app.kubernetes.io/component=api" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
  if [[ "${#api_pods[@]}" -eq 3 ]]; then
    all_ready=1
    for pod in "${api_pods[@]}"; do
      body_file="${TOOLS_DIR}/readyz-${pod}.json"
      err_file="${TOOLS_DIR}/readyz-${pod}.err"
      # `kubectl get --raw` surfaces a non-2xx proxied response as a non-zero
      # exit (client-go treats the pod-proxy subresource's own status code as
      # its own) - _readyz (src/memory_manager/http.py) only ever answers 200
      # with `"ready": true` in the body, 503 otherwise, so "exit 0 and ready
      # true" below is exactly "GET /readyz -> 200".
      if "${KCTL[@]}" get --raw "/api/v1/namespaces/${NAMESPACE}/pods/${pod}:8080/proxy/readyz" \
          >"$body_file" 2>"$err_file" && grep -q '"ready": *true' "$body_file"; then
        echo "OK: ${pod} GET /readyz -> 200 $(cat "$body_file")"
      else
        echo "not yet ready: ${pod} GET /readyz -> $(cat "$body_file" "$err_file" 2>/dev/null)"
        all_ready=0
      fi
    done
    if [[ "$all_ready" == "1" ]]; then
      echo "OK: GET /readyz returned 200 (ready: true) for exactly 3 api pods"
      break
    fi
  else
    echo "waiting for 3 api pods (currently ${#api_pods[@]})"
  fi
  if (( SECONDS > readyz_deadline )); then
    echo "FAIL: did not see /readyz 200 for exactly 3 api pods within ${STEP_TIMEOUT_SECONDS}s" >&2
    exit 1
  fi
  sleep 5
done

echo "E2E OK: 3 api pods, /readyz 200 on all three"
