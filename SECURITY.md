# Security Policy

## Scope

In scope: this repository's server code (`src/memory_manager/`), CLI, container
image, and deployment manifests (`Dockerfile`, `charts/`, `deploy/`). Out of
scope: a specific operator's deployment (hostnames, cluster configuration,
TLS termination in front of this process), the git hosting provider securing
the vault remote, an optional third-party embedding API an operator chooses
to configure (see "Security properties" below), and the behaviour of MCP
clients themselves (claude.ai, Claude Code).

## Security review

[`docs/security-review.md`](./docs/security-review.md) is a manual review
against the OWASP Top 10 for LLM Applications 2025 plus classic web/OAuth
concerns (authn, session/token, access control, SSRF, injection, logging),
last performed 2026-10-07. Every finding is either fixed (with a test) or
explicitly accepted with a rationale; re-run it (or review it again) before
any release that touches `src/memory_manager/auth/`, `mcp/`, `vault/`, or
`index/`.

## Security properties

These are the non-negotiable properties this project is built to (see
`CLAUDE.md`'s "Project-specific rules" for the authoritative list):

- **Git is the source of truth.** The Postgres index is always fully
  rebuildable from the vault (`reindex --full`); nothing except operational
  data (tokens, the audit log, OAuth state) lives only in Postgres.
- **Never overwrite silently.** Every write carries `if_version`; a conflict
  is reported to the caller with the current content and version, never
  applied over it.
- **Never hard-delete notes.** A note is archived (`_archive/`), never removed.
- **Note content is data, not instructions.** No code path interprets note
  content as a command; every tool description says so explicitly.
- **Path allowlist, no symlinks.** Every note path is validated against a
  strict shape (`<namespace>/<type>/<slug>.md`) and resolved without ever
  following a symlink, on both the write path and every read-enumeration path
  (`docs/security-review.md`'s F-01).
- **Secrets are never stored verbatim.** A secret scan runs before every
  commit; tokens are stored as hashes only; a rejected write's error never
  echoes the matched secret text back.
- **Audit log for every write.** Every processed write — success, conflict,
  rejection or failure — gets one `audit_log` row.
- **An optional embedding provider is a deliberate data flow, not a leak.**
  Configuring `EMBEDDING_PROVIDER=openai` (or any other external
  OpenAI-compatible endpoint) sends note content to that API to be embedded;
  `EMBEDDING_PROVIDER=ollama` or `none` keep every byte on the operator's own
  infrastructure. Choose accordingly.

## Reporting a vulnerability

Please report security vulnerabilities through GitHub's private vulnerability reporting, not
through a public issue:

1. Go to the [Security tab](../../security) of this repository.
2. Open **Advisories** → **Report a vulnerability**.

This opens a private channel with the maintainers so the issue can be assessed before any public
disclosure.

## Supported versions

Pre-alpha: there is no tagged release yet, and no versioned support policy applies. Once
v0.1.0 ships, only the latest minor release receives security fixes:

| Version | Supported |
|---|---|
| latest minor | yes |
| older minors | no |

## Response targets

- **Acknowledgement:** within 7 days of the report.
- **Fix or public advisory:** within 90 days of the report, depending on severity and complexity.

These are targets, not contractual guarantees; this is a self-hosted open-source project
maintained on a best-effort basis.
