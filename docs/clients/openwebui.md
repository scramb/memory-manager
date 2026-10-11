# Connecting Open WebUI to memory-manager

Open WebUI speaks MCP (Streamable HTTP only) as an admin-configured tool server, and ships its
own built-in memory feature that competes with ours unless an admin disables or fences it. This
page covers that fencing (#153); the full admin setup walkthrough, the filter path for setups
without native MCP, and `memory-manager import openwebui` itself are tracked separately (#155,
#149, #152).

Checked against Open WebUI **0.11.4** on **2026-10-11**.

## Tested version

- Image `ghcr.io/open-webui/open-webui:0.11.4`, index digest
  `sha256:9591b13f13843c7721c2b8eaf7382846c81b3ffe126526d1888d1fed50c6a33f`.
- memory-manager commit `f455d66` (`import openwebui`, #152), live-verified on 2026-10-11 against
  the pinned stack `deploy/openwebui/docker-compose.yml` (WP-42, `make openwebui-up`).

## Setup

Follows in #155.

## Auth variants

Follows in #155.

## Instructions file

Follows in #155.

## Known limits

Follows in #155.

## Open WebUI's built-in memory

Open WebUI stores its own per-user memory snippets in its own database and, by default, both
injects them into the system message and offers a model eight native memory tools (`add_memory`,
`search_memories`, …) — see
[`../research/clients/openwebui.md`](../research/clients/openwebui.md) "Built-in memory" for the
full shape. Running memory-manager next to Open WebUI without turning this off means a user's
facts can live in two places, and a model can see two competing `<memory_context>` blocks (or a
`<memory-context>` block from our own [Open WebUI filter](../features/F-02-client-integrations.md)
next to Open WebUI's own `<memory_context>` plus eight extra tools) in the same turn.

**Recommendation:** import any existing built-in memories (`memory-manager import openwebui`,
below), then switch the built-in feature off globally with `ENABLE_MEMORIES=false`. Do this before
onboarding a group of users to memory-manager's own Open WebUI integration (native MCP, WP-43, or
the filter, WP-44) — the two were never meant to run side by side.

### Settings

| Env var | Admin UI path | Effective-value check | Effect |
|---|---|---|---|
| `ENABLE_MEMORIES` (default `true`) | Admin > Settings > General > Features > Memories | `GET /api/v1/auths/admin/config` field `ENABLE_MEMORIES`; public mirror `GET /api/config` → `features.enable_memories` | Off: the whole `/api/v1/memories/*` API 404s for every role including admin, no `<memory_context>` injection, the eight native tools disappear. The only switch that is a true "off", for everyone. |
| `USER_PERMISSIONS_FEATURES_MEMORIES` (default `true`) | Admin > Settings > Users > Permissions > Features > Memories, and per group under Admin > Groups | `GET /api/v1/users/default/permissions` field `features.memories`; `GET /api/v1/groups/id/<id>` for a group's own `permissions.features.memories` | Off (default): a non-admin's `GET /api/v1/memories/` is `403`, no injection, no native tools for that user. Admins are always exempt. See "Group fencing" below — this is **not** a full disable, only a per-role/group narrowing. |
| Model `meta.capabilities.memory` (default `true`) | Workspace > Models > *(model)* > Capabilities > Memory | `GET /api/v1/models/model?id=<id>` field `info.meta.capabilities.memory` | Off: both injection and the native tools are off for that model, for every user. |
| Model `meta.builtinTools.memory` (default `true`) | Workspace > Models > *(model)* > Builtin Tools > Memory | `GET /api/v1/models/model?id=<id>` field `info.meta.builtinTools.memory` | Off: only the native tools disappear for that model; injection is unaffected. A tool switch, not a disable. |
| `ENABLE_MEMORY_SYSTEM_CONTEXT` (default `true`) | Admin > Settings > General > Features > Memory System Context | `GET /api/v1/auths/admin/config` field `ENABLE_MEMORY_SYSTEM_CONTEXT` | Off: no `<memory_context>` injection; the eight native tools stay available. |
| `ENABLE_MEMORY_BACKGROUND_REVIEW` (default `false`) | none — no admin UI switch exists | `GET /api/v1/configs/namespace/memories` field `memories.background_review.enable`; writable only via `POST /api/v1/configs/import` with `{"config": {"memories.background_review.enable": false}}` | Off (default): the chat model never runs its periodic review/edit pass over a user's memories. |

### Group fencing

`USER_PERMISSIONS_FEATURES_MEMORIES` and a group's own `features.memories` combine by **union**,
not override: a user has the permission if *any* group they are in grants it, or the site-wide
default does — a group set to `false` can never take the permission away from a user whose default
(or another group) already grants it. Practically: fencing the built-in memory down to a handful
of users means setting the **default to `false`** and adding only those users to a group whose own
`features.memories` is `true` — the reverse (default `true`, deny one group) does not fence
anyone. New groups created in the Open WebUI admin UI start with `memories: true` by default, so an
admin building a fence must explicitly set it to `false` on every group that should not have
access. Admins bypass this permission check entirely and always see the built-in memory,
independent of any of the above.

### Migrating off the built-in memory

`memory-manager import openwebui` (#152) reads a user's existing built-in memories and imports them
as notes before the built-in feature is switched off — **import first, then disable**, since
`GET /api/v1/memories/` (the only read path; there is no export button or endpoint) starts
returning `404` the moment `ENABLE_MEMORIES` is `false`.

```sh
# live fetch, with the user's own Open WebUI API key:
OPENWEBUI_API_KEY=<key> memory-manager import openwebui \
  --url http://localhost:3000 --namespace me --apply

# or from a saved copy of GET /api/v1/memories/ (no API key needed —
# save it from the signed-in browser, or via session-cookie auth):
memory-manager import openwebui --file export.json --namespace me --apply
```

`--url` needs `ENABLE_API_KEYS=true` (default `false`) and, for a non-admin caller, the
`features.api_keys` permission; with `ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS` on, `/api/v1/memories`
must be in the allowed-endpoints list. `--file` has neither requirement — it is the simpler path
for a one-off migration. Dry run (no `--apply`) is the default either way.

### Two memory-context blocks at once

With memory-manager's Open WebUI filter (WP-44, #147) enabled *and* `ENABLE_MEMORY_SYSTEM_CONTEXT`
still on, a model sees **two** data blocks in the same system message: our filter's own
`<memory-context>` and Open WebUI's `<memory_context>`, plus up to eight extra native memory
tools Open WebUI adds on top of whatever the filter or native MCP already expose. Nothing breaks,
but it wastes context budget and gives the model two, possibly conflicting, sources of "memory" —
another reason to switch `ENABLE_MEMORIES` off once memory-manager's own integration is in place.

### Version note

Versions before 0.11.4 leaked `<memory_context>` injection and native tool availability behind
`ENABLE_MEMORIES=false` in some code paths (fixed by CHANGELOG entries #30228/#30309) — the
settings table above describes 0.11.4's corrected behaviour, confirmed live (below). An older
deployment should not be trusted to fence the built-in memory the same way without its own check.

### How this was verified

Against the pinned stack (`make openwebui-up`, WP-42, admin + one plain user created via the
auth API, a throwaway admin-installed filter logging only booleans to stderr — never note
content or credentials):

| # | Check | Observed |
|---|---|---|
| C1 | `GET /api/v1/configs/namespace/memories`, `GET /api/v1/auths/admin/config`, `GET /api/v1/users/default/permissions` on a fresh instance | `memories.enable=true`, `ENABLE_MEMORIES=true`, `ENABLE_MEMORY_SYSTEM_CONTEXT=true`, `features.memories=true` — matches the documented defaults |
| C2 | User `POST /api/v1/memories/add` (`type="user"`) | `200` |
| C3 | Baseline probe round (`POST /api/chat/completions`, `features.memory=true`, `session_id` set, no `chat_id`) | `<memory_context>` present, 8/8 native memory tools offered |
| C4 | `ENABLE_MEMORY_SYSTEM_CONTEXT=false`, same probe | no `<memory_context>`, still 8/8 tools (tools and injection are independent switches) |
| C5 | Model `meta.builtinTools.memory=false` (`POST /api/v1/models/model/update`), same probe | `<memory_context>` still present, 0/8 tools (confirms `builtinTools.memory` is a tool-only switch) |
| C6 | Default `features.memories=false`, same probe | user `GET /api/v1/memories/` → `403`; admin → `200` (admin exempt); probe: no context, 0/8 tools |
| C7 | Group with `features.memories=true` added on top of a `false` default, then the reverse (`true` default, group `false`) | first case: `200` (group grants on top of a denying default); second case: still `200` (a denying group cannot revoke a granting default) — confirms union semantics |
| C8 | `ENABLE_MEMORIES=false` | user and admin `GET /api/v1/memories/` → `404`; `GET /api/config` → `features.enable_memories=false`; probe: no context, 0/8 tools |
| C9 | `import openwebui --url` with a live API key: dry run lists the C2 memory; after `ENABLE_MEMORIES=false`, the same call exits `2` naming the disabled-memories cause; `import openwebui --file` against a saved `GET /api/v1/memories/` response: same dry-run plan | all as expected |
| C10 | Fresh container + volume with `ENABLE_MEMORIES=false`/`USER_PERMISSIONS_FEATURES_MEMORIES=false` at first start, then recreated on the **same** volume with `ENABLE_MEMORIES=true`, then again with `ENABLE_PERSISTENT_CONFIG=false` added — re-verified on 2026-10-11 by recreating the container on the same volume for each stage and re-querying `GET /api/v1/configs/namespace/memories` and `GET /api/v1/users/default/permissions` after each restart | first start: `memories.enable=False`, `features.memories=False`, matching the env; same volume, new env (`ENABLE_MEMORIES=true`, `USER_PERMISSIONS_FEATURES_MEMORIES` unset): both stay `False` (persisted DB value wins — `ENABLE_PERSISTENT_CONFIG` only governs *first*-start seeding); with `ENABLE_PERSISTENT_CONFIG=false` added: `memories.enable=True`, `features.memories=True` (persistence bypassed, the env default is read directly) |

## Org rollout

Follows in #155.

## Troubleshooting

Follows in #155.
