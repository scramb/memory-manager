> [🇩🇪 Deutsch](../../../handover/HO-07-memory-manager-helm-chart.md) (Leitfassung) · 🇬🇧 **English**

# HO-07 — memory-manager as a generic Helm chart, the values for tethys live in tethys-cluster

Status: handed over · Version: 1 · As of: 2026-10-18 · Target date: none
Sender: `scramb/tethys-cluster` · ADR 0020 "Delivery as a Helm chart" · Receiver: `scramb/memory-manager`
Open decisions are decided by: the owner of both repos (scramb).

**Receiver copy, neutralized.** The binding version stays with the sender in `scramb/tethys-cluster` at `docs/handover/HO-07-memory-manager-helm-chart.md`. Because this repo is public, all operator values here are replaced by placeholders: hostnames, identity provider, vault remote and gateway references (`example.com`, `gateway-name`). The agreement is the same.

## Occasion

tethys is the owner's Kubernetes cluster. Flux applies everything in `scramb/tethys-cluster` there. memory-manager runs on it publicly, at an address of the operator.

Today Flux fetches the Kustomize base `deploy/` from `scramb/memory-manager`, pinned to the tag `v0.1.3`. tethys adapts it with patches that depend on internals of the base: the container name, the names of the environment variables, JSON paths. If the base changes, a patch breaks or a new placeholder goes live unpatched.

On 2026-10-18 the owner decided (ADR 0020 in tethys-cluster): every application ships a Helm chart from its own repo. Everything that applies only to tethys lives as values in `tethys-cluster`. tethys provides databases and secrets.

The repo already has a chart, `charts/memory-manager`, that covers most of this. This catalog names the four missing values. It also names what then leaves the app repo: the Kustomize base and `deploy/flux/`, a Flux setup of its own that does not belong in the app repo under the standard.

The cluster is being rebuilt right now. The switch to the chart happens on the new cluster. Until the signal "HO-07: chart ready", memory-manager runs there from the Kustomize base as it does today.

### What tethys patches today and where it goes

| Set by patch today | In the future | In chart `v0.1.3` |
|---|---|---|
| Pod label `istio.io/dataplane-mode: ambient`, which makes the pod join tethys's service mesh | additional pod labels (AF-01) | missing |
| `OIDC_ALLOWED_EMAILS` from a Secret, because the allowlist is personal and does not belong in Git | allowlist from a Secret key (AF-02) | missing, plain text only |
| `ALLOWED_ORIGINS` = `https://claude.ai,https://claude.com` | a value (AF-03) | missing |
| `FORWARDED_ALLOW_IPS` = `127.0.0.1`, set explicitly | a value (AF-04) | missing |
| `PUBLIC_URL`, `VAULT_REMOTE`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `LOGIN_NAMESPACES` | `publicUrl`, `vault.remote`, `login.oidc.*`, `login.namespaces` | present |
| `EMBEDDING_PROVIDER` = `none` | `embedding.provider: none` | present |
| HTTPRoute `parentRefs` and `hostnames` | `httpRoute.*` | present |
| Database: CNPG cluster `memory-manager-db` from the base, StorageClass by patch | entirely with tethys, the chart only reads the connection (`database.cnpg.enabled: false`, `database.existingSecret`) | present |
| `ExternalSecret` from the base, store and paths by patch | entirely with tethys, the chart references the Secret (`secrets.existingSecret`) | present |
| Image tag `0.1.3` | no value, `appVersion` at the pinned tag applies | present |

## Benefit

tethys runs memory-manager from the app repo's generic chart, with all tethys values in `tethys-cluster`: the app repo no longer contains anything that applies only to tethys, and tethys no longer patches internals.

**Done when:** tethys has closed the issue "HO-07: switched" in `scramb/memory-manager` (see Integration), and at the app repo's current tag neither `deploy/flux/` nor the Kustomize base exists.

## Requirements

Order = recommended order of work. Must: no integration without it · Should: needed before "Done when" · Could: blocks nothing, may come later.

