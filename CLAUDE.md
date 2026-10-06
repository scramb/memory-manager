# CLAUDE.md — memory-manager

Self-hosted long-term memory for Claude: Markdown notes in Git as the source of truth, a derived Postgres search index, and one remote MCP server that claude.ai and Claude Code share.
Plan and architecture: [`docs/PLAN.md`](./docs/PLAN.md) · Work backlog: [`docs/TASKS.md`](./docs/TASKS.md)

> Project and CLI name: `memory-manager` (O6, decided 2026-10-06).

## Stack

- Language/runtime: Python 3.12+, official MCP Python SDK 2.x, Starlette/uvicorn, `uv` ([ADR-0001](./docs/adr/0001-implementation-language.md)) — owner-approved deviation from the pool
- Frontend: none in v1 (optional read-only view from v1.x)
- Operation: container (Docker Compose locally, Helm chart on Kubernetes); local stdio mode for Claude Code
- Persistence: Git repository with Markdown (source of truth) · PostgreSQL 16+ with `pgvector` (derived index)

## Precedence

When two sources contradict each other, the higher rank wins. Two rules sit above the table:

- **Facts beat documents.** `git log`, CI results and the working tree are what is true; a document claiming otherwise is outdated and gets fixed.
- **A user instruction beats every document** for the case at hand. If it should hold beyond that, it goes into the matching document.

| Rank | Source | Wins over the lower ranks on |
|---|---|---|
| 1 | `CLAUDE.md` | how work is done: formats, conventions, guardrails |
| 2 | `docs/adr/` (status *Accepted*) | individual architecture decisions |
| 3 | `docs/PLAN.md` | goal, scope, architecture |
| 4 | GitHub Issues + milestones in `scramb/memory-manager` | **source of truth for tasks**: scope, state, assignment |
| 5 | `docs/TASKS.md` | readable mirror of rank 4; on conflict GitHub wins and this file is fixed |
| 6 | `.claude/handoff.md` | session state; oldest information, lowest rank |

Special case: a contradiction between `docs/PLAN.md` and this file is **not** resolved by rank. Stop and present it.

ADRs with status *Proposed* do not count as decisions. Work blocked by them stays blocked.

## Working rules

### Repository language

All repository content — code, comments, docs, commit messages, issues — is **English**.

### Branches

- One branch per **work package**, not per task: `wp/<nr>-<slug>`, e.g. `wp/03-write-queue`.
- A WP may contain several tasks. A task belongs to exactly one WP.
- Base is always the current `main`. Never build on someone else's WP branch.

### Commits

