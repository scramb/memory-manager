# F-02 — Client Integrations

Status: planned · Created: 2026-10-07
Milestones: M12, M13, M14, M15, M16, M17 · Work packages: WP-35 … WP-62 · Label: `feature:F-02`

## Benefit

A memory-manager user can use the same memory from every common AI client: claude.ai, Claude Code, Open WebUI, Cursor, OpenAI Codex, GitHub Copilot, Google Antigravity, Gemini CLI / Code Assist, Hermes Agent, OpenClaw, ChatGPT and Gemini Enterprise. Each client either has a tested, documented integration or a documented reason why it cannot have one. One command (`memory-manager connect <client>`) sets it up, and one command (`memory-manager doctor --client <client>`) proves it works. Open WebUI ships first, as its own release. The feature ends in `v1.0.0-rc.1` with a stable, documented API.

**Done when:**
- The owner-approved support matrix (`docs/clients/README.md`) is implemented. Every *Full* or *Partial* client has a profile that passes the conformance suite.
- E2E tests are green for Open WebUI, Hermes and OpenClaw (scripted stub model, no API keys). The headless CLI clients (Claude Code, Codex CLI, Gemini CLI) are verified manually with the harness (O21) and recorded in the checklist.
- The manual checklist `docs/release/client-checklist.md` is ticked for the release.
- `v1.0.0-rc.1` is tagged with a signed image and chart.

The prompt's milestones M1–M5 map to project-wide numbers: M1 → **M12**, M2 → **M13**, M3 → **M14**, M3b → **M15**, M4 → **M16**, M5 → **M17**.

## Users and scenario

- **Individual user (Git backend, static token or OAuth).** Uses claude.ai on the phone, Cursor and Codex at work, and Open WebUI with a local model at home. All of them share one memory.
- **Organisation (Postgres backend, Entra, F-01).**
  - An admin connects Open WebUI for 500 users, and each user lands in their own `me` namespace.
  - IT rolls out the server to Copilot Business and ChatGPT Enterprise through their admin policies.
- **Agent operator.** Runs Hermes or OpenClaw on a home server with Telegram attached. The agent remembers what its owner tells it, but strangers in a group chat cannot plant memories.

## Not in scope

- Browser extensions for any client.
- Forked or per-client tool contracts. One server, one tool surface ([ADR-0010](../adr/0010-client-compatibility-profiles.md)).
- Automatic fact extraction from conversations. The Open WebUI `outlet` and the agent integrations act only on explicit user requests (PLAN "Explicitly not").
- An OpenAPI facade, unless research shows a supported client needs one. Open WebUI has native MCP since v0.6.31.
- Accepting IdP-issued tokens directly (resource-server mode, [ADR-0006](../adr/0006-enterprise-auth-entra.md) §8). It is needed for Open WebUI token forwarding (ADR-0011 option B) and stays a follow-up.
- Trusted identity headers from Open WebUI (ADR-0011 option C). They may follow later through their own ADR with a threat model, never as the default.
- Brand logos in README or docs.
- Native memory plugins for agent runtimes (tier 2) — after 1.0 ([ADR-0014](../adr/0014-agent-integration-tier.md)).
- A docs website generator; docs stay Markdown on GitHub (O20).
- Model API keys in CI; CLI clients are checked manually (O21).

## Existing users