Every requirement for a new value (AF-01 to AF-04) delivers four things:
- the value itself,
- an entry in `values.schema.json`,
- a line in the chart's README,
- a render proof with `helm template memory-manager <chart-path> -n memory-manager -f <file>`.

The value names are proposals. The receiver sets the binding ones.

| ID | Requirement | Acceptance criterion (verifiable from outside) | Priority | Depends on |
|---|---|---|---|---|
| AF-01 | The chart sets additional pod labels from a value (`podLabels`). | With `podLabels: {istio.io/dataplane-mode: ambient}` the pod template carries this label next to `app.kubernetes.io/name: memory-manager`. With `podLabels: "x"`, rendering fails on the schema. | Must | — |
| AF-02 | The allowlist can be read from a key of the Secret `secrets.existingSecret` (e.g. `login.oidc.allowedEmailsSecretKey`). | With the key set and `allowedEmails: ""`, `OIDC_ALLOWED_EMAILS` renders as a `secretKeyRef` to `memory-manager-secrets`/`OIDC_ALLOWED_EMAILS`. If both are non-empty, rendering fails with an error message. The plain value `allowedEmails` may stay. If the key is missing from the Secret, the pod ends in `CreateContainerConfigError`. That is accepted. | Must | — |
| AF-03 | The chart sets `ALLOWED_ORIGINS` from a value (e.g. `allowedOrigins`). | With `allowedOrigins: https://claude.ai,https://claude.com`, exactly this value is in the environment. If the value is empty, the variable is absent. | Must | — |
| AF-04 | The chart sets `FORWARDED_ALLOW_IPS` from a value (e.g. `forwardedAllowIps`). | With `forwardedAllowIps: 127.0.0.1`, exactly this value is in the environment. If the value is empty, the variable is absent. | Must | — |
| AF-05 | A published tag renders the complete example from the contract. | The signal "HO-07: chart ready" names the tag and the chart path. The image `ghcr.io/scramb/memory-manager:<appVersion>` can be pulled at that point. `helm template` with the example returns exactly what "Expected output" lists. | Must | AF-01 to AF-04 |
| AF-06 | The app repo no longer contains Flux objects. | At the next tag there is no `deploy/flux/`, and the command below the table returns nothing. | Should | — |
| AF-07 | The Kustomize base `deploy/` is removed after tethys has switched over. | At the first tag after "HO-07: switched" there is no `deploy/kustomization.yaml`. | Should | AF-05, signal "HO-07: switched" |
| AF-08 | CI renders the chart with a values file that uses the same keys as the example, with neutral placeholders. | The file lies under `charts/memory-manager/ci/`. A public CI run renders it green. | Could | AF-05 |

Proof for AF-06, from the root of the app repo:

```bash
git grep -nE 'kind: (HelmRelease|HelmRepository|GitRepository|OCIRepository)|kustomize\.toolkit\.fluxcd\.io' -- '*.yaml' '*.yml' ':!*.md'
```

## Interface contract

- **Direction and trigger:**
  - Flux in tethys fetches the chart from `https://github.com/scramb/memory-manager`, pinned to a tag, and installs it with the values from tethys-cluster.
  - Release name and target namespace are both `memory-manager`.
  - A rollout is a commit in tethys that raises the tag. The app repo pushes nothing into the cluster.
- **Data shape:** The interface is the chart's values. Here is the example of tethys's values:

```yaml
fullnameOverride: memory-manager
serviceAccount:
  create: false
podLabels:                                  # new (AF-01)
  istio.io/dataplane-mode: ambient
publicUrl: https://memory.example.com
vault:
  remote: git@git.example.com:owner/vault.git
  branch: main
login:
  mode: oidc
  oidc:
    issuer: https://idp.example.com/
    clientId: memory-manager
    allowedEmails: ""
    allowedEmailsSecretKey: OIDC_ALLOWED_EMAILS   # new (AF-02)
  namespaces: "*"
allowedOrigins: https://claude.ai,https://claude.com   # new (AF-03)
forwardedAllowIps: 127.0.0.1                # new (AF-04)
embedding:
  provider: none
secrets:
  existingSecret: memory-manager-secrets
database:
  cnpg:
    enabled: false
  existingSecret:
    name: memory-manager-db-app
    key: uri
httpRoute:
  enabled: true
  parentRefs:
    - name: gateway-name
      namespace: gateway-namespace
      sectionName: https
  hostnames:
    - memory.example.com
```

