# Contributing

Thanks for considering a contribution to memory-manager. The short version: sign off your
commits, follow the branch/commit/PR rules in [`CLAUDE.md`](./CLAUDE.md), and run `make check`
before you push.

## Setup

```sh
uv sync
uv run pre-commit install
```

`uv sync` creates the virtual environment and installs the dependency groups from
`pyproject.toml`. `pre-commit install` wires up the local hooks (format, lint, docs checks) so
they run automatically on `git commit`.

## Developer Certificate of Origin (DCO)

Every commit must carry a `Signed-off-by:` trailer, certifying that you wrote it or otherwise
have the right to submit it under the project's license (AGPL-3.0-only). Add it with:

```sh
git commit -s
```

A CI check rejects pull requests containing commits without a sign-off.

## Branches, commits, pull requests

The full rules — one branch per work package, Conventional Commits, one PR per work package
against `main` — are defined in [`CLAUDE.md`](./CLAUDE.md#working-rules) and apply to every
contribution, internal or external. Read that section before opening a PR.

In short:

- Branch from the current `main`: `wp/<nr>-<slug>`.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/), one task per
  commit, sign-off required.
- Open the PR against `main`; describe the goal of the work package, the tasks it closes, what
  you verified and how, and what was deliberately left open.

## Before every commit

```sh
make check
```

This runs formatting/lint checks (`ruff`), strict type checks (`mypy`), the test suite
(`pytest`) and the planning/docs checks (`scripts/check-docs.sh`). A commit that fails `make
check` locally will also fail in CI.

## Reporting bugs and proposing work

Open a GitHub issue using the provided templates (bug report or task). See
[`SECURITY.md`](./SECURITY.md) instead if you are reporting a security vulnerability.
