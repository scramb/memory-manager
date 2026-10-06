# ADR-0002 — License: AGPL-3.0-only, contributions under DCO

Status: Accepted · Date: 2026-10-06
Relates to: `LICENSE`, every source file, network-facing server behaviour

## Context

The project is open source from day one and meant to be self-hosted. It stores personal memory, so auditability and the right to fork matter. The license must be fixed before the first commit: relicensing later needs the consent of every contributor. Expected dependencies (MCP SDK: MIT; Starlette, uvicorn, psycopg, pgvector, pydantic: BSD/MIT/LGPL/PostgreSQL; argon2-cffi: MIT) are all compatible with GPLv3-family licenses; Apache-2.0 code is compatible with GPLv3/AGPLv3 too.

## Options

### A — Apache-2.0
Pro: permissive, explicit patent grant, easiest corporate adoption · Con: allows closed hosted forks (e.g. a SaaS "memory for Claude" built on this code without giving back).

### B — MIT
Pro: shortest, maximally permissive · Con: no explicit patent grant; same closed-fork situation as A.

### C — AGPL-3.0
Pro: anyone who offers a modified version as a network service must offer its source to the users of that service (§13); includes a patent grant (GPLv3 §11); strong copyleft keeps improvements public · Con: some companies ban AGPL internally, which can block adoption at work; contributors must accept copyleft.

## Decision

**C — AGPL-3.0-only**, chosen by the owner on 2026-10-06. A memory server is exactly the kind of software that gets wrapped into a hosted service; the AGPL keeps such derivatives open. Contributions are accepted under the same license with a DCO sign-off (`Signed-off-by:`), no CLA.

`-only` (not `-or-later`) is the literal reading of the owner's choice; switching to `-or-later` is possible now at no cost and becomes expensive after the first external contribution.

Checked against the guardrails:
- Few dependencies: n/a; every dependency is checked for AGPL compatibility when added (no GPLv2-only, no proprietary).
- OSS first: OSI-approved.
- Container: images carry `LICENSE` and an SBOM; the image label `org.opencontainers.image.source` points to the repository.
- Technology pool: n/a.

## Consequences

- `LICENSE` holds the canonical AGPL-3.0 text; source files carry `# SPDX-License-Identifier: AGPL-3.0-only`.
- **§13 compliance is a feature:** the server advertises its source URL (MCP `serverInfo.websiteUrl` / landing page and `/healthz` build info) so users of a modified deployment can get the corresponding source. Configurable for forks (`SOURCE_URL`).
- `CONTRIBUTING.md` requires DCO sign-off; a DCO check runs on PRs.
- Running an unmodified copy for yourself or your company triggers no obligations beyond the license itself; the README says so plainly to reduce adoption friction.
- Notes stored in the vault are user data, not covered by the license.

## Reversibility

Cheap until the first external contribution (owner holds all copyright); expensive afterwards.