- **MCP tool contract:** additive only.
  - Tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`) are added.
  - Tool descriptions gain a short core of the usage rules.
  - An optional `?profile=` query and an `MM-Client-Profile` header are accepted.
  - Names, parameters and results stay unchanged. The `default` profile keeps today's behaviour, and the conformance suite proves it.
- **Usage rules:** `src/memory_manager/mcp/instructions.py` is generated from `docs/memory-guide.md`. Its texts stay identical until someone edits the guide.
- **Static tokens:** existing tokens become `kind = service` and keep working ([ADR-0012](../adr/0012-personal-tokens.md)).
- **CLI:** `doctor` without `--client` keeps its vault health check. `connect`, `instructions` and `agent` are new subcommands.
- **Single-user mode:** everything in M12–M14 works with the Git backend and a static token, without Enterprise.

## Dependencies on F-01 (Enterprise Scale)

F-01 is in progress. M7 is done except WP-21, and M8–M11 are not started. F-02 reuses its pieces and duplicates none of them:

| F-02 needs | Comes from | State on 2026-10-07 | Blocks in F-02 |
|---|---|---|---|
| namespaces, principal, permission matrix, RLS | WP-19 (#100, #101, #115, #116, #119) | done | nothing |
| auth facade with Entra login (OAuth + DCR towards clients) | WP-22 (M8) | not started | enterprise variants of Open WebUI and web-client tests (WP-46, WP-56, WP-57) |
| static tokens with owner, mandatory expiry in enterprise mode | #115 done; WP-24 (M8) | partly | personal tokens in enterprise mode (WP-39) |
| `/account` page | WP-25 (M9) | not started | self-service tokens (WP-39, #135), agent approval UI (WP-52) |
| quotas per token or namespace | WP-27 (M9) | not started | agent quotas (WP-52) |
| threat model | WP-33 (M11) | not started | security review (WP-60) |
| release v0.2.0 | WP-34 (M11) | not started | v1.0.0-rc (WP-62) |

Blocked items carry `⛔ blocked by WP-NN` in `docs/TASKS.md`. They become issue references once F-01 cuts those milestones into issues.

## Architecture delta

| Component | Delta | ADR |
|---|---|---|
| `src/memory_manager/compat/` | new: profile data (limits, instruction delivery mode, result budget), profile selection per request, schema linter | [ADR-0010](../adr/0010-client-compatibility-profiles.md) |
| `src/memory_manager/mcp/server.py` | extended: tool annotations; description core rules; profile-aware `instructions` | ADR-0010 |
| `docs/memory-guide.md` + `src/memory_manager/guide/` | new: single source of the usage rules; generator for instructions, the `memory_guide` prompt, a short form and client instruction files | — (implementation detail, see Design) |
| `src/memory_manager/auth/`, migrations | extended: token `kind`, `created_by`, `last_used_at`; owner-bounded rights; later agent policy and `pending_writes` | [ADR-0012](../adr/0012-personal-tokens.md), [ADR-0013](../adr/0013-agent-identity.md) |
| `src/memory_manager/auth/` + `cli.py` | extended: operator-registered confidential OAuth clients (`oauth-client create|list|revoke`) | [ADR-0015](../adr/0015-preregistered-oauth-clients.md) |
| `/account` | extended: token section whenever the embedded AS runs; agent approvals | ADR-0012, ADR-0013 |
| `src/memory_manager/clients/` + `cli.py` | new: `connect <client>` (config merge with backup, diff, idempotence), `doctor --client`, `instructions generate`, `agent …` | — |
| `src/memory_manager/importers/` | extended: `import openwebui`, `import hermes`, `import openclaw` | — |
| `integrations/<client>/` | new per client: config examples, generated instruction file, README with tested version; Open WebUI `filter_memory.py`, `tool_memory.py`, `system_prompt.md` | [ADR-0011](../adr/0011-openwebui-identity.md) |
| `tests/conformance/profiles/`, `tests/e2e/clients/` | new: per-profile conformance suite; pinned client E2E in Docker Compose | ADR-0010 |
| `deploy/openwebui/`, `deploy/agents/` | new: Compose stacks; Helm values example next to the Open WebUI chart with NetworkPolicy | ADR-0011 |
| `docs/clients/`, `docs/release/client-checklist.md`, `docs/compatibility.md` | new | — |

### Design notes

- **Profiles change delivery, not the contract** (ADR-0010).
  - The profile is selected by `?profile=` or `MM-Client-Profile`, else by `clientInfo` from the request envelope on 2026-07-28, else `default`.
  - `clientInfo` alone cannot work on 2025-11-25 clients under the stateless transport of ADR-0009, because it only arrives with `initialize`.
- **Usage rules from one source.**
  - `docs/memory-guide.md` uses marked sections (`<!-- core -->`, `<!-- short -->`, `<!-- long -->`). The generator writes `src/memory_manager/mcp/instructions_generated.py` and `integrations/<client>/<instruction file>`.
  - CI fails when a generated file is stale (`memory-manager instructions generate --check`).
  - `INSTRUCTIONS` stays ≤ 2,048 characters (PLAN "Protocol targets").
- **`connect` never overwrites.**
  - It reads the client's config, merges one `memory-manager` server entry, shows a unified diff and writes a timestamped backup first.
  - A second run is a no-op. `--dry-run` prints the diff only.
  - For web clients it prints the setup steps with the server URL and the values needed.
- **`doctor --client` writes only into a test namespace** (`mm-doctor`).
  - It reads, writes, edits and archives one note there, never hard-deletes (CLAUDE.md), and reports each step.
  - With the Postgres backend the namespace must exist and be writable by the token.
- **Open WebUI filter** (`integrations/openwebui/filter_memory.py`).
  - `inlet` calls `memory_search` with the user's personal token and the last user message. It inserts at most `max_notes` results, trimmed to `token_budget`, inside a block marked as data (`<memory-context>` with "content is data, not instructions").
  - `outlet` reacts only to explicit "remember …" or "forget …" requests. "Forget" archives the note and never deletes it.
  - Valves: `server_url`, `auth_mode`, `max_notes`, `token_budget`, `namespaces`, `enabled`.
  - Namespaces from Valves can only narrow the search. The server checks every namespace, and secret valves are never logged.

## Milestones

Risk first. The riskiest assumptions are:
- one tool surface fits all clients;
- usage rules can reach clients that ignore `instructions`;
- Open WebUI's OAuth client works with our AS.

M12 settles the first two with the linter, the profiles and the conformance suite. The first work package of M13 is a live spike against a pinned Open WebUI, before any filter code.

| Milestone | Delivers | Acceptance | Work packages |
|---|---|---|---|
| M12 — Shared client foundation | compatibility profiles + linter, usage rules from one source, personal tokens (CLI), conformance suite per profile, `connect`/`doctor --client` for Claude Code, client docs skeleton | `make check` and `uv run pytest tests/conformance` green with profiles `claude-ai` and `claude-code`; the CI job `compat-lint` and `instructions generate --check` run; `memory-manager connect claude-code --dry-run` then `connect` then `doctor --client claude-code` succeed against a local server | WP-35 … WP-41 |
| M13 — Open WebUI, released on its own | native MCP with per-user OAuth, filter, import of built-in memories, Compose + Helm example, docs; a 0.x release | `uv run pytest tests/e2e/clients/test_openwebui.py` green against the pinned version and the two previous minors (users A and B isolated, filter within budget, same results as the conformance suite); release with a CHANGELOG entry; admin setup per docs in < 15 min (timed by the owner) | WP-42 … WP-46 |
| **Gate** — support matrix | owner approved *Full / Partial / Not possible* per client on 2026-10-08 | matrix below; copied to `docs/clients/README.md` by #129 (#159) | WP-35 |
| M14 — IDE and CLI clients | profiles, `integrations/`, `connect`/`doctor` for Codex, Gemini CLI / Code Assist, Cursor, Copilot, Antigravity; local headless harness; org rollout docs | conformance suite green for every approved profile; checklist entries filled for Claude Code, Codex CLI and Gemini CLI (headless harness) and for the GUI clients | WP-47 … WP-51 |
| M15 — Autonomous agent runtimes | agent identity and write guard, Hermes and OpenClaw at the approved tier, importers | conformance + E2E for both runtimes with pinned versions; the injection test proves that a third party's "remember …" never reaches the owner's namespace | WP-52 … WP-55 |
| M16 — Web clients | ChatGPT and Gemini Enterprise per the approved matrix; manual client checklist | checklist in `docs/release/client-checklist.md` filled for every GUI/web client, with result and version | WP-56 … WP-58 |
| M17 — v1.0.0-rc | stable API + compatibility policy, upgrade guide, security review, docs website, release | `v1.0.0-rc.1` tag with signed image and chart; no open High/Critical findings | WP-59 … WP-62 |

M12 and M13 do not wait for the support-matrix gate. M14–M16 do.

### Work packages and planned tasks

All milestones are cut into GitHub issues (owner's request, 2026-10-08), not only the first. Issues of M14–M17 are written against code that does not exist yet. When a milestone starts, its issues are re-read against the actual code and the re-verified research note, and corrected on GitHub and in `docs/TASKS.md` before work begins.

**M12**
- **WP-35** `wp/35-client-decisions`: research notes and ADR-0010 … 0013 accepted, F-02 plan (#126); support-matrix gate (#159).
- **WP-36** `wp/36-memory-guide`: `docs/memory-guide.md` as single source with a byte-identical generated `instructions` (#127), generator for short form and client instruction files with a CI staleness check (#128).
- **WP-37** `wp/37-client-docs`: `docs/clients/` template, support-matrix skeleton, pages for claude.ai and Claude Code (#129).
- **WP-38** `wp/38-compat-profiles`: profile model with `default`/`claude-ai`/`claude-code` (#130), selection per request (#131), tool annotations and core rules in descriptions (#132), schema linter + CI job (#133).
- **WP-39** `wp/39-personal-tokens`: token kind, owner-bounded rights, CLI (#134); self-service on `/account` (#135) ⛔ WP-25.
- **WP-40** `wp/40-conformance-profiles`: per-profile conformance harness incl. error cases (#136).
- **WP-41** `wp/41-connect-doctor`: `connect claude-code` / `connect claude-ai` (#137), `doctor --client` (#138).

**M13 — Open WebUI** (blocked by ADR-0011)
- **WP-42** `wp/42-openwebui-auth`: spike against a pinned Open WebUI (v0.11.4) in Compose: DCR with `client_secret_post`, redirect shape, where `UserValves` are stored, whether filters see the MCP OAuth token; fixes in the AS; `deploy/openwebui/docker-compose.yml` (Open WebUI, memory-manager, Postgres, Ollama); E2E with users A/B over native MCP. Issues: #141, #142, #143, #144.
- **WP-43** `wp/43-openwebui-profile`: profile `openwebui` (no `instructions`, `<server_id>_` prefix budget); conformance over Open WebUI's MCP path. Issues: #145, #146.
- **WP-44** `wp/44-openwebui-filter`: `filter_memory.py` (inlet, budget, data marker, Valves), explicit-only `outlet`, `tool_memory.py` fallback, `system_prompt.md`; unit tests with fake Open WebUI objects; E2E filter test; small-model check (7–8B via Ollama) as a nightly job. Issues: #147, #148, #149, #150, #151.
- **WP-45** `wp/45-openwebui-import`: `memory-manager import openwebui` from `/api/v1/memories` or an export file; docs for disabling or fencing the built-in memory (`ENABLE_MEMORIES`, per-group permission). Issues: #152, #153.
- **WP-46** `wp/46-openwebui-release`: Helm values example next to the official chart with NetworkPolicy, `docs/clients/openwebui.md`, `integrations/openwebui/README.md` with version matrix, CI matrix over the pinned and two previous minors, enterprise variant via the facade ⛔ WP-22, release. Issues: #154, #155, #156, #157, #158.

**M14 — IDE and CLI clients** (blocked by the support-matrix gate)
- **WP-47** `wp/47-headless-cli`: local headless harness (self-tested in CI with a fake CLI, real CLIs run manually per O21); Claude Code `-p` and Codex CLI `exec`; profile, `integrations/codex/` (`config.toml`, `AGENTS.md`), `connect`/`doctor codex`. Issues: #161, #162, #163, #164.
- **WP-48** `wp/48-gemini-cli`: profile (OpenAPI schema subset), `integrations/gemini/` (`settings.json`, `GEMINI.md`), `connect`/`doctor gemini`, headless `gemini -p` via the harness (manual); Code Assist notes. Issues: #165, #166, #167, #168.
- **WP-49** `wp/49-cursor`: profile, `integrations/cursor/` (`mcp.json` global/project, rules file), `connect`/`doctor cursor`, Teams rollout docs. Issues: #169, #170, #171.
- **WP-50** `wp/50-copilot`: profile, `integrations/copilot/` (VS Code `mcp.json`, JetBrains, coding-agent config, `copilot-instructions.md`), `connect`/`doctor copilot`, Business/Enterprise MCP policy docs. Issues: #172, #173, #174.
- **WP-51** `wp/51-antigravity`: profile, `integrations/antigravity/`, `connect`/`doctor antigravity`, manual checklist entry. Issues: #175.

**M15 — Autonomous agent runtimes** (blocked by the support-matrix gate and ADR-0013)
- **WP-52** `wp/52-agent-identity`: agent tokens, namespace kind `agent`, delegation grants, write policies incl. `pending_writes` + approval CLI, `MM-Agent-Channel` in the audit; approval on `/account` ⛔ WP-25; quotas ⛔ WP-27; injection test. Issues: #176, #177, #178, #179, #180, #181, #182, #183.
- **WP-53** `wp/53-hermes`: tier 1 (MCP with tool filter and `trust: untrusted`), skill with the usage rules, `connect`/`doctor hermes`, `import hermes` (`MEMORY.md`/`USER.md`), Compose example, E2E with a pinned version. Issues: #184, #185, #186.
- **WP-54** `wp/54-openclaw`: tier 1 (MCP with `toolFilter`, per-requester OAuth), skill, `connect`/`doctor openclaw`, `import openclaw`, Compose example, E2E with a pinned version. Issues: #187, #188, #189.
- **WP-55** `wp/55-agent-native`: decision recorded as [ADR-0014](../adr/0014-agent-integration-tier.md) (tier 1 only in v1, #190); the Hermes provider (#191) and the OpenClaw plugin (#192) are closed as not planned and revisited after 1.0.

**M16 — Web clients** (blocked by the support-matrix gate)
- **WP-56** `wp/56-chatgpt`: profile (annotations drive write approval), `docs/clients/chatgpt.md`, `connect chatgpt` instructions, Enterprise/Business rollout docs, checklist entry. Issues: #194, #195.
- **WP-57** `wp/57-gemini-enterprise`: pre-registered confidential client support in the AS ⛔ O18, profile, docs incl. "consumer Gemini app: not possible", checklist entry. Issues: #196, #197, #198.
- **WP-58** `wp/58-client-checklist`: `docs/release/client-checklist.md` for Cursor, Copilot, Antigravity, ChatGPT, Gemini Enterprise, Open WebUI; filled per release. Issues: #160, #193.

**M17 — v1.0.0-rc**
- **WP-59** `wp/59-stable-api`: `docs/compatibility.md` (SemVer, deprecation policy); tool contract, config format, note format and CLI marked stable; upgrade guide from the last 0.x; migration tests. Issues: #199, #200, #201, #202.
- **WP-60** `wp/60-security-rc`: threat model update (Open WebUI identity, personal tokens, agents with third-party input) ⛔ WP-33; review; no open High/Critical findings. Issues: #203, #204.
- **WP-61** `wp/61-docs-site`: Markdown docs on GitHub (O20): `docs/README.md` as index with quickstart, client pages, support matrix, operations, enterprise; README lists clients as text only. Issues: #205, #206, #207.
- **WP-62** `wp/62-release-rc`: all features done in TASKS or deferred with a reason; release notes; signed image, chart, tag `v1.0.0-rc.1` ⛔ WP-34. Issues: #208, #209.

## Support matrix (approved 2026-10-08)

Approved by the owner on 2026-10-08 (O19), based on the desk research of 2026-10-07 in [`docs/research/clients/`](../research/clients/). Each level is re-verified with a tested version when the client's work package starts; a client that turns out weaker is downgraded in `docs/clients/README.md` and reported, never promised.

| Client | MCP transport | Auth that fits our server | Server `instructions` used | Proposed level | Main caveat |
|---|---|---|---|---|---|
| claude.ai | Streamable HTTP | OAuth (CIMD/DCR) | yes | Full | — (verified 2026-10-07) |
| Claude Code | stdio, HTTP | OAuth (CIMD), static token | yes (≤ 2,048 chars) | Full | — (verified) |
| Open WebUI ≥ 0.6.31 | Streamable HTTP, admin-configured | per-user OAuth (DCR, `client_secret_post`), no CIMD | no | Full (M13) | tool must be enabled per chat; DCR request shape unverified live |
| Codex CLI / IDE | stdio, Streamable HTTP | CIMD, DCR, bearer from env | no → `AGENTS.md` | Full (cloud agent unverified) | reported MCP call cancellation under `codex exec` |
| Gemini CLI | stdio, SSE, HTTP | DCR, headers; no CIMD; strict `iss` check | yes | Full | names ≤ 63 chars, `additionalProperties` stripped; free tier status unclear |
| Gemini Code Assist | via Gemini CLI agent mode | as Gemini CLI | partly | Partial | IDE agent mode only |
| GitHub Copilot (VS Code, CLI) | stdio, HTTP, SSE | CIMD, DCR, headers | yes | Full | 128 tools per request |
| Copilot coding agent, JetBrains | HTTP | headers only (no OAuth) in the cloud agent | no | Partial | needs a static token; firewall reachability unknown |
| Cursor | stdio, HTTP, SSE | DCR, headers; no CIMD | no → rules file | Partial | reported: static `Authorization` header dropped when the server advertises OAuth |
| Google Antigravity | stdio, HTTP | DCR, CIMD, headers | unverified | Partial | sources mostly secondary |
| Hermes Agent | stdio, HTTP, SSE | headers, OAuth (DCR) | unverified → skill | Full, tier 1 | third-party messages can trigger writes (ADR-0013) |
| OpenClaw | stdio, HTTP, SSE | headers, per-requester OAuth | unverified → skill | Full, tier 1 | tier 2 is TypeScript (O17) |
| ChatGPT | remote MCP apps | OAuth (CIMD, DCR) | unverified | Partial | write actions only on Business/Enterprise/Edu; mobile unclear; secondary sources |
| Gemini Enterprise | Streamable HTTP, admin data store (preview) | OAuth with pre-registered client only | unverified | Partial ⛔ O18 | no DCR; ≤ 100 actions |
| Gemini app (consumer) | custom MCP apps, US only | DCR, reported failing | unverified | Not possible (reliably) | revisit at M16 |

## Decisions

All open decisions were taken by the owner on 2026-10-08.

| # | Question | Decision | Record |
|---|---|---|---|
| O13 | Open WebUI identity | per-user OAuth for tools, personal token for the filter; trusted headers possibly later via own ADR; Python for `integrations/openwebui/` confirmed | [ADR-0011](../adr/0011-openwebui-identity.md) |
| O14 | Client compatibility and profile selection | one strict tool surface, profiles change delivery only, explicit selection | [ADR-0010](../adr/0010-client-compatibility-profiles.md) |
| O15 | Personal tokens | extend static tokens; `/account` token section whenever the embedded AS runs | [ADR-0012](../adr/0012-personal-tokens.md) |
| O16 | Agent identity and write guard | agent namespace kind, server-side policy, approval queue, explicit delegation | [ADR-0013](../adr/0013-agent-identity.md) |
| O17 | Native agent integration (tier 2) | tier 1 (MCP) only in v1; #191, #192 closed as not planned | [ADR-0014](../adr/0014-agent-integration-tier.md) |
| O18 | Clients without DCR/CIMD (Gemini Enterprise) | operator-registered confidential OAuth clients in our AS | [ADR-0015](../adr/0015-preregistered-oauth-clients.md) |
| O19 | Support matrix | draft approved as is | this file, `docs/clients/README.md` (#129) |
| O20 | Docs website tooling | plain Markdown on GitHub, no site generator | PLAN technology decisions |
| O21 | Model API keys for CLI E2E in CI | none; CLI clients are verified manually with the local harness and the checklist; agent runtimes and Open WebUI run in CI against the scripted stub model | PLAN technology decisions |

## Spikes

- **WP-42 (first issue)** is the Open WebUI spike. It answers the unverified points of [`docs/research/clients/openwebui.md`](../research/clients/openwebui.md) against a running pinned instance before the filter is written.
- The support-matrix gate rests on desk research. Several notes (ChatGPT, Gemini) could not reach the vendor docs through the network proxy and are marked as such. Each M14–M16 work package therefore starts by re-verifying its client's note with a tested version.
