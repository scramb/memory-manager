# Client acceptance checklist

This checklist lists the manual acceptance steps for every GUI, web and headless-CLI client in
the owner-approved support matrix ([`../clients/README.md`](../clients/README.md),
[F-02](../features/F-02-client-integrations.md), [`../PLAN.md`](../PLAN.md) → O21). Hermes Agent
and OpenClaw (tier 1, ADR-0014) are out of this checklist's scope: their acceptance is planned as
pinned-version CI end-to-end tests (WP-53/#186, WP-54/#189); until those land, neither client is
covered by any check, manual or automated. Open WebUI's native MCP path is planned to get its own
opt-in E2E test (`MM_E2E_OPENWEBUI=1`, #144), which #156 is planned to turn into a required CI
job; until both land, this checklist still keeps a manual UI check row for it, since the per-chat
tool-enable step will not be exercised by that planned CI run either.

## Scope

One row per client surface actually in use, in the same approval as
[`../clients/README.md`](../clients/README.md). "How checked" is `manual GUI` for an
interactively-driven GUI/web client, `headless harness (O21)` for a CLI run through the harness
once it exists (#161), or `not possible` where the matrix says so.

| Client | Approved level (verbatim) | Main caveat (verbatim) | How checked | Setup source |
|---|---|---|---|---|
| claude.ai | Full | — (verified 2026-10-07) | manual GUI | `memory-manager connect claude-ai` / `doctor --client claude-ai`; [page](../clients/claude-ai.md) |
| Open WebUI (UI check only) | Full (M13) | tool must be enabled per chat; DCR request shape unverified live | manual GUI | [research](../research/clients/openwebui.md) |
| Codex IDE extension | Full (cloud agent unverified) | reported MCP call cancellation under `codex exec` | manual GUI | [research](../research/clients/codex.md) |
| Gemini Code Assist (VS Code; IntelliJ unverified) | Partial | IDE agent mode only | manual GUI | [research](../research/clients/gemini.md) |
| GitHub Copilot in VS Code | Full | 128 tools per request | manual GUI | [research](../research/clients/copilot.md) |
| Copilot CLI | Full | 128 tools per request | manual (CLI; not in the headless harness, O21) | [research](../research/clients/copilot.md) |
| Copilot coding agent | Partial | needs a static token; firewall reachability unknown | manual GUI | [research](../research/clients/copilot.md) |
| Copilot in JetBrains | Partial | needs a static token; firewall reachability unknown | manual GUI | [research](../research/clients/copilot.md) |
| Cursor | Partial | reported: static `Authorization` header dropped when the server advertises OAuth | manual GUI | [research](../research/clients/cursor.md) |
| Google Antigravity | Partial | sources mostly secondary | manual GUI | [research](../research/clients/antigravity.md) |
| ChatGPT | Partial | write actions only on Business/Enterprise/Edu; mobile unclear; secondary sources | manual GUI | [research](../research/clients/chatgpt.md) |
| Gemini Enterprise | Partial | no DCR; ≤ 100 actions; pre-registered client only (ADR-0015, `oauth-client create`), live unverified | manual GUI | [page](../clients/gemini-enterprise.md) |
| Gemini app (consumer) | Not possible (reliably) | US/18+/personal/English only; token exchange reportedly broken [secondary]; revisit triggers on the page | not possible | [page](../clients/gemini-app.md) |
| Claude Code (headless CLI) | Full | — (verified) | headless harness (O21) | `memory-manager connect claude-code` / `doctor --client claude-code`; [page](../clients/claude-code.md) |
| Codex CLI (headless) | Full (cloud agent unverified) | reported MCP call cancellation under `codex exec` | headless harness (O21) | [research](../research/clients/codex.md) |
| Gemini CLI (headless) | Full | names ≤ 63 chars, `additionalProperties` stripped; free tier status unclear | headless harness (O21) | [research](../research/clients/gemini.md) |

Not listed, with reason: Hermes Agent, OpenClaw — both tier 1 (ADR-0014); their acceptance is
planned as pinned-version CI end-to-end tests (WP-53/#186, WP-54/#189). Until those tests land,
neither client is covered by any check, manual or automated; neither is part of this checklist.

## Test environment

Run every scope row against a disposable instance, never a production vault:

- Storage backend: `postgres` (ADR-0007) — the backend `audit_log` and the conflict steps below
  need.
- Server version/commit under test: record it in the Template section's heading for this run.
- Server URL: a placeholder, never a real hostname — `https://memory.example.com`.
- Auth: a static token from `memory-manager token create <tester-handle> --scope memory:read
  --scope memory:write --namespace '*'`, or the client's own OAuth flow against this disposable
  instance's authorization server.
- Content: fictional facts only (for example "the team's mascot is a quokka") — never real
  personal data, never anything copied from a production vault.

## Steps

Run S1–S6 in order, through the client under test's own UI/CLI, against the test environment
above.

| ID | Step | How | Expected result |
|---|---|---|---|
| S1 | Connect | Add the server using the client's Setup source (Scope table) | The client lists memory-manager's tools, or shows the server as connected |
| S2 | Auth | Complete the client's OAuth flow, or configure the static token from `memory-manager token create` | A tool call succeeds without an auth error |
| S3 | Search | Ask the client to find the test note by topic | The client's response includes the test note |
| S4 | Write | Ask the client to create or edit a note | A new `audit_log` row: `client` = this client's committer label, `op` ∈ `write`/`edit`, `outcome` = `ok`; the note's Git commit author carries that same label |
| S5 | Conflict | After the client has read the test note, change it through a second path (a direct Git commit, or a second token), then ask the client to edit it again | First an `audit_log` row with `outcome` = `conflict`; the client follows the conflict-merge loop ([`../memory-guide.md`](../memory-guide.md) → "The conflict-merge loop") and retries, producing a second row with `outcome` = `ok` and the merged content |
| S6 | Archive | Ask the client to archive the test note | An `audit_log` row with `op` = `archive`, `outcome` = `ok`; the note now lives under `_archive/`, not deleted |

S1–S3 touch no write path and leave no `audit_log` row by design (`src/memory_manager/audit.py:124-132`:
`outcome` is one of `ok`/`conflict`/`rejected`/`failed`, written only by the write hook) — check
them against the client's own response instead.

## Client notes

Deviations from a scope row's level or caveat, approved in
[`../clients/README.md`](../clients/README.md):

- **ChatGPT** — Pro is read-only (snippets only); run S1–S3, mark S4–S6 `n/a`.
- **Copilot coding agent** — needs a static token; no OAuth.
- **Open WebUI** — the tool must be enabled per chat; redo S1 whenever the chat changes.
- **Cursor** — reported: a static `Authorization` header is dropped when the server advertises
  OAuth [secondary]; check this explicitly during S2.
- **Gemini Enterprise** — not tested — needs a Gemini Enterprise tenant
  ([`../clients/gemini-enterprise.md`](../clients/gemini-enterprise.md)). S2: the Authorization
  URL Parameters field must carry `&resource=<URL-encoded resource>` (RFC 8707; this server
  rejects `/authorize` without it), PKCE must be enabled (S256 only), and Scopes must be exactly
  `memory:read memory:write`, never `offline_access`. S4/S5: expect the Git commit author to read
  `claude-code`, not `gemini-enterprise`, until #322 lands.
- **claude.ai** — `doctor --client claude-ai` fails by design at its first step ("config found");
  check the connection in claude.ai's Settings → Connectors instead
  ([`../clients/claude-ai.md`](../clients/claude-ai.md) → Troubleshooting).
- **Headless CLIs** (Claude Code, Codex CLI, Gemini CLI) — once #161 exists, run
  `make e2e-cli CLIENT=<name>` instead of walking S1–S6 by hand.

## Recording a run

Result vocabulary, one per cell:

- `pass` — the expected result was observed.
- `fail` — it was not; link the issue filed for it.
- `partial` — some but not all of the expected result held; say what, in Notes.
- `n/a (reason)` — the step does not apply to this client (see Client notes).
- `not tested — reason` — not run this cycle (blocked, not possible, out of time, ...).

Copy the Template section below under "Releases" for each run, newest entry on top. Never rewrite
a past release's section — a later finding gets its own new entry. Headless-harness results
(`make e2e-cli`) gathered before a release go into that release's own, upcoming section, never
retroactively into an older one.

## Template

### vX.Y.Z — server <version/commit>

| Client | Level | S1 | S2 | S3 | S4 | S5 | S6 | Client version | Date | Tester | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| claude.ai | Full | – | – | – | – | – | – | – | – | – | – |
| Open WebUI (UI check only) | Full (M13) | – | – | – | – | – | – | – | – | – | – |
| Codex IDE extension | Full (cloud agent unverified) | – | – | – | – | – | – | – | – | – | – |
| Gemini Code Assist (VS Code; IntelliJ unverified) | Partial | – | – | – | – | – | – | – | – | – | – |
| GitHub Copilot in VS Code | Full | – | – | – | – | – | – | – | – | – | – |
| Copilot CLI | Full | – | – | – | – | – | – | – | – | – | – |
| Copilot coding agent | Partial | – | – | – | – | – | – | – | – | – | – |
| Copilot in JetBrains | Partial | – | – | – | – | – | – | – | – | – | – |
| Cursor | Partial | – | – | – | – | – | – | – | – | – | – |
| Google Antigravity | Partial | – | – | – | – | – | – | – | – | – | – |
| ChatGPT | Partial | – | – | – | – | – | – | – | – | – | – |
| Gemini Enterprise | Partial | – | – | – | – | – | – | – | – | – | – |
| Gemini app (consumer) | Not possible (reliably) | not tested — documented as not possible | not tested — documented as not possible | not tested — documented as not possible | not tested — documented as not possible | not tested — documented as not possible | not tested — documented as not possible | – | – | – | see [`../clients/gemini-app.md`](../clients/gemini-app.md) for the reason and revisit triggers |
| Claude Code (headless CLI) | Full | – | – | – | – | – | – | – | – | – | – |
| Codex CLI (headless) | Full (cloud agent unverified) | – | – | – | – | – | – | – | – | – | – |
| Gemini CLI (headless) | Full | – | – | – | – | – | – | – | – | – | – |

## Releases

No release recorded yet; newest on top.
