# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Shared kind/local-registry/CNPG-operator bootstrap (#270, WP-32),
# factored out of scripts/e2e-kind.sh so scripts/loadtest-cluster.sh's own
# local-kind path stays the exact same mechanics instead of a second,
# drifting copy - both scripts source this file and keep their own
# cluster-specific bits (what gets installed into the cluster afterwards)
# to themselves.
#
# Sourced, never executed directly - every function below is a thin
# wrapper around a handful of `kind`/`podman`/`kubectl` calls and reads or
# writes plain globals in the *caller's* shell (ENGINE, CTR, KIND,
# REGISTRY_IP), the same pattern scripts/e2e-kind.sh used inline before
# this file existed. Callers are expected to already have `set -euo
# pipefail` active and their own cleanup trap for the resources these
# functions create.

# Sets ENGINE ("docker" or "podman") and the CTR/KIND argv arrays every
# other function below calls through - CONTAINER_ENGINE overrides
# detection, same as scripts/e2e-kind.sh's own original logic. podman
# needs the rootful instance (`sudo podman`) and `sudo -E kind` with
# KIND_EXPERIMENTAL_PROVIDER=podman - see scripts/e2e-kind.sh's own module
# docstring for why both have to land in the same, rootful container
# storage/network.
mm_detect_container_engine() {
  if [[ -n "${CONTAINER_ENGINE:-}" ]]; then
    ENGINE="$CONTAINER_ENGINE"
  elif command -v docker >/dev/null 2>&1; then
    ENGINE=docker
  elif command -v podman >/dev/null 2>&1; then
    ENGINE=podman
  else
    echo "FAIL: neither docker nor podman found on PATH" >&2
    return 1
  fi
  echo "using container engine: ${ENGINE}"

  if [[ "$ENGINE" == podman ]]; then
    CTR=(sudo podman)
    KIND=(sudo -E env "KIND_EXPERIMENTAL_PROVIDER=podman" kind)
  else
    CTR=(docker)
    KIND=(kind)
  fi
}

# Downloads the pinned `kind` CLI into "$2" (a directory already on, or
# about to be put on, PATH) - fetched fresh every run, never whatever
# happens to be on PATH already (same reasoning as validate.yml's own
# kubeconform/tofu pattern).
mm_install_pinned_kind() {
  local kind_version="$1" tools_dir="$2"
  echo "installing kind ${kind_version} into ${tools_dir}"
  curl -sL "https://github.com/kubernetes-sigs/kind/releases/download/${kind_version}/kind-linux-amd64" \
    -o "${tools_dir}/kind"
  chmod +x "${tools_dir}/kind"
}

# (Re-)creates a throwaway local registry container, plain HTTP, exposed
# on 127.0.0.1 only.
mm_start_local_registry() {
  local registry_name="$1" registry_host_port="$2"
  echo "--- creating local registry '${registry_name}' ---"
  "${CTR[@]}" rm -f "$registry_name" >/dev/null 2>&1 || true
  "${CTR[@]}" run -d --restart=always -p "127.0.0.1:${registry_host_port}:5000" \
    --network bridge --name "$registry_name" docker.io/library/registry:3
}

# (Re-)creates a kind cluster with the containerd `certs.d` config patch
# every pushed-image reference below needs, and waits for every node to
# report Ready.
mm_create_kind_cluster() {
  local cluster_name="$1" node_image="$2" tools_dir="$3"
  echo "--- creating kind cluster '${cluster_name}' ---"
  "${KIND[@]}" delete cluster --name "$cluster_name" >/dev/null 2>&1 || true
  local kind_config="${tools_dir}/kind-config.yaml"
  cat >"$kind_config" <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
containerdConfigPatches:
- |-
  [plugins."io.containerd.grpc.v1.cri".registry]
    config_path = "/etc/containerd/certs.d"
EOF
  "${KIND[@]}" create cluster --name "$cluster_name" --image "$node_image" --config "$kind_config"
  kubectl --context "kind-${cluster_name}" wait --for=condition=Ready node --all --timeout=120s
}

# Connects the registry container onto the kind cluster's own container
# network and points every node's containerd at it under
# "localhost:$registry_host_port" (the image refs built/pushed below) -
# sets REGISTRY_IP (the caller's global) to the registry's address on
# that network, which a HelmRepository/chart install points at directly
# (a pod cannot resolve either the registry's engine-level name or
# "localhost:<port>" itself - only node-level containerd can).
mm_wire_registry_into_kind() {
  local cluster_name="$1" registry_name="$2" registry_host_port="$3"
  local kind_network="kind"
  "${CTR[@]}" network connect "$kind_network" "$registry_name" 2>/dev/null || true
  REGISTRY_IP="$("${CTR[@]}" inspect "$registry_name" --format "{{(index .NetworkSettings.Networks \"${kind_network}\").IPAddress}}")"
  if [[ -z "$REGISTRY_IP" ]]; then
    echo "FAIL: could not determine the registry container's IP on the '${kind_network}' network" >&2
    return 1
  fi
  echo "registry '${registry_name}' reachable in-cluster at ${REGISTRY_IP}:5000, from the host at localhost:${registry_host_port}"

  for node in $("${KIND[@]}" get nodes --name "$cluster_name"); do
    "${CTR[@]}" exec "$node" mkdir -p "/etc/containerd/certs.d/localhost:${registry_host_port}"
    printf '[host."http://%s:5000"]\n' "$REGISTRY_IP" \
      | "${CTR[@]}" exec -i "$node" cp /dev/stdin "/etc/containerd/certs.d/localhost:${registry_host_port}/hosts.toml"
  done
}

# Pushes an already-built "localhost:<port>/..." image ref - podman needs
# --tls-verify=false against the plain-HTTP local registry; docker treats
# "localhost:<port>" as insecure automatically (both verified locally/
# against validate.yml's own docker job).
mm_push_image() {
  local image_ref="$1"
  if [[ "$ENGINE" == podman ]]; then
    "${CTR[@]}" push --tls-verify=false "$image_ref"
  else
    "${CTR[@]}" push "$image_ref"
  fi
}

# Installs the pinned CloudNativePG operator release and waits for its
# controller manager to become Available.
mm_install_cnpg_operator() {
  local cnpg_operator_version="$1" kube_context="$2"
  echo "--- installing the CNPG operator ${cnpg_operator_version} ---"
  kubectl --context "$kube_context" apply --server-side -f \
    "https://github.com/cloudnative-pg/cloudnative-pg/releases/download/v${cnpg_operator_version}/cnpg-${cnpg_operator_version}.yaml"
  kubectl --context "$kube_context" wait --for=condition=Available deployment/cnpg-controller-manager \
    -n cnpg-system --timeout=180s
}