- **Expected output** of `helm template` with this example (checklist for AF-05):
  - **Objects:** exactly one `Deployment`, one `Service` and one `HTTPRoute`, all named `memory-manager`. There is no `Secret`, no `ServiceAccount`, no CNPG `Cluster`, no `Namespace`, no `Ingress`, no `NetworkPolicy`, no `ServiceMonitor` and no Flux object.
  - **Pod labels:** `app.kubernetes.io/name: memory-manager` and `istio.io/dataplane-mode: ambient`.
  - **Image:** `ghcr.io/scramb/memory-manager:<appVersion of the tag>`.
  - **Environment with fixed values:**
    - `PUBLIC_URL=https://memory.example.com`
    - `VAULT_REMOTE=git@git.example.com:owner/vault.git`
    - `VAULT_BRANCH=main`
    - `LOGIN_MODE=oidc`
    - `OIDC_ISSUER=https://idp.example.com/`
    - `OIDC_CLIENT_ID=memory-manager`
    - `LOGIN_NAMESPACES=*`
    - `ALLOWED_ORIGINS=https://claude.ai,https://claude.com`
    - `FORWARDED_ALLOW_IPS=127.0.0.1`
  - **Environment from Secrets:** `DATABASE_URL` from `memory-manager-db-app`/`uri`. `OIDC_ALLOWED_EMAILS`, `OIDC_CLIENT_SECRET`, `VAULT_WEBHOOK_SECRET` and `OAUTH_CLIENT_SECRET_KEY` from `memory-manager-secrets`.
  - **Volume** `ssh` from `memory-manager-secrets`, with `SSH_PRIVATE_KEY` and `SSH_KNOWN_HOSTS`.
  - **No** `EMBEDDING_PROVIDER`.
  - **HTTPRoute:** `parentRefs` and `hostnames` as in the example, backend `memory-manager:8080`.
- **Secrets tethys provides:** The chart only references them.
  - `memory-manager-secrets`: The keys `SSH_PRIVATE_KEY`, `SSH_KNOWN_HOSTS`, `VAULT_WEBHOOK_SECRET`, `OIDC_CLIENT_SECRET` and `OAUTH_CLIENT_SECRET_KEY` are already expected by chart `v0.1.3` (`templates/deployment.yaml`). `OIDC_ALLOWED_EMAILS` comes with AF-02.
  - `memory-manager-db-app` with the key `uri`, the connection to tethys's CNPG cluster.
- **Names tethys relies on:** the pod label `app.kubernetes.io/name: memory-manager`, which tethys's network policy targets (only the public gateway may reach the pod), and the object names `memory-manager`. The chart's selector (`name` and `instance`) stays as it is.
- **Error behaviour:**
  - If rendering or installation fails, the `HelmRelease` shows `Ready False` with the cause, and the last installed state keeps running.
  - tethys does not retry failed upgrades.
  - If the app repo is unreachable, the app keeps running with the last state.
- **Compatibility:**
  - A breaking change is a renamed or removed key, a new required key in the Secret, a new port or a new outbound connection. Such a change goes onto the delivered branch only after tethys has confirmed in an issue in the app repo that the new values are in place.
  - While `0.x` applies, a breaking change raises at least the minor version, after that the major version, and it is listed in `CHANGELOG.md`.
  - An existing tag never changes.
- **Binding version:** the chart's `values.yaml` and `values.schema.json` at the given tag. tethys only holds its own values. Until AF-05 is delivered, this section applies.

## Out of scope

- Publishing the chart as an OCI artifact. It may stay, tethys does not use it.
- The options `database.cnpg`, `secrets.create`, `ingress`, `networkPolicy` and `serviceMonitor`. They stay as they are for other operators. tethys does not turn them on.
- Namespace, secrets, database, network policy and taking over existing objects. All of that stays with tethys.
- Image automation and changes to the app's behaviour.

