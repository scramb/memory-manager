# Compatibility

What memory-manager 1.x promises to anyone calling it — an MCP client, an operator's automation, a human running the CLI — and what it does not. The decision behind this document is [ADR-0017](./adr/0017-api-compatibility-policy.md); read that for the rejected alternatives.

## Scope and versioning

This promise starts at **1.0.0**. Nothing in 0.x, and no `-rc.N` pre-release, carries any compatibility guarantee: every 0.x release has changed tool parameters, config and CLI flags freely, and will keep doing so until 1.0.0 ships.

From 1.0.0 the server follows [SemVer 2.0.0](https://semver.org/): `MAJOR.MINOR.PATCH`. A version bump is driven by the Conventional Commit types merged since the last release — see [`./releasing.md`](./releasing.md) for how release-please turns those commits into a tag, image and chart.

- **Patch**: bug fixes, internal refactors, documentation.
- **Minor**: additive changes to a Stable surface (below), and anything Operational.
- **Major**: a breaking change to a Stable surface, or a Stable surface's deprecation period running out.

**Security exception:** a security fix may break a Stable surface in a minor or even a patch release. It is announced in the release notes and, where applicable, a security advisory — this is the one case where shipping a fix takes priority over the SemVer promise below.

## The public surface, in three tiers

### Stable — SemVer and a deprecation period apply

| Surface | Covers | Source |
|---|---|---|
| MCP tools | Names, parameters, result fields, annotations: `memory_index`, `memory_read`, `memory_search`, `memory_write`, `memory_edit`, `memory_supersede`, `memory_promote`, `memory_archive` | [`../src/memory_manager/mcp/server.py`](../src/memory_manager/mcp/server.py), [reference](./reference/tools.md) |
| Structured error kinds | The `error` field of a tool result's structured error (`isError` content channel 1): `VersionConflict`, `WriteConflict`, `NotFound`, `InvalidNote`, `SecretRejected`, `EditMismatch`, `BlocklistRejected`, `WriteFailed`, `PathRejected`, `NoteFormatError`, `NoteInvalid` | [`../src/memory_manager/storage/base.py`](../src/memory_manager/storage/base.py), [`../src/memory_manager/mcp/errors.py`](../src/memory_manager/mcp/errors.py), [reference](./reference/tools.md) |
| `memory_guide` prompt | The prompt's name (not its wording) | [`../src/memory_manager/mcp/server.py`](../src/memory_manager/mcp/server.py), [reference](./reference/tools.md) |
| Compatibility profiles | Profile names and how one is selected: the `?profile=` query parameter, the `MM-Client-Profile` header, `serve --stdio --profile` | [ADR-0010](./adr/0010-client-compatibility-profiles.md) |
| OAuth scopes | `memory:read`, `memory:write` | [ADR-0004](./adr/0004-auth-model.md) |
| HTTP paths | `/mcp` (configurable via `MCP_PATH`), `/healthz`, `/readyz`, `/metrics`, `/hooks/vault`, `/.well-known/oauth-protected-resource` (incl. its path-suffixed variant), `/.well-known/oauth-authorization-server`, `/register`, `/token`, `/authorize`, `/login`, `/oidc/callback`, `/account` (`/account/login`, `/account/logout`) | [`../src/memory_manager/http.py`](../src/memory_manager/http.py) |
| Config | Env vars with their defaults, and the config file formats they point at: `BLOCKLIST_FILE` (TOML), `ERASURE_LOG_REPLAY_FILE`, `LOGIN_NAMESPACE_MAP` | [`../src/memory_manager/config.py`](../src/memory_manager/config.py), [reference](./reference/config.md) |
| Note format | Frontmatter fields, path layout, `export` output | [ADR-0005](./adr/0005-note-format.md), [reference](./reference/note-format.md) |
| CLI | Commands and flags | [`../src/memory_manager/cli.py`](../src/memory_manager/cli.py), [reference](./reference/cli.md) |
| Exit codes | The distinction between `0` (success) and non-zero (failure) — **not** which specific non-zero value a given failure returns; those are inconsistent across commands today | [`../src/memory_manager/cli.py`](../src/memory_manager/cli.py), [reference](./reference/cli.md) |
| `doctor --client --json` | The field names of the JSON report | [`../src/memory_manager/cli.py`](../src/memory_manager/cli.py) |
| Helm values | `values.schema.json` | [`../charts/memory-manager/values.schema.json`](../charts/memory-manager/values.schema.json) |
| Image and chart tags | `ghcr.io/scramb/memory-manager:X.Y.Z`/`:X.Y`/`:latest` (stable releases only); the chart `oci://ghcr.io/scramb/charts/memory-manager` in lockstep with the app version | [`./releasing.md`](./releasing.md) |

**Result channel:** a tool result carries two channels. `structuredContent` always matches the tool's `outputSchema` (a list result renders as `{"result": [...]}`) and is the Stable result contract covered by "MCP tools" above. The text `content` channel is a rendering the MCP SDK builds from `structuredContent`, not a second contract: it splits a list result into one `TextContent` block per element, so `memory_read` returns one bare JSON object for a single note but several blocks for several, while `memory_search` and the write tools always render one block holding the whole object. That rendering is Operational (below), not Stable — its shape may change between releases without notice.

A breaking change to the Helm values is, by definition, a product major: the chart version and the app version move together (`Chart.yaml`), so there is no such thing as a Helm-only breaking release.

The `message` text of a structured error, and the plain-text error raised through the second `ToolError` channel (invalid arguments, scope/namespace checks, quota errors) are explicitly **not** covered — only the `error` kinds listed above are. `message` wording changes freely.

### Operational — announced in release notes, no deprecation period

Metric names and labels (`mm_*`), dashboards and alerts, audit-export fields, the log format, the `/account` HTML, human-readable CLI output (as opposed to `--json` output), and the text `content` channel of a tool result (as opposed to `structuredContent`, Stable above). These change when they need to for operational reasons; a release that changes one says so in its release notes, but there is no deprecation cycle first.

### Internal — no compatibility promise

The Postgres schema and its migrations, the Python API (anything not exposed as a tool, HTTP path, env var, note field or CLI flag), tool/instruction description wording, error `message` text, token string formats, and the Kustomize base's internals.

## Change rules

For anything in the Stable tier:

**Additive — safe in a minor:**
- a new tool
- a new optional tool parameter
- a new result field
- a new structured error kind
- a new enum value in a result field (e.g. `namespace_kind`, `mode`)
- a new compatibility profile
- a new env var or CLI flag whose absence keeps today's default behaviour

**Breaking — needs a major (after the deprecation period in [ADR-0017](./adr/0017-api-compatibility-policy.md)):**
- removing or renaming a tool, parameter, result field, error kind, HTTP path, env var or CLI flag
- turning an optional parameter required
- narrowing the allowed values of a parameter or enum
- changing the type of a field
- changing a default's behaviour
- making an annotation more permissive (e.g. `readOnlyHint` `true` → `false`)
- dropping support for an MCP protocol revision the server currently speaks

**Client duty:** a client that only reads the fields and values it knows about keeps working across minors. Concretely: tolerate unknown result fields, unknown structured error kinds, and unknown enum values — do not fail on something new and unrecognised; either handle it generically or ignore it. Prefer `structuredContent` over the text `content` channel: it always matches the tool's documented `outputSchema`, while the text rendering is Operational and its shape may change without notice.

**Note format across a rollback:** a newer minor may add an optional frontmatter field. That is additive for servers at or after that minor, but an older server rejects unknown frontmatter keys outright ([ADR-0005](./adr/0005-note-format.md)). A rollback to an older minor after such a field has been written is therefore **not guaranteed** to work — the older server will refuse notes carrying the new field.

## Deprecation

A Stable surface may be removed only in the next major release, and only once it has shipped as deprecated for at least one minor release **and** 90 days — whichever is later ([ADR-0017](./adr/0017-api-compatibility-policy.md)). A deprecation is announced in this document's register below, in the GitHub release notes of the release that introduces it, and through runtime signals: the affected tool's description is prefixed `Deprecated:`, the server logs one warning per process when the deprecated surface is actually used, the CLI prints a warning to stderr, and a deprecated env var produces a startup warning. (The runtime signals are a promise of this document; their implementation is tracked separately from the policy itself.)

### Deprecations

| Surface | Deprecated in | Earliest removal in | Replacement |
|---|---|---|---|
| — | — | — | — |

No surface is currently deprecated.
