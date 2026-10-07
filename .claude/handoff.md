Date: 2026-10-07 · Branch: main · Last commit: release 0.1.3

## Done

- All milestones M0 to M6 are done. Every planned issue is closed and verified.
- v0.1.0 to v0.1.3 are released, each image signed and checked with `cosign verify`. 0.1.3 runs on the owner's deployment, which is delivered through an operator overlay kept outside this repository.
- The owner verified the remote path on 2026-10-07: claude.ai connects through CIMD and OIDC and writes notes (vault commits by `claude-ai`), and Claude Code finds them via OAuth. The Claude Code skill shows up in `/skills`. Steps are in `docs/guides/remote-connect.md`.
- Live fixes after 0.1.0:
  - #80 (0.1.1): claude.ai's CIMD document has no `scope`, which caused invalid_scope.
  - #82 (0.1.2): CIMD fetches pinned an IPv6 address at random in an IPv4-only cluster.
  - #77: deploy keys without a trailing newline now load.
  - #85 (0.1.3): access logs redact OAuth codes, state and pending ids.
- The smoke-test note has been archived.

## In progress

- Nothing.

## Failed / dead ends

- Storing private keys through `$(cat key)` strips the trailing newline, and OpenSSH then fails with `error in libcrypto`. The app tolerates this since #77.
- release-please needs the repo setting that lets Actions create PRs. Tags and PRs it creates with GITHUB_TOKEN trigger no other workflows, so it dispatches `release.yml` and `validate.yml` itself. A closed release PR keeps its `autorelease: pending` label, so remove the label before regenerating.
- A CDN in front of the server blocks the `Python-urllib` user agent.

## Next step

- #89: let release-please bump uv.lock (cosmetic).
- Rollouts: bump the tag in the operator overlay (see `docs/releasing.md` and the overlay's runbook).

## Open questions

- None.