## Constraints

Decided by the owner on 2026-10-18 (ADR 0020). Not negotiable.

- **Location:** The chart lives in the app repo. Flux consumes it through a Git source on that repo with a `HelmRelease`. The receiver chooses the path.
- **No environment-bound values:** The chart contains no values that apply to only one environment: no hostname, no gateway reference, no StorageClass and no name of an operator environment. This can be checked by the fact that no name of an operator environment appears. It contains no namespace object and no Flux objects.
- **Image tag:** The default belongs to the chart (`appVersion`). tethys pins through the Git tag. A tag appears only once its image is in the registry.
- **Secrets:** With tethys's values the chart creates no Secret. Creating Secrets from values stays as today, an explicit opt-in that is off by default (`secrets.create: false`).
- **Database:** It belongs to tethys. The chart reads the connection from a Secret whose name is a value.
- **One-time recreation at the switch:** Deployment, Service and HTTPRoute may be recreated once during the switch. That means about one minute of downtime.
- **Target platform, as of 2026-10-18:**
  - Kubernetes 1.36.2,
  - Gateway API v1.5.1 (`HTTPRoute` `gateway.networking.k8s.io/v1`),
  - Flux v2.9.4 with `HelmRelease` `helm.toolkit.fluxcd.io/v2`, rendered by the helm-controller's Helm,
  - CloudNativePG for the database, run by tethys.

## Assumptions

| ID | Assumption | Basis | If wrong |
|---|---|---|---|
| A-01 | If `EMBEDDING_PROVIDER` is missing, the app behaves as with `none`. The chart omits the variable for `none` | Template `deployment.yaml` in `v0.1.3` | AF-05 needs the variable explicitly |

## Open decisions

| Question | Options | Blocks | Decides |
|---|---|---|---|
| F-01: Which data in the database can **not** be rebuilt from the vault? This matters for dump and restore during the cluster rebuild. According to the comments of the migrations `0002_static_tokens.sql` and `0003_oauth.sql` in `v0.1.3`, `static_tokens`, `oauth_clients`, `oauth_pending`, `oauth_auth_codes` and `oauth_tokens` are operational data, "never derivable from the vault" | A: only the search index can be rebuilt, everything else has to move with the dump · B: a different list, named by the receiver. Until it answers, tethys treats the whole database as not rebuildable and restores it completely. If these tables were lost in the restore, all OAuth clients would have to register again, and all static tokens would have to be issued again | no AF, only the restore at tethys | receiver answers |

The questions about the database, the selector and secret creation were decided by the owner on 2026-10-18. They are listed under Constraints.

## Integration

Integration work package: [`scramb/tethys-cluster#3`](https://github.com/scramb/tethys-cluster/issues/3).

Once AF-05 is delivered, the receiver opens an issue "HO-07: chart ready" in `scramb/tethys-cluster`. It names the tag, the chart path and the fulfilled AFs.

tethys then switches over on the rebuilt cluster:
- It provides the database and the secrets.
- It deletes the old `Deployment`, `Service` and `HTTPRoute` objects from the Kustomize delivery before the `HelmRelease` installs. This is necessary because of the different selector, about one minute of downtime.
- It installs the chart at the named tag.

Afterwards tethys re-checks from outside:
- The `HelmRelease` shows `Ready True`.
- `https://memory.example.com/healthz` returns `200`.
- `/mcp` without a token returns `401`.
- `/.well-known/oauth-protected-resource/mcp` names `resource` `https://memory.example.com/mcp`.
- Login works in the browser.
- The connector in claude.ai keeps working without registering again.

Once that holds, tethys reports it with an issue "HO-07: switched" in `scramb/memory-manager`. That unblocks AF-06 and AF-07.

## Changes

| Version | Date | What |
|---|---|---|
| 1 | 2026-10-18 | Draft. The owner's decisions from ADR 0020 and the cross-examination findings worked in; handed over |