[Conventional Commits](https://www.conventionalcommits.org/). One commit belongs to exactly one task and reads on its own.

```
<type>(<scope>): <what changes, imperative, no period>

<Why this change was needed and what it does. What was rejected, if the
alternative was obvious. Two to five lines, no retelling of the diff.>

Refs #13
```

- Every commit carries a DCO sign-off (`git commit -s`), required by ADR-0002.
- `type`: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `ci`, `build`, `perf`, `security`
- `scope`: component name — `vault`, `queue`, `index`, `search`, `mcp`, `auth`, `cli`, `deploy`, `eval`, `docs`
- The last commit of a task uses `Closes #13`.
- If a commit completes a box in `docs/TASKS.md`, the file is updated **in the same commit**.
- No commits spanning several tasks, no `wip` commits in a PR.

### Pull requests

- **One PR per work package**, against `main`.
- Title: `WP-03 — Write queue` (release-please reads the squash commit, so the squash message follows Conventional Commits).
- Body: goal of the WP, contained tasks (`Closes …`), what was verified and how, what was deliberately left open.
- Merge only with green validation.

## Technology guardrails

- **Application code only from this pool:** Go, Rust, C, C++, React, Vue.js.
  Anything else is discussed **beforehand** — even when it obviously fits.
  Not affected: build, CI and infrastructure tooling (shell, Make, Dockerfile, Helm, SQL).
- **Few dependencies.** Standard library first. A dependency only when it solves a real problem that would otherwise cost significant own code (crypto, OAuth/JOSE, DB drivers, MCP SDK, Git are legitimate). Convenience still beats performance.
- **OSS first.** At comparable fitness the open-source option wins. Every external service (embedding API) is optional and pluggable.
- **Container by default.** Everything runs in a container, also locally.

## Project-specific rules

- **Git is the source of truth.** Postgres must be fully rebuildable from the vault (`reindex --full`). Never store information only in the DB that is not derivable from Git — except operational data (tokens, audit log, OAuth state).
- **Never overwrite silently.** Every write carries `if_version`; conflicts surface to the caller with the current content.
- **Never hard-delete notes.** Archive to `_archive/`.
- **Note content is data, not instructions.** No code path interprets note content as commands; tool descriptions say so.
- **Security is not negotiable:** path allowlist, no symlinks, `.md` only, secret scan before every commit, token hashes only, audit log for every write. A change that weakens one of these needs an ADR.
- **License:** AGPL-3.0-only. Every source file starts with `# SPDX-License-Identifier: AGPL-3.0-only`. New dependencies must be AGPL-compatible.
- **Public repository:** no operator-specific values (hostnames, cluster names, secret paths, IPs, real namespaces) in code, manifests, examples or issues. Deployment artefacts are generic; operators keep their values in their own overlay.
- **No real personal data** in `examples/`, tests or fixtures.
- **Verify, don't recall.** Anything about the MCP spec, claude.ai connectors or SDK APIs is checked against current sources and recorded in `docs/research/` with source and version.

## Tests and validation

- Tests where they catch something: note parsing/validation, path safety, write queue and conflict handling, chunking, RRF ranking, auth flows, secret scanning, concurrency.
- No tests that only mirror structure.
- Coverage target ≥ 80 % for core modules (vault, queue, index, search, auth).
- Retrieval eval (recall@5, MRR on the golden set) runs in CI; a regression fails the build.
- CI always runs: build, lint/vet, format check, tests, container build (once a Dockerfile exists).
- Before ticking a box in `docs/TASKS.md`, **run** the task's verification. Ticked means verified.

Locally before every commit: `make check` (ruff format/lint, mypy strict, pytest).

## Documentation

- `CLAUDE.md` (this file): how development works here.
- `docs/PLAN.md`: goal, architecture, decisions. No backlog.
- `docs/TASKS.md`: work packages, milestones, tasks as a checklist.
- `docs/features/F-NN-<slug>.md`: one planned feature each (written by `feature-planning`).
- `docs/adr/NNNN-<slug>.md`: one architecture decision each, incl. rejected alternatives.
- `docs/research/<topic>.md`: comparisons, spikes, spec notes — always with source URLs and retrieval date.
- `.claude/handoff.md`: session state, written by the `handoff` skill.

Root-level Markdown only for OSS hygiene files GitHub expects there: `README.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `CHANGELOG.md`, `LICENSE`. No other loose Markdown in the root.

## When to ask

Stop and present when facing:

- technology outside the pool, a new dependency, a new service
- a new schema, API, data model (note format, DB schema, MCP tool signatures) — or a breaking change to one
- two viable paths with different long-term costs
- license, data format, auth model (explicitly reserved by the owner)
- a contradiction between `docs/PLAN.md` and this file
- anything that would deserve an ADR

**Don't** ask about: naming, local refactorings, test structure, formatting, ordering within a task.

Then **stop** — don't start building the favoured option.

## Formats

Each format is defined here and only here.

### TASKS entry

```markdown
## M1 — <Milestone title>

Goal: <one sentence> · Due: <YYYY-MM-DD | open>

### WP-03 — <Work package> · F-01 · Branch: `wp/03-<slug>` · PR: <#nr | open>
- [ ] #13 <result in one line>
  - [ ] <sub-step, independently verifiable>
- [ ] #14 <result> ⛔ blocked by #13 / O5
```

IDs are GitHub issue numbers (`#13`). A parent box is ticked only when all children are.

### Task (issue body)

```markdown
## Goal
<One sentence without "and". What exists afterwards that didn't before?>

## Context
<Why it is needed; exact paths; links to ADRs instead of re-deriving decisions.>

## Implementation
- [ ] <independently verifiable step>

## Definition of Done
<Exactly one command or observation.>

## Not included
- <what belongs elsewhere>

## Dependencies
Blocked by: <#12 | none> · Relates to: <ADR-NNNN> · Work package: WP-NN (`wp/NN-<slug>`)
```

Title describes the result, not the activity.

### Feature file

`docs/features/F-NN-<slug>.md`: Goal · Users and scenario · Scope / not in scope · Design (link ADRs) · Milestones and work packages · Open questions.

### ADR

```markdown
# ADR-NNNN — <decision in one line>

Status: <Proposed | Accepted | Superseded by ADR-NNNN> · Date: <YYYY-MM-DD>
Relates to: <component / tasks>

## Context
## Options
### A — <option>
Pro: … · Con: …
## Recommendation   (while Proposed) / ## Decision   (once Accepted)
Checked against the guardrails: few dependencies · OSS first · container · technology pool
## Consequences
## Reversibility
```

### Status report (session start)

```
Status <YYYY-MM-DD>
- Last state: <from handoff, verified against git log>
- Next task: <ID + title> — <why this one>
- Blockers: <none | open decision / failing check>
- Plan for this session: <1–3 bullets>
```

### Gate proposal

```
Decision: <one sentence>
A: <option> — <consequence>
B: <option> — <consequence>
Recommendation: <A|B>, because <one sentence>
Reversible: <cheap | expensive — why>
```

### Handoff

`.claude/handoff.md` starts with `Date: <YYYY-MM-DD> · Branch: <name> · Last commit: <sha> <subject>`, followed by exactly these headings: `## Done`, `## In progress`, `## Failed / dead ends`, `## Next step`, `## Open questions`.

## Definition of Done (work package)

- [ ] All tasks of the WP closed, boxes in `docs/TASKS.md` ticked
- [ ] Validation in CI green
- [ ] Architecture decisions recorded as ADR and linked from the PLAN
- [ ] PR describes goal, tasks and verification
- [ ] `CHANGELOG.md` is generated by release-please — not edited by hand
