# memory-manager (Helm chart)

Helm packaging of the same server `deploy/` ships as a generic Kustomize base (`deploy/README.md`):
the `Deployment`/`Service`, a `HTTPRoute`/`Ingress`, an optional CloudNativePG `Cluster` and an
optional NetworkPolicy, restricted Pod Security by default, single writer while `storage` → `backend`
is `git` (`replicaCount` 0 or 1 - `postgres` lifts the maximum, ADR-0007/ADR-0009 §6). For operators
who prefer Helm over Flux+Kustomize. Every tagged release publishes this
chart as a signed OCI artifact (`docs/releasing.md`):

```sh
helm install memory-manager oci://ghcr.io/scramb/charts/memory-manager --version X.Y.Z -f my-values.yaml
```

or, straight from a checkout:

```sh
helm install memory-manager ./charts/memory-manager -f my-values.yaml
```

Deliberately **operator-agnostic** (`CLAUDE.md`: a public repository carries no real hostnames,
cluster names, secret-store paths, IPs or namespaces) - every value an operator must supply is a
clearly-named placeholder in `values.yaml` instead, exactly like `deploy/`'s own placeholder table.
`scripts/check-deploy-placeholders.sh` (run in CI) fails the build if a real one ever creeps into
this chart instead.

## Required secrets

Either set `secrets` → `existingSecret` to a Secret created out-of-band (External Secrets Operator,
SOPS, `kubectl create secret generic`, ...) carrying the keys below, or - for a quick, non-production
install only - set `secrets` → `create: true` and `secrets` → `values` directly (ends up in `helm
get values`/the release history in plaintext, see `templates/secret.yaml`'s own comment).

| Secret key | Purpose | How to generate |
|---|---|---|
| `SSH_PRIVATE_KEY` | the vault's deploy key | `ssh-keygen -t ed25519 -f deploy-key -N ''` - **not** read-only on the remote: the server pushes every write back |
| `SSH_KNOWN_HOSTS` | pins the vault remote's host key | `ssh-keyscan -t ed25519 git.example.com` (the real git host), reviewed against the host's own published fingerprint before trusting it |
| `VAULT_WEBHOOK_SECRET` | verifies the GitHub/Gitea push webhook's HMAC signature | `openssl rand -hex 32` |
| `OIDC_CLIENT_SECRET` | the upstream OIDC provider's client secret, when `login` → `mode` is `oidc` | issued by the provider when the client is registered there |
| `OAUTH_CLIENT_SECRET_KEY` | encrypts each DCR client's `client_secret` at rest (ADR-0004) | the one-liner in `src/memory_manager/auth/store.py`'s own `ValueError` message (generates a Fernet key) |
| `ADMIN_PASSWORD_HASH` | the quickstart login alternative, when `login` → `mode` is `password` | `memory-manager hash-password` (reads the password from stdin) |

## Values

| Key | Default | Description |
|---|---|---|
| `storage` → `backend` | `git` | `git` (default, ADR-0007) or `postgres` (enterprise) - only `postgres` may run more than one replica or an autoscaler (ADR-0009 §6), enforced by `values.schema.json` and the chart's own `validate` helper |
| `replicaCount` | `1` | 0 or 1 while `storage` → `backend` is `git` (`values.schema.json`) - single writer to the vault's git remote and per-process OAuth login rate limiter, never a scaled service; `postgres` lifts the maximum |
| `image` → `repository` | `ghcr.io/scramb/memory-manager` | |
| `image` → `tag` | `""` | Defaults to the chart's own `appVersion`; never `latest` |
| `image` → `pullPolicy` | `IfNotPresent` | |
| `serviceAccount` → `create` | `false` | The pod needs no Kubernetes identity by default |
| `podSecurityContext`, `securityContext` | restricted PSS | Matches `deploy/deployment.yaml` exactly |
| `service` → `port` | `8080` | |
| `resources` | 50m/128Mi request, 512Mi limit | |
| `probes` → `liveness`/`readiness` | see `values.yaml` | `/healthz`, `/readyz` |
| `publicUrl` | `https://memory.example.com` | Must equal the `HTTPRoute`/`Ingress` hostname exactly (ADR-0004) |
| `vault` → `remote`/`branch` | placeholder / `main` | The vault's own git remote |
| `login` → `mode` | `oidc` | `oidc` or `password` (quickstart alternative) |
| `login` → `oidc` → `issuer`/`clientId`/`allowedEmails` | placeholders | Deny-by-default allowlist (ADR-0004 addendum) |
| `login` → `namespaces` | `personal` | Fallback for a subject with no namespace mapping |
| `embedding` → `provider` | `none` | `none`, `ollama` or `openai` - every embedding API is optional |
| `metrics` → `enabled` | `true` | `/metrics`; restrict with `networkPolicy` or a `serviceMonitor`-only scrape path |
| `logFormat` | `json` | |
| `secrets` → `existingSecret` | `""` | Defaults to `<release>-memory-manager-secrets` |
| `secrets` → `create` | `false` | Chart-managed Secret from `secrets` → `values` - not for production |
| `database` → `cnpg` → `enabled` | `true` | Renders a CloudNativePG `Cluster`; needs the CNPG operator installed already |
| `database` → `url`/`existingSecret` | `""` | Used instead, when the `cnpg` block above is disabled |
| `httpRoute` → `enabled` | `true` | Gateway API `HTTPRoute`, same shape as `deploy/httproute.yaml` |
| `ingress` → `enabled` | `false` | Classic `Ingress`, for clusters without Gateway API |
| `networkPolicy` → `enabled` | `false` | Off by default, documented; ingress scoped to configurable selectors, egress allow-all-with-DNS by default (remote IPs are operator-specific and unknown to this chart) |
| `serviceMonitor` → `enabled` | `false` | Needs the Prometheus Operator CRDs installed |

See `values.yaml` itself for the full, commented reference - this table is the summary.

## Validate before installing

```sh
helm lint charts/memory-manager
helm template memory-manager charts/memory-manager -f my-values.yaml | kubeconform -strict -summary \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
```

## Not included

- A Flux `HelmRelease` example with SOPS secrets - #46 (`deploy/flux/`).
- Backups for the CNPG `Cluster` - it is a derived index (`memory-manager reindex --full` rebuilds
  it from the vault), not a primary store; add a CNPG `Backup`/`ScheduledBackup` and object-store
  configuration in your own values/overlay if you want point-in-time recovery anyway.
