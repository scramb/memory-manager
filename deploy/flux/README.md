# Flux example (`deploy/flux/`)

A generic Flux deployment of memory-manager via the Helm chart (`charts/memory-manager`, #45),
not the plain Kustomize base (`deploy/`'s own `README.md` covers that one). Pick whichever matches
how the rest of an operator's cluster is managed - both install the same server.

Like `deploy/`, this is deliberately **operator-agnostic** (`CLAUDE.md`: a public repository
carries no real hostnames, cluster names, secret-store paths, IPs or namespaces) - every value an
operator must supply is a placeholder, same table as `charts/memory-manager/README.md`'s own
"Required secrets"/"Values". `scripts/check-deploy-placeholders.sh` (run in CI) fails the build if
a real one ever creeps into this directory instead.

## What is here

| File | What it is |
|---|---|
| `namespace.yaml` | the `memory-manager` namespace everything else renders into (`kustomization.yaml`'s own `namespace:` transformer adds it to the namespaced resources below; `Namespace` itself is cluster-scoped and stays untouched) |
| `helmrepository.yaml` | an OCI `HelmRepository` pointing at `oci://ghcr.io/scramb/charts` - published by `.github/workflows/release.yml` on every tag (`docs/releasing.md`); anonymous pull, no `secretRef` needed for a public package |
| `helmrelease.yaml` | the `HelmRelease` that installs `charts/memory-manager` from that source, with the same values `charts/memory-manager/README.md`'s own "Values" table documents |
| `externalsecret.yaml` | pulls the secrets `helmrelease.yaml`'s own `secrets` → `existingSecret` value points at from an operator's `ClusterSecretStore` - the primary secrets path (#46) |
| `sops-secret.example.yaml`, `.sops.yaml` | the SOPS alternative, see below - **illustrative only**, not wired into `kustomization.yaml` |
| `kustomization.yaml` | ties the four resources above together under the `memory-manager` namespace |

## Required secrets

Same keys, same generation commands as `charts/memory-manager/README.md`'s own "Required secrets"
table (`SSH_PRIVATE_KEY`, `SSH_KNOWN_HOSTS`, `VAULT_WEBHOOK_SECRET`, `OIDC_CLIENT_SECRET`,
`OAUTH_CLIENT_SECRET_KEY`, and `ADMIN_PASSWORD_HASH` for `login` → `mode` set to `password`
instead of the `oidc` default `helmrelease.yaml` ships) - this directory only decides *how* they
reach the cluster, not what they are.

### Primary path: ExternalSecret

`externalsecret.yaml` is the default here: it needs the External Secrets Operator installed
already and a `ClusterSecretStore` named `secret-store` (a placeholder - patch it to the
operator's own store, Vault/OpenBao/AWS Secrets Manager/whatever is already running) carrying
every key the table above lists at the `remoteRef` paths the file comments. Nothing in Git is a
secret; a rotation happens entirely in the secret store, picked up on the next `refreshInterval`.

### Alternative: SOPS (secrets committed to Git, encrypted)

For an operator without a secret-store backend already running, SOPS encrypts the Secret itself
and commits the ciphertext - Flux's `kustomize-controller` decrypts it in-cluster, never storing
the plaintext in Git. `sops-secret.example.yaml` and `.sops.yaml` in this directory show the
*shape* only (hand-typed `ENC[...]` placeholders, not real ciphertext - they do not decrypt with
any key) and are **not** listed in `kustomization.yaml`'s `resources`, on purpose: an encrypted
Secret's `stringData` values only turn back into base64/plain strings inside a Flux
`Kustomization` whose own `decryption` field is configured; rendered through plain
`kubectl kustomize`/`kubeconform` as this directory's other files are, they would be ciphertext
where a Secret value belongs and fail schema validation.

To make it real:

1. `age-keygen -o agekey` (keep the private key out of Git - a password manager, or a
   Kubernetes `Secret` named `sops-age` in the `flux-system` namespace that Flux's own
   `decryption` field reads).
2. Replace the placeholder `age:` recipient in `.sops.yaml` with the public key `age-keygen`
   printed (the `# public key: age1...` comment line it writes to stdout).
3. Fill in `sops-secret.example.yaml`'s `stringData` with the real values (plain text at this
   point), rename it without `.example`, and run `sops -e -i <file>` - SOPS encrypts exactly the
   fields `.sops.yaml`'s `encrypted_regex` names (`data`/`stringData`), leaving `apiVersion`/
   `kind`/`metadata` readable so Git history and diffs still show *what* changed, never the value.
4. Commit the encrypted file, add it to `kustomization.yaml`'s `resources`, and set
   `helmrelease.yaml`'s own `secrets` → `existingSecret` to this Secret's `metadata` → `name`.
5. Add a `decryption` field to the operator's own Flux `Kustomization` that applies this directory:

   ```yaml
   spec:
     decryption:
       provider: sops
       secretRef:
         name: sops-age
   ```

## Applying this

Same pattern as `deploy/README.md`'s own "Operator overlay" example, with `path: ./deploy/flux`
instead of `./deploy` and no `images:` override (the chart's own `image` → `tag` takes that role,
via `helmrelease.yaml`'s `values`):

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
  path: ./deploy/flux
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
    - target:
        kind: ExternalSecret
        name: memory-manager-secrets
      patch: |
        - op: replace
          path: /spec/secretStoreRef/name
          value: my-real-secret-store
```

## Validate before applying

```sh
kubectl kustomize deploy/flux | kubeconform -strict -summary \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
```

Validates `namespace.yaml`, `helmrepository.yaml`, `externalsecret.yaml` and `helmrelease.yaml`
against the real Flux/External Secrets CRD schemas (the CRD catalog `-schema-location` above);
it does not render the Helm chart itself (that is `helm template` + `kubeconform`, already covered
by `charts/memory-manager/README.md`'s own "Validate before installing" and the CI `deploy` job).

## Not included

- Cloudflare Tunnel as an `HTTPRoute`/`Ingress` alternative - [`docs/guides/cloudflare-tunnel.md`](../../docs/guides/cloudflare-tunnel.md) (#47).
