# ADR-0003 — Git access: git CLI via a thin subprocess wrapper

Status: Accepted · Date: 2026-10-06
Relates to: vault component, write queue; T-014, T-015, T-018

## Context

The vault clones a remote (GitHub, Gitea, …), commits every change individually with the client as author, pushes, pulls human changes, and on push rejection rebases; on rebase failure it writes a `*.conflict.md` (see PLAN). Auth to the remote is HTTPS token or SSH deploy key. Language is Python (ADR-0001). Only one writer process touches the working copy (single-writer, see PLAN).

## Options

### A — git CLI, wrapped in `memory_manager/vault/git.py`
Pro: exactly the behaviour humans get (`fetch`, `rebase`, `push --force-with-lease` never needed); full SSH/HTTPS/credential-helper support; rebase, conflict detection and `rerere`-free abort are one command each; no native Python dependency; debuggable by running the same command by hand · Con: subprocess calls and output parsing (porcelain v2 / `-z` keeps it robust); runtime image must contain `git` (no pure distroless Python image); ~5–20 ms per call.

### B — `pygit2` 1.20.1 (libgit2 bindings)
Pro: in-process, typed objects, wheels bundle libgit2; has a low-level `Rebase` API · Con: no `pull`/high-level rebase — the rebase loop, conflict handling and fast-forward logic must be written by hand; SSH through libssh2 is a frequent source of auth problems; libgit2 behaviour differs from git in edge cases (hooks are not run, config subtly ignored).

### C — GitPython 3.2.0
Pro: Pythonic API · Con: wraps the git CLI anyway (so all cons of A) plus its own abstraction layer; project is in maintenance mode.

## Decision

**A — git CLI**, accepted by the owner on 2026-10-06. The hard part of this component is conflict handling during rebase, and that is exactly what the CLI does correctly and what libgit2 leaves to the caller. With a single writer and one-file commits the performance difference is irrelevant.

Checked against the guardrails:
- Few dependencies: none in Python; `git` in the image.
- OSS first: yes.
- Container: runtime base `python:3.12-slim` + `git` + `openssh-client`, non-root, read-only root FS with the vault on a volume.
- Technology pool: n/a (tooling choice inside ADR-0001).

## Consequences

- Every git call goes through one wrapper with timeout, fixed env (`GIT_TERMINAL_PROMPT=0`, `LC_ALL=C`, no global config) and argument lists (never a shell string).
- Commit identity per call: `-c user.name=<client> -c user.email=<client>@<domain>`.
- Integration tests run against a local bare repository — no network needed.
- Secret scan runs in Python before `git commit`, not as a git hook (hooks are not a security boundary).

## Reversibility

Cheap: the wrapper is the only module that knows how git is invoked; swapping to pygit2 later touches one module.
