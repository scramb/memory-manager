Date: 2026-10-07 · Branch: main · Last commit: 24c93c1 chore(main): release 0.1.0 (#76)

## Done

- M0 to M6 implemented and merged (WP-01 to WP-15). v0.1.0 is released: https://github.com/scramb/memory-manager/releases/tag/v0.1.0. The signed image `ghcr.io/scramb/memory-manager:0.1.0` passes `cosign verify`, and the chart (`oci://ghcr.io/scramb/charts/memory-manager`) and SPDX SBOM are attached.
- Deployed on the owner's cluster via an operator overlay kept outside this repository (OIDC provider plus app delivery). `/healthz` reports 0.1.0, `/readyz` returns 200, PRM, AS metadata and the 401 challenge are correct.
- End-to-end check with a static token over HTTPS: `memory_write` lands as a commit by `claude-code` in the private vault repo, and `memory_search` finds the note (fulltext mode). The test tokens are revoked. The note `ops/reference/deployment-smoke-test.md` is still in the vault and can be archived.
- OAuth flow verified automatically up to the upstream IdP login page: DCR, `/authorize` with PKCE + `resource`, the login interstitial showing the redirect host, and the handoff to the OIDC provider as client `memory-manager`.

## In progress

- Nothing is in a half-done state.

## Failed / dead ends

- The deploy key stored through `$(cat key)` lost its trailing newline, and OpenSSH failed with `error in libcrypto`. The key was rotated and re-stored with a newline. App robustness is tracked in #77.
- Cloudflare in front of the host blocks the default `Python-urllib` user agent (403). Real clients are unaffected; scripts need a user agent.
- release-please: Actions could not create PRs (repo setting changed), and plain YAML extra-files only bumped `$.version` (fixed with the generic updater, #74). Tags and PRs created with GITHUB_TOKEN trigger nothing, so release-please now dispatches `release.yml` and `validate.yml`.

## Next step

- #40: owner adds the claude.ai custom connector (`https://<host>/mcp`) and logs in via OIDC. Then `claude mcp add --transport http` in Claude Code with the OAuth login. A note written in one client must be found with `memory_search` in the other; record the client versions and the date in docs.
- #22: owner copies `integrations/claude-code/skills/memory/` to `~/.claude/skills/` and checks that `/skills` lists `memory`.
- #47: one live run of the Cloudflare Tunnel guide (optional; needs a Cloudflare tunnel).
- #77: newline-tolerant deploy key, then a patch release.

## Open questions

- Does the upstream IdP put `email` + `email_verified` into the userinfo for the owner's account? If the OIDC login is denied, add the IdP subject to the allowlist (`OIDC_ALLOWED_SUBJECTS`).
