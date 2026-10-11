# Upgrading from 0.2.0 to 1.0.0-rc

`v1.0.0-rc` is the stable API release: a tiered compatibility policy
([ADR-0017](../adr/0017-api-compatibility-policy.md)), client compatibility profiles
([ADR-0010](../adr/0010-client-compatibility-profiles.md)), personal tokens and a handful of new
CLI commands. Every one of them is additive - there is no breaking change to the MCP tool
contract, the note format or the CLI between `0.2.0` and this release, and that is proven, not
just stated: [`scripts/upgrade-smoke-1.0.sh`](../../scripts/upgrade-smoke-1.0.sh)
(`make upgrade-smoke-1.0`) runs a real `v0.2.0` image and the current image against a real
Postgres, on both storage backends, and compares their `tools/list` responses over the wire with
[`tests/contract_snapshot.py`](../../tests/contract_snapshot.py)'s own `compare_snapshots` -
failing this guide's own CI job on any `breaking` finding. Each claim below either names the
exact step in that script which proves it, or is marked "(operator-specific)" because it has no
one correct form to test against (`docs/guides/upgrade-0.2.md`'s own pattern).

## No action required

Pulling the new image/chart tag and rolling it out the same way as any other `0.2.x` patch
(`deploy/README.md`'s own rollout step, operator-specific to your own overlay) is the entire
upgrade. Nothing below is required to keep today's behaviour; every item is either new surface an
operator may now opt into, or something that changed for operational reasons only.

## What changes

### Stable and additive (SemVer, ADR-0017)

- **Tool contract**: zero breaking changes. `scripts/upgrade-smoke-1.0.sh`'s own tools/list
  comparison (its last step) is exactly this check, run for real against both images.
- **Compatibility profiles** ([`compat/select.py`](../../src/memory_manager/compat/select.py),
  ADR-0010): which client-specific limits (tool count, result size, ...) a request runs under is
  now selectable instead of fixed. Resolution order: an explicit override - `?profile=` on the
  `/mcp` URL, the `MM-Client-Profile` header, or `serve --stdio --profile` for the stdio
  transport - wins outright; otherwise the connecting client's own `clientInfo.name` is looked up
  against a small built-in table of known clients; neither present falls back to the `default`
  profile, i.e. today's behaviour unchanged. An operator who never sets an override and connects
  with a client this release does not yet recognize sees no change at all; `?profile=default` (or
  no override) restores exactly what `0.2.0` did for every client.
- **Personal tokens** (`token create --kind personal|service`, default `service` - today's only
  kind, unchanged default): a token kind of its own, additive to the existing `--owner`/`--role`
  shape, bounded by the new `PERSONAL_TOKEN_MAX_DAYS` (default 90 days - see "New config" below).
  `/account` gained its own token section for creating and revoking these self-service
  (`docs/guides/upgrade-0.2.md`'s own `/account` already existed; this is a new section on it,
  enterprise mode only).
- **New CLI commands**: `connect` (merge the server into a client's own config - Claude Code,
  claude.ai), `doctor --client <name>` (check that client's live connection instead of a vault),
  `compat lint` (the real tool contract against every registered client profile's documented
  limits), `instructions generate` (render client instruction files from
  `docs/memory-guide.md`), `import openwebui` (import a user's built-in Open WebUI memories).
  None of these touch the server's own wire contract; they are client-side or operator tooling.
- **New config**: `PERSONAL_TOKEN_MAX_DAYS` (default 90, ADR-0012) is the only new `Stable`
  environment variable since `0.2.0` (`git diff v0.2.0..HEAD -- src/memory_manager/config.py`);
  see [`reference/config.md`](../reference/config.md) for the full current list.
  `STATIC_TOKEN_MAX_DAYS` (enterprise static tokens, ADR-0006 §7) already existed in `0.2.0`.

### Operational (announced here, no deprecation period, ADR-0017)

- `mm_tool_calls_total` gained a `profile` label (the resolved compatibility profile for that
  call) alongside its existing `tool`/`outcome` labels - additive to the metric's label set, any
  query grouping by the old labels alone is unaffected.

### Internal (no promise, ADR-0017)

- Tool/instruction description wording changed in several places; nothing a client should parse.
- New migrations shipped since `0.2.0` - deliberately not named or numbered here (ADR-0017:
  "the Postgres schema and its migrations" are Internal); `scripts/upgrade-smoke-1.0.sh`'s own
  `schema_migrations` check (`importlib.resources` over every migration file the image ships,
  `postgres/` included for that backend) proves every one of them actually applies, on both
  backends, without naming which.

## Behaviour change: concurrent refresh with one OAuth token (#310)

If the same OAuth refresh token is presented twice concurrently (a client retrying a timed-out
request, or a genuine replay), only one call ever wins the rotation; the loser gets
`invalid_grant` and the whole token family - every access and refresh token the original grant
ever issued - is revoked (`auth/provider.py`'s `exchange_refresh_token`). `0.2.0` could let both
calls through under the right timing; `1.0` closes that race. A legitimate client that genuinely
double-submits a refresh (rare, but possible after a network retry) now has to sign in again
instead of silently getting two valid pairs - the safer failure mode for a reused credential.
OAuth sessions are not preserved across the upgrade itself (operator-specific: whether a rollout
disrupts in-flight sessions depends on your own deployment shape); this is about ongoing
behaviour once both ends run `1.0`.

## New client label: Open WebUI (`vault/repo.py`)

Every vault commit is authored as the client that made it; `openwebui` joins
`claude-ai`/`claude-code`/`human`/`import` as a recognized author identity - relevant only if you
read `git log` on the vault directly, never to a client over MCP.

## Helm chart

Unchanged since `0.2.0` (`git diff v0.2.0..HEAD -- charts/` is empty). The image tag an operator
points `image.tag`/`appVersion` at still follows the same convention
([`release.yml`](../../.github/workflows/release.yml)): an rc tag such as `v1.0.0-rc.1` only ever
gets the full-version image tag, never the floating `:0.2`/`:latest` tags - those keep pointing at
the last stable `0.2.x` release until a stable `1.0.0` ships and takes them over.

## Migrations run forward, no downgrade

Schema migrations apply automatically at process start, forward only
(`deploy/README.md`'s "Rollout" section) - there is no downgrade path for the schema itself.
Back up your database before rolling out `1.0.0-rc` the same way you would before any schema
change. If something looks wrong afterwards, rollback means restoring that backup (`postgres`
backend) or, with the Git backend, simply keeping the untouched Git vault as the source of truth
and not cutting traffic over to a new instance in the first place - the same two rollback shapes
`docs/guides/upgrade-0.2.md`'s own "Rollback via export" section describes, unchanged here.

## Verify

`scripts/upgrade-smoke-1.0.sh` is the executable version of every claim above that can be
verified mechanically:

- a `v0.2.0` token, created and used before the upgrade, still verifies against the current image
  on both the Git backend (with a Postgres index/auth database) and the `postgres` backend, and
  `memory_read` returns the exact same `version` for the note it wrote;
- `memory_search` finds a note migrated from Git into the `postgres` backend under `me/` for its
  mapped owner, proving the migration path from `docs/guides/upgrade-0.2.md` still lands notes
  where a personal token can find them;
- `memory_write`, a stale `if_version` (`structuredContent.error == "VersionConflict"`) and
  `memory_archive` all still behave the same on freshly-upgraded data, on both backends - the
  subset of the per-profile conformance suite that substitutes for it here, since that suite's
  own goldens assume a freshly seeded database, not an upgraded one (ADR-0010);
- every migration file the current image ships is recorded in `schema_migrations`, on both
  backends;
- the old and current images' own `tools/list` responses, fetched for real over the wire, compare
  with zero `breaking` findings (ADR-0017).

## See also

- [`upgrade-0.2.md`](./upgrade-0.2.md) - the `0.1.x` -> `0.2.0` upgrade (storage backend, Entra
  login, the enterprise profile) this one builds on.
- [`../compatibility.md`](../compatibility.md) - the SemVer and deprecation policy this upgrade is
  the first release to operate under.
- [`../releasing.md`](../releasing.md) - what an rc tag publishes and how to verify it.
- [`migrate-git-to-postgres.md`](./migrate-git-to-postgres.md) - the full migration reference this
  guide's "Verify" section exercises a subset of.

## Not included

- Downgrade - there is no supported path back to `0.2.0` once migrations have applied; back up
  first (see "Migrations run forward" above).
- OAuth sessions surviving the upgrade itself - only the ongoing behaviour change (#310) is
  described above; whether a given rollout disrupts in-flight sessions is operator-specific.
