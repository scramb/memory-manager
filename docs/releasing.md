# Releasing

How a version gets from a merged commit on `main` to a signed, published
image and Helm chart. See [ADR-0002](./adr/0002-license.md) for why the
image carries `LICENSE`/SBOM metadata, and `CLAUDE.md`'s "Pull requests"
section for `CHANGELOG.md` being generated, never hand-edited.

## The two workflows

- **`.github/workflows/release-please.yml`** runs on every push to
  `main`. It keeps a single "chore(release): x.y.z" pull request open,
  built from [Conventional Commits](https://www.conventionalcommits.org/)
  since the last release (`release-please-config.json`,
  `.release-please-manifest.json`). That PR bumps `pyproject.toml`'s
  `version`, the two files below that carry an `x-release-please-version`
  marker, and `CHANGELOG.md` — never edited by hand (`CLAUDE.md`).
  Merging it tags the repo `vX.Y.Z` and creates the matching GitHub
  release (empty of assets at that point), then this workflow explicitly
  dispatches `release.yml` for that tag (see "Why the tag push doesn't
  trigger `release.yml` on its own" below).
- **`.github/workflows/release.yml`** builds, signs and publishes the
  image and the chart, then attaches the SBOM and the chart archive to
  the GitHub release (creating a `--prerelease` one first if
  release-please didn't, i.e. for a hand-pushed pre-release tag). It runs
  on:
  - a hand-pushed `v*` tag (e.g. a release candidate `v0.0.1-rc.1`) —
    the normal `push: tags:` trigger works here because a human pushed
    it with their own credentials;
  - `workflow_dispatch`, with the run's ref pointing at the tag —
    how `release-please.yml` starts it for a stable release (see below).

## Why the tag push doesn't trigger `release.yml` on its own

`release-please-action` tags and releases through the GitHub API using
the workflow's `GITHUB_TOKEN`. Per GitHub's own docs on
[triggering a workflow from a workflow](https://docs.github.com/en/actions/using-workflows/triggering-a-workflow-from-a-workflow#triggering-a-workflow-from-a-workflow)
(retrieved 2026-10-07):

> When you use the repository's `GITHUB_TOKEN` to perform tasks, events
> triggered by the `GITHUB_TOKEN` will not create a new workflow run.
> This prevents you from accidentally creating recursive workflow runs.

That rule has exactly two exceptions, both event types designed to be
started *by* another workflow: `workflow_dispatch` and
`repository_dispatch`. So without this workflow's explicit dispatch
step, merging the release-please PR would create tag `vX.Y.Z` and an
empty GitHub release, but `release.yml`'s `push: tags:` trigger would
never fire — no image, no signature, no chart. The same applies to the
release PR itself: release-please opens/updates it with `GITHUB_TOKEN`,
so its `pull_request` events never reach `validate.yml` either, which is
why `release-please.yml` dispatches `validate.yml` on the PR's head
branch as well (`dco.yml` is left out: it needs `pull_request` event
context for the base/head SHAs, and release-please's own commits are
exempt from that check regardless — see "Cutting a release" below).

`release-please.yml` therefore needs `permissions: actions: write` to
call `gh workflow run`, and reads the action's own outputs
(`release_created`, `tag_name`, `pr`) to know what to dispatch and
where — a hand-pushed pre-release tag skips all of this and reaches
`release.yml` through the ordinary tag-push trigger instead.

`release.yml`'s `workflow_dispatch` trigger takes no inputs: a dispatch
run's `github.ref`/`github.ref_name` already reflect whatever ref was
passed to `--ref` (here, the tag), exactly as for a real tag push, so
the rest of the workflow (version, image tags, cosign identity) derives
from `github.ref_name` unchanged. A guard step rejects any dispatch run
that isn't on a `v*` tag ref, so a stray manual dispatch on a branch
can't produce a broken "release".

## Version markers kept in sync by release-please

| File | Field | Marker |
|---|---|---|
| `pyproject.toml` | `version` | built-in Python updater (no marker needed) |
| `charts/memory-manager/Chart.yaml` | `version`, `appVersion` | `# x-release-please-version` |
| `deploy/kustomization.yaml` | `images[].newTag` | `# x-release-please-version` |

The Kustomize base's `newTag` is still a placeholder an operator overlay
patches explicitly for its own rollout (`deploy/README.md`) — release-please
only keeps the base's *default* pointed at the latest tag instead of a
stale `0.0.0`, it does not drive any deployment.

## What a tag publishes

For tag `vX.Y.Z` (or `vX.Y.Z-pre` for a pre-release):

- **Container image**, multi-arch (`linux/amd64`, `linux/arm64`), pushed to
  `ghcr.io/scramb/memory-manager`:
  - `:X.Y.Z` always
  - `:X.Y` and `:latest` additionally, but **only for a stable tag** — a
    pre-release never moves `latest` or the minor floating tag.
  - Built with BuildKit attestations (`provenance: true`, `sbom: true`)
    baked into the image manifest, build args `MM_GIT_SHA`/`MM_VERSION`
    (same as the local `make image` target).
- **Signature**: keyless `cosign sign` using the workflow's GitHub OIDC
  identity (no key material to manage or rotate). The same workflow runs
  `cosign verify` right after, so a broken signing setup fails the
  release instead of shipping an unverifiable image.
- **SBOM**: an SPDX document generated by `anchore/sbom-action` from the
  pushed image, attached to the GitHub release as `sbom.spdx.json` (in
  addition to the BuildKit attestation already inside the image).
- **Helm chart**, packaged with `version` and `appVersion` both set to the
  tag, pushed as an OCI artifact to `oci://ghcr.io/scramb/charts` and
  signed the same way as the image. The packaged `.tgz` is also attached
  to the GitHub release.

### Why OCI push instead of chart-releaser

Issue #52 named `chart-releaser` (which publishes a chart as a classic
Helm *index* repository served from GitHub Pages). The chart's own
`deploy/flux` example and `docs/releasing.md`'s "Chart (OCI)" summary
already point at `oci://ghcr.io/scramb/charts`, and an OCI registry is
what Helm 3 and Flux's `OCIRepository` both treat as first-class today —
adding a second, index-based distribution channel for the same chart
would be one more thing to keep in sync for no operator benefit. OCI push
via `helm push` is the direct equivalent and is what this workflow does.

## Verifying a release

```bash
# Image signature (replace <digest> with the one from the release's
# workflow summary, or `docker buildx imagetools inspect` the tag)
cosign verify \
  --certificate-identity-regexp 'https://github.com/scramb/memory-manager/.github/workflows/release.yml@refs/tags/v.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/scramb/memory-manager@<digest>

# Chart signature
cosign verify \
  --certificate-identity-regexp 'https://github.com/scramb/memory-manager/.github/workflows/release.yml@refs/tags/v.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/scramb/charts/memory-manager@<chart-digest>

# SBOM: download sbom.spdx.json from the GitHub release's assets
```

## Cutting a release

- **Stable**: merge the open "chore(release): x.y.z" PR from
  release-please. That tags, releases and triggers `release.yml`.
- **Pre-release / release candidate**: push a tag by hand, e.g.
  `git tag v0.1.0-rc.1 && git push origin v0.1.0-rc.1`. `release.yml`
  runs the same pipeline and marks the GitHub release `--prerelease`.

Upgrading an existing `0.1.x` deployment to `0.2.0`?
[`docs/guides/upgrade-0.2.md`](./guides/upgrade-0.2.md) covers what changes, staying on the Git
backend, moving to Postgres and rolling back.

Release-please's own commits (the "chore(release)" PR) and Dependabot's
commits carry no `Signed-off-by:` trailer - `.github/workflows/dco.yml`
exempts the `github-actions[bot]`, `release-please[bot]` and
`dependabot[bot]` author identities from that check; every human commit
still needs one.

## First release: package visibility

Packages published from this public repository were public from the first release (`v0.0.1-rc.1`, 2026-10-07). In forks or with other GHCR settings a new package can start **private**. In that case, set both packages to public once: Package settings → Danger Zone → Change visibility. Until then, clusters need `imagePullSecrets` and Flux needs a `secretRef` on the `HelmRepository`. To check that anonymous pulls work:

```bash
docker logout ghcr.io || true
docker pull ghcr.io/scramb/memory-manager:<version>
helm pull oci://ghcr.io/scramb/charts/memory-manager --version <version>
```
