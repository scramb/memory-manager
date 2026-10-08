# kind E2E: pinned tool versions and local registry wiring (#258)

Retrieved: 2026-10-08 · Feeds: WP-30, `scripts/e2e-kind.sh`, `.github/workflows/e2e-kind.yml`,
`tests/e2e/kind/`

## Pinned versions

| Tool | Version | Source |
|---|---|---|
| kind | `v0.30.0`, node image `kindest/node:v1.33.4@sha256:25a6018e48dfcaee478f4a59af81157a437f15e6e140bf103f85a2e7cd0cbbf2` | [kind v0.30.0 release notes](https://github.com/kubernetes-sigs/kind/releases/tag/v0.30.0) - lists the pre-built node images per Kubernetes minor; `v1.33.4` chosen as the most recent patch of a non-latest minor (not the release's own new default `v1.34.0`), for a node image with a longer track record at retrieval time |
| Flux CLI/controllers | `v2.9.6` | [flux2 v2.9.6 release](https://github.com/fluxcd/flux2/releases/tag/v2.9.6) |
| CloudNativePG operator | `v1.26.1` | [cloudnative-pg v1.26.1 release](https://github.com/cloudnative-pg/cloudnative-pg/releases/tag/v1.26.1), manifest asset `cnpg-1.26.1.yaml` |

`scripts/e2e-kind.sh` downloads its own pinned `kind`/`flux` binaries on every run (the same
pattern `.github/workflows/validate.yml`'s `deploy` job already uses for `kubeconform`/`tofu`) -
whatever happens to be on `PATH` is never trusted. `kubectl`/`helm` are assumed already installed
(both ship on GitHub's `ubuntu-latest` runner image; CLAUDE.md does not ask this script to pin
every transitive tool, only "every tool version" this E2E itself chooses - `kind`, Flux and the
CNPG operator are the three the issue names).

## Local registry: images vs. the Flux OCI `HelmRepository`

Both the `memory-manager`/mock-idp container images and the packaged Helm chart go through one
throwaway registry (`distribution/distribution`, i.e. `registry:3`) so the run exercises the PR's
own build, not a published release - but the two consumers resolve it two different ways:

- **containerd on each kind node** (pulling `localhost:5001/memory-manager:e2e` etc.) - the
  node is itself a container on the engine-level network kind creates (`kind`, confirmed by
  `podman network ls`/`docker network ls` once a cluster exists), so a per-node
  `/etc/containerd/certs.d/localhost:5001/hosts.toml` aliasing `localhost:5001` to
  `http://<registry-container-name-or-ip>:5000` lets the node's own containerd reach it - the
  [kind local registry doc](https://kind.sigs.k8s.io/docs/user/local-registry/)'s own pattern,
  confirmed working here with `podman`'s `KIND_EXPERIMENTAL_PROVIDER` (`v0.30.0` ships "Fix HA
  control-plane loadbalancer for podman", i.e. active upstream support for that provider).
- **Flux's `source-controller`** (pulling the chart from the `HelmRepository`'s own `spec.url`)
  runs as a *pod*, not as node-level containerd - a pod's own network stack resolves hostnames
  through cluster CoreDNS, which has no record for the registry container's engine-level name or
  alias at all (confirmed by a direct test: `kind-registry` times out from inside a pod/node
  shell, even though the same name resolves fine from the host). The `HelmRepository`'s `url`
  therefore points at the registry container's **raw IP** on the `kind` network instead (no DNS
  involved), with `spec.insecure: true` - [documented field](https://github.com/fluxcd/source-controller/blob/main/docs/spec/v1/helmrepositories.md#insecure)
  ("supported only for Helm OCI repositories"), confirmed end-to-end here: a `HelmRelease`
  against a `test-repo` `HelmRepository` built this way successfully resolved and templated the
  chart. `scripts/e2e-kind.sh` resolves that IP once the registry container exists and substitutes
  it into `tests/e2e/kind/`'s own `__LOCAL_REGISTRY__` placeholder before applying the overlay -
  the committed overlay itself never carries a real IP (CLAUDE.md: no operator-specific values;
  here the equivalent is "no run-specific values" since this is test fixture, not a deployment
  example).

## kindnet enforces NetworkPolicy

Confirmed directly (a non-allow-listed pod's connection to a ClusterIP backed by a policy-selected
pod times out): kind's default CNI, kindnet, does enforce `NetworkPolicy` - this E2E therefore
runs with `networkPolicy.enabled` left at the enterprise profile's own default (`true`), not
turned off, so it is a real test of `charts/memory-manager/templates/networkpolicy.yaml`, not just
of the rollout with policies out of the way. That enforcement is exactly what first caught the
CNPG peer-selector gap fixed as #255 (`templates/networkpolicy.yaml`'s own comment and
`tests/chart/test_networkpolicies.py::test_postgres_backend_cnpg_policy_peers_match_cluster_label_only`):
a joining replica's own `pg_basebackup` step runs as a Job, never labelled
`cnpg.io/podRole: instance`, so a peer selector that required that label let a *running* replica
reach the primary but rejected every *joining* one - the `Cluster` never reached 3 ready instances
with policies on before that fix.

## Local prerequisite: outbound/forwarded traffic from the podman bridge

Confirmed on this node (Debian 13, `ufw` active): with `ufw`'s default `DEFAULT_FORWARD_POLICY`
("DROP"), neither a kind node's outbound internet access (pulling `ghcr.io/fluxcd/*`, the CNPG
operator image, etc.) nor its DNS queries to podman's own `aardvark-dns` (bound on the bridge
gateway IP, a host-local service reached through the `INPUT` chain, not `FORWARD`) worked at all -
every such packet showed up in `ufw`'s own forward/input-block log counters. Fixed once, at the
host level (not by this script, which must stay portable to any CI runner or operator machine):
`DEFAULT_FORWARD_POLICY="ACCEPT"` in `/etc/default/ufw` plus `ufw allow in on podman+` (then
`ufw reload`). A host without `ufw` (GitHub's `ubuntu-latest` runners: no `ufw`) or without this
restrictive a default needs no such change. This is a one-time local-machine prerequisite, not
part of `scripts/e2e-kind.sh` itself.
