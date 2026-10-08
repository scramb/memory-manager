# Enterprise Flux example (`deploy/flux/enterprise/`)

Installs `charts/memory-manager` (#45) with the chart's own enterprise profile
(`charts/memory-manager/values-enterprise.yaml`, WP-29) instead of `../`'s single-replica
defaults: `storage` → `backend` `postgres`, Entra login (ADR-0006), 3 `api` replicas behind a
`PodDisruptionBudget` and 2 `worker` replicas (ADR-0009 §4/§5), a CPU `HorizontalPodAutoscaler`
for both (#251), a 3-instance CNPG `Cluster` with Barman Cloud plugin backups (#252), and
per-component `NetworkPolicy` objects (#255). Same operator-agnostic rule as `../README.md`
(CLAUDE.md: a public repository carries no real hostnames, cluster names, secret-store paths,
IPs or namespaces) - every value an operator must supply is a placeholder, checked by
`scripts/check-deploy-placeholders.sh`.

## What is here

| File | What it is |
|---|---|
| `namespace.yaml`, `helmrepository.yaml` | identical to `../namespace.yaml`/`../helmrepository.yaml`, duplicated rather than referenced by relative path - `kustomize build`'s own root-only load restriction (the default `kubectl kustomize` runs with) refuses a `resources:` entry outside this directory |
| `helmrelease.yaml` | the `HelmRelease` that installs the chart's enterprise profile - its own `values` mirror `values-enterprise.yaml`'s settings plus the operator-specific bits that file leaves to a further overlay |
| `externalsecret.yaml` | two `ExternalSecret` objects: the app secrets (`memory-manager-secrets`, `helmrelease.yaml`'s own `secrets` → `existingSecret`) and the CNPG Barman Cloud backup credentials (`memory-manager-backup-credentials`, `helmrelease.yaml`'s own `database` → `cnpg` → `backup` → `existingSecret`) |
| `kustomization.yaml` | ties the four resources above together under the `memory-manager` namespace |

The SOPS alternative (`../README.md`'s own "Secrets" section) applies here identically - this
directory does not repeat the illustrative `sops-secret.example.yaml`/`.sops.yaml` pair.

## Prerequisites (not installed by this example)

| Component | Tested version | Why |
|---|---|---|
| CloudNativePG operator | **>= 1.26** (`docs/research/cnpg-backups.md`) | Older versions do not implement the CNPG-I plugin protocol the Barman Cloud plugin needs |
| Barman Cloud CNPG-I plugin | **version 0.15.1** (`docs/research/cnpg-backups.md`) | Object-store backups (`ObjectStore`/`ScheduledBackup`, #252) - install in the same namespace as the CNPG operator |
| cert-manager | any current release (`docs/research/cnpg-backups.md`) | The Barman Cloud plugin's own installation manifest provisions a self-signed `Issuer` and client/server `Certificate`s through it |
| KEDA | **version 2.17** (`docs/research/kubernetes-scaling.md`) | Optional - only if you turn `api` → `keda` → `enabled` on and `api` → `autoscaling` → `enabled` off in your own overlay instead of the CPU `HorizontalPodAutoscaler` this example ships |
| Prometheus Operator | any current release shipping `monitoring.coreos.com` CRDs | Optional - only if you turn `serviceMonitor` → `enabled` on in your own overlay; the KEDA Prometheus trigger above also needs a Prometheus to query |

None of the above is installed by this repository's CI or by `kubectl apply`/Flux reconciling
this directory - an operator step, same as `../README.md`'s own Barman Cloud plugin note.

## Required secrets

Narrower than `../README.md`'s own table: `storage` → `backend` `postgres` never clones the
vault (no `SSH_PRIVATE_KEY`/`SSH_KNOWN_HOSTS`) and `login` → `mode` `entra` never reads the
`oidc` sub-block (no `OIDC_CLIENT_SECRET`), `charts/memory-manager/templates/api-deployment.yaml`.

| Secret key | Purpose | How to generate |
|---|---|---|
| `VAULT_WEBHOOK_SECRET` | verifies the GitHub/Gitea push webhook's HMAC signature | `openssl rand -hex 32` |
| `OAUTH_CLIENT_SECRET_KEY` | encrypts each DCR client's `client_secret` at rest (ADR-0004) | the one-liner in `src/memory_manager/auth/store.py`'s own `ValueError` message (generates a Fernet key) |
| `ENTRA_CLIENT_SECRET` | the Entra app registration's client secret (ADR-0006) | `deploy/entra`'s own OpenTofu module (`client_secret` output, #256), or issued by Entra when the app is registered manually |
| `ACCESS_KEY_ID`, `ACCESS_SECRET_KEY` | the S3-compatible object store credentials for CNPG Barman Cloud backups | issued by your own object-store provider |

## Applying this

Same pattern as `../README.md`'s own "Applying this" example, with `path: ./deploy/flux/enterprise`
instead and one more `ExternalSecret` patch target for the backup credentials:

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: memory-manager
spec:
  interval: 5m
  sourceRef:
    kind: GitRepository
    name: memory-manager
  path: ./deploy/flux/enterprise
  prune: true
  patches:
    - target:
        kind: HelmRelease
        name: memory-manager
      patch: |
        - op: replace
          path: /spec/values/publicUrl
          value: https://memory.example.com
        - op: replace
          path: /spec/values/httpRoute/hostnames/0
          value: memory.example.com
        - op: replace
          path: /spec/values/httpRoute/parentRefs/0/name
          value: my-real-gateway
        - op: replace
          path: /spec/values/login/entra/tenantId
          value: 11111111-1111-1111-1111-111111111111
        - op: replace
          path: /spec/values/login/entra/clientId
          value: 22222222-2222-2222-2222-222222222222
        - op: replace
          path: /spec/values/database/cnpg/backup/destinationPath
          value: s3://my-real-bucket/
        - op: replace
          path: /spec/values/database/cnpg/backup/endpointURL
          value: https://s3.my-real-provider.example.com
    - target:
        kind: ExternalSecret
        name: memory-manager-secrets
      patch: |
        - op: replace
          path: /spec/secretStoreRef/name
          value: my-real-secret-store
    - target:
        kind: ExternalSecret
        name: memory-manager-backup-credentials
      patch: |
        - op: replace
          path: /spec/secretStoreRef/name
          value: my-real-secret-store
```

## Validate before applying

```sh
kubectl kustomize deploy/flux/enterprise | kubeconform -strict -summary \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
```

Validates the four manifests here against the real Flux/External Secrets CRD schemas; it does
not render the Helm chart itself, nor install the prerequisites above. `tests/chart/
test_flux_enterprise.py` covers the first gap (`helmrelease.yaml`'s own values block rendered
against the local chart, `MM_REQUIRE_HELM=1 uv run pytest tests/chart/test_flux_enterprise.py`);
the second is the kind E2E (#258, not yet in this repository).

## Not included

- Installing the CNPG operator, the Barman Cloud plugin, cert-manager, KEDA or the Prometheus
  Operator (see "Prerequisites" above).
- The kind E2E that rolls this example out against a real cluster and checks `/readyz` across
  three `api` replicas (#258).
- An operator guide walking through the enterprise profile end to end - [`docs/guides/enterprise-operations.md`](../../../docs/guides/enterprise-operations.md) (#259).
- Cloudflare Tunnel as an `HTTPRoute`/`Ingress` alternative - [`docs/guides/cloudflare-tunnel.md`](../../../docs/guides/cloudflare-tunnel.md) (`../README.md`'s own "Not included").
