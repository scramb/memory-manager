#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# 1.0 upgrade smoke test (#202, WP-59): proves the path from the last 0.x
# release ($UPGRADE_FROM, fixed default "0.2.0" - not read from
# .release-please-manifest.json, unlike scripts/upgrade-smoke.sh's own
# 0.1.x -> 0.2.0 test, because the manifest will keep moving past 1.0 while
# this specific upgrade must stay provable) to this worktree's own current
# image ($CURRENT_IMAGE, default `memory-manager:upgrade-smoke` - a tag of
# its own, never `memory-manager:dev`, so this never rebuilds or touches
# whatever another lane may be running against that tag), on both storage
# backends.
#
# Two independent scenarios, each: old image writes/serves, current image
# reads/serves the very same data:
#
#   A. Git backend with a Postgres index/auth database (ADR-0007 §2): a
#      throwaway bare Git remote is seeded with a copy of `examples/vault`;
#      the old image creates an owner-less static token and serves that
#      remote with `--network host` (to reach `mm-pg`'s published port),
#      `DATABASE_URL` pointing at a throwaway database and `PUBLIC_URL`
#      (mandatory once `DATABASE_URL` is set, ADR-0004) and writes one note
#      through a real `memory_write` call, bearer-authenticated. The
#      current image then serves the same Git remote and the same
#      database and must read that note back with the exact `version`.
#
#   B. Postgres backend (ADR-0007 §4): the old image's own `migrate
#      git-to-postgres` imports the vault scenario A's old image left
#      behind (same `--map` shape as scripts/upgrade-smoke.sh) into a
#      second throwaway database. `docs/guides/upgrade-0.2.md` "Verify"
#      notes that `STORAGE_BACKEND=postgres` refuses a token for an owner
#      with no `users` row - normally a completed Entra login
#      (`auth/login_entra.py`'s `upsert_user`) - so this inserts that one
#      row directly with `psql`, simulating the one-time login an operator
#      would otherwise have to perform between the import and creating a
#      token. The old image then creates a personal-scoped static token
#      for that owner and serves the database (plus a disposable
#      `DATABASE_APP_ROLE`, granted automatically at startup,
#      `db/rls.py`'s `grant_app_role`) and writes one note under `me/`.
#      The current image then serves the same database and role and must
#      read that note back unchanged, and find the *migrated* note
#      (`examples/vault/personal/feedback/disliked-emoji-in-commit-
#      messages.md`) under `me/` too - `migrate_git.py`'s own docstring:
#      "a personal namespace is always shown as `me`, never under a chosen
#      alias" - and `mm_ensure_personal_ns()` is idempotent, so the mapped
#      owner's own token resolves `me` to the alias the import already
#      created.
#
# On the current image, both scenarios additionally exercise the write
# path new code must still support - `memory_write` (create), a stale
# `if_version` (`structuredContent.error == "VersionConflict"`) and
# `memory_archive` - the subset of the per-profile conformance suite this
# script substitutes for an upgraded database (the suite's own goldens
# assume a freshly seeded one, ADR-0010), and that every migration file
# the image ships is recorded in `schema_migrations` (`importlib.resources`
# over `memory_manager.db.migrations`, generic so this keeps working once
# later migrations land).
#
# Finally, the old and current images' own `tools/list` responses (fetched
# for real, over the wire, during scenario A) are compared with
# `tests/contract_snapshot.py`'s own `normalize_schema`/`compare_snapshots`
# (ADR-0017) - any `breaking` finding fails this script, proving the tool
# contract itself only ever changed additively across this upgrade.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

# Engine detection, /healthz polling, MCP tool calls and the mm-pg
# throwaway-database helpers - shared with scripts/upgrade-smoke.sh, see
# that file for its own 0.1.x -> 0.2.0 upgrade path.
source "${REPO_ROOT}/scripts/lib/upgrade-smoke.sh"

UPGRADE_FROM="${UPGRADE_FROM:-0.2.0}"
OLD_IMAGE="ghcr.io/scramb/memory-manager:${UPGRADE_FROM}"
CURRENT_IMAGE="${CURRENT_IMAGE:-memory-manager:upgrade-smoke}"
GIT_PORT="${UPGRADE_SMOKE_1_0_GIT_PORT:-18096}"
PG_PORT="${UPGRADE_SMOKE_1_0_PG_PORT:-18097}"
PUBLIC_URL="https://mm.upgrade-smoke-1-0.example.invalid"

mm_detect_engine

ADMIN_URL="${MM_TEST_DATABASE_URL:-postgresql://mm:mm@localhost:55432/mm}"
GIT_DB_NAME="mm_upgrade_smoke_1_0_git"
PG_DB_NAME="mm_upgrade_smoke_1_0_pg"
GIT_DATABASE_URL="${ADMIN_URL%/*}/${GIT_DB_NAME}"
PG_DATABASE_URL="${ADMIN_URL%/*}/${PG_DB_NAME}"
APP_ROLE="mm_upgrade_smoke_1_0_app_$$"
MAPPED_OID="oid-upgrade-smoke-1-0"

WORKDIR=$(mktemp -d)
DATA="${WORKDIR}/data"
mkdir -p "$DATA"
chmod 0777 "$DATA"

CONTAINER_NAME=""
APP_ROLE_CREATED=0

cleanup() {
  if [[ -n "$CONTAINER_NAME" ]]; then
    "$ENGINE" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  fi
  # Same reasoning as scripts/upgrade-smoke.sh's own cleanup: the clone and
  # the bare remote were written by the image's uid 10001, the host user
  # cannot unlink them directly.
  "$ENGINE" run --rm --user 0:0 --entrypoint rm \
    --volume "${WORKDIR}:/cleanup" "$CURRENT_IMAGE" -rf /cleanup/data >/dev/null 2>&1 || true
  if [[ "$APP_ROLE_CREATED" -eq 1 ]]; then
    # Drop owned (on the database the role was granted on) before drop
    # role - the order tests/conformance_fixtures.py's own `drop_app_role`
    # fixture uses, for the same "DependentObjectsStillExistError"
    # reason - best effort, this only ever runs from a cleanup trap.
    podman exec mm-pg psql -U mm -d "$PG_DB_NAME" -v ON_ERROR_STOP=1 \
      -c "drop owned by \"${APP_ROLE}\";" >/dev/null 2>&1 || true
    podman exec mm-pg psql -U mm -d mm -v ON_ERROR_STOP=1 \
      -c "drop role if exists \"${APP_ROLE}\";" >/dev/null 2>&1 || true
  fi
  mm_pg_drop_database "$PG_DB_NAME"
  mm_pg_drop_database "$GIT_DB_NAME"
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

echo "UPGRADE_FROM=${UPGRADE_FROM} (${OLD_IMAGE}) -> CURRENT_IMAGE=${CURRENT_IMAGE}"

echo "waiting for mm-pg..."
mm_pg_wait_ready
mm_pg_create_database "$GIT_DB_NAME" mm
mm_pg_create_database "$PG_DB_NAME" mm

# --- tools/list helper (needs a bearer token, both scenarios require one) --

mcp_list_tools() {
  # $1 base url, $2 bearer token
  local raw
  raw=$(curl --silent --show-error --fail \
    --header 'Content-Type: application/json' \
    --header 'Accept: application/json, text/event-stream' \
    --header "Authorization: Bearer $2" \
    --data '{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}' \
    "$1/mcp")
  mm_mcp_unwrap "$raw"
}

# --- scenario A: Git backend + Postgres index/auth -------------------------

echo "=== scenario A: Git backend with a Postgres index/auth database ==="

echo "creating a throwaway Git remote (bare)..."
"$ENGINE" run --rm --entrypoint git \
  --volume "${DATA}:/data" \
  "$OLD_IMAGE" init --quiet --bare --initial-branch=main /data/remote.git

echo "seeding the remote with a copy of examples/vault..."
"$ENGINE" run --rm --entrypoint bash \
  --volume "${DATA}:/data" \
  --volume "${REPO_ROOT}/examples/vault:/examples:ro" \
  "$OLD_IMAGE" -c '
    set -euo pipefail
    git clone --quiet /data/remote.git /data/seed
    cp -r /examples/. /data/seed/
    cd /data/seed
    git -c user.name="upgrade-smoke-1-0" -c user.email="upgrade-smoke-1-0@example.invalid" add -A
    git -c user.name="upgrade-smoke-1-0" -c user.email="upgrade-smoke-1-0@example.invalid" \
      commit --quiet -m "seed: copy examples/vault for the 1.0 upgrade smoke test"
    git push --quiet origin main
  '

echo "creating an owner-less static token on the old image (git + Postgres index)..."
GIT_TOKEN=$("$ENGINE" run --rm --network host \
  --env "DATABASE_URL=${GIT_DATABASE_URL}" \
  "$OLD_IMAGE" memory-manager token create upgrade-smoke-1-0-git \
  --scope memory:read --scope memory:write)

echo "starting the old image (${OLD_IMAGE}, git + Postgres index)..."
CONTAINER_NAME="mm-upgrade-smoke-1-0-old-git-$$"
"$ENGINE" run --detach --name "$CONTAINER_NAME" --network host \
  --read-only --tmpfs /tmp \
  --volume "${DATA}:/data" \
  --env HOST=127.0.0.1 \
  --env "PORT=${GIT_PORT}" \
  --env VAULT_REMOTE=file:///data/remote.git \
  --env "DATABASE_URL=${GIT_DATABASE_URL}" \
  --env "PUBLIC_URL=${PUBLIC_URL}" \
  --env EMBEDDING_PROVIDER=none \
  "$OLD_IMAGE" >/dev/null
mm_wait_healthz "$GIT_PORT" "$CONTAINER_NAME"
echo "OK: old image is up (git + Postgres index)"

OLD_GIT_URL="http://127.0.0.1:${GIT_PORT}"
GIT_NOTE_PATH="personal/fact/upgrade-smoke-1-0-git-note.md"
GIT_NOTE_BODY=$'---\ntitle: 1.0 upgrade smoke note (git)\ndescription: Written through the old image, before the 1.0 upgrade.\ntype: fact\n---\nWritten by scripts/upgrade-smoke-1.0.sh against the old image (git backend), before switching to the current one.\n'

resp=$(mm_mcp_call "$OLD_GIT_URL" memory_write \
  "$(jq -n --arg path "$GIT_NOTE_PATH" --arg content "$GIT_NOTE_BODY" \
    '{path: $path, content: $content, if_version: "new"}')" "$GIT_TOKEN")
mm_require_ok "$resp" "memory_write ${GIT_NOTE_PATH} on the old image"
GIT_NOTE_VERSION_OLD=$(jq -r '.result.structuredContent.version' <<<"$resp")
echo "OK: wrote ${GIT_NOTE_PATH} (${GIT_NOTE_VERSION_OLD:0:12}...) through the old image"

echo "fetching tools/list from the old image..."
OLD_TOOLS_JSON="${WORKDIR}/old-tools.json"
mcp_list_tools "$OLD_GIT_URL" "$GIT_TOKEN" >"$OLD_TOOLS_JSON"

"$ENGINE" rm -f "$CONTAINER_NAME" >/dev/null
CONTAINER_NAME=""

# --- scenario B: Postgres backend -------------------------------------------

echo "=== scenario B: Postgres backend ==="

echo "migrate git-to-postgres (old image, on the vault the old image left)..."
import_out=$("$ENGINE" run --rm --network host \
  --volume "${DATA}:/data" \
  --env "DATABASE_URL=${PG_DATABASE_URL}" \
  "$OLD_IMAGE" memory-manager migrate git-to-postgres \
  --vault /data/vault \
  --map "personal=user:${MAPPED_OID}" --map work=project:work --map shared=org:org)
echo "$import_out"
echo "$import_out" | grep -q '3 namespace(s) imported, 0 refused' || {
  echo "FAIL: migrate git-to-postgres did not import all 3 namespaces cleanly" >&2
  exit 1
}

# `migrate git-to-postgres` never writes `users` - only a completed Entra
# login does (`auth/login_entra.py`'s `upsert_user`) - so
# `STORAGE_BACKEND=postgres` would otherwise refuse a token for
# $MAPPED_OID (docs/guides/upgrade-0.2.md "Verify": "the owner must sign
# in at least once"). This simulates that one-time login directly, the
# same row tests/conformance_fixtures.py's own `seed_personal_namespace`
# inserts.
echo "simulating the mapped owner's one-time login (users row)..."
podman exec mm-pg psql -U mm -d "$PG_DB_NAME" -v ON_ERROR_STOP=1 \
  -c "insert into users (oid, tid, display_name) values ('${MAPPED_OID}', 'tenant-upgrade-smoke-1-0', '${MAPPED_OID}');" \
  >/dev/null

echo "creating a disposable app role..."
podman exec mm-pg psql -U mm -d mm -v ON_ERROR_STOP=1 \
  -c "create role \"${APP_ROLE}\" nologin nosuperuser nobypassrls;" \
  >/dev/null
APP_ROLE_CREATED=1

echo "creating a personal, owner-scoped static token on the old image (postgres)..."
PG_TOKEN=$("$ENGINE" run --rm --network host \
  --env STORAGE_BACKEND=postgres \
  --env "DATABASE_URL=${PG_DATABASE_URL}" \
  "$OLD_IMAGE" memory-manager token create upgrade-smoke-1-0-pg \
  --scope memory:read --scope memory:write \
  --owner "$MAPPED_OID" --role Memory.User --expires-days 1)

echo "starting the old image (${OLD_IMAGE}, postgres backend)..."
CONTAINER_NAME="mm-upgrade-smoke-1-0-old-pg-$$"
"$ENGINE" run --detach --name "$CONTAINER_NAME" --network host \
  --read-only --tmpfs /tmp \
  --env HOST=127.0.0.1 \
  --env "PORT=${PG_PORT}" \
  --env STORAGE_BACKEND=postgres \
  --env "DATABASE_URL=${PG_DATABASE_URL}" \
  --env "DATABASE_APP_ROLE=${APP_ROLE}" \
  --env "PUBLIC_URL=${PUBLIC_URL}" \
  --env EMBEDDING_PROVIDER=none \
  "$OLD_IMAGE" >/dev/null
mm_wait_healthz "$PG_PORT" "$CONTAINER_NAME"
echo "OK: old image is up (postgres backend)"

OLD_PG_URL="http://127.0.0.1:${PG_PORT}"
PG_NOTE_PATH="me/fact/upgrade-smoke-1-0-pg-note.md"
PG_NOTE_BODY=$'---\ntitle: 1.0 upgrade smoke note (postgres)\ndescription: Written through the old image, before the 1.0 upgrade.\ntype: fact\n---\nWritten by scripts/upgrade-smoke-1.0.sh against the old image (postgres backend), before switching to the current one.\n'

resp=$(mm_mcp_call "$OLD_PG_URL" memory_write \
  "$(jq -n --arg path "$PG_NOTE_PATH" --arg content "$PG_NOTE_BODY" \
    '{path: $path, content: $content, if_version: "new"}')" "$PG_TOKEN")
mm_require_ok "$resp" "memory_write ${PG_NOTE_PATH} on the old image"
PG_NOTE_VERSION_OLD=$(jq -r '.result.structuredContent.version' <<<"$resp")
echo "OK: wrote ${PG_NOTE_PATH} (${PG_NOTE_VERSION_OLD:0:12}...) through the old image"

"$ENGINE" rm -f "$CONTAINER_NAME" >/dev/null
CONTAINER_NAME=""

# --- current image: every migration the image ships is applied -------------

echo "listing the migration files the current image ships..."
MIGRATIONS_JSON=$("$ENGINE" run --rm --entrypoint python "$CURRENT_IMAGE" -c '
import json
from importlib import resources

pkg = resources.files("memory_manager.db.migrations")
common = sorted(e.name.removesuffix(".sql") for e in pkg.iterdir() if e.name.endswith(".sql"))
pg_dir = pkg / "postgres"
postgres_only = sorted(
    e.name.removesuffix(".sql") for e in pg_dir.iterdir() if e.name.endswith(".sql")
)
print(json.dumps({"git": common, "postgres": sorted(common + postgres_only)}))
')

check_schema_migrations() {
  # $1 database name, $2 "git" or "postgres" (the expected migration set)
  local expected actual
  expected=$(jq -r --arg backend "$2" '.[$backend][]' <<<"$MIGRATIONS_JSON" | sort)
  actual=$(podman exec mm-pg psql -U mm -d "$1" -At -c 'select version from schema_migrations order by version;' | sort)
  if [[ "$expected" != "$actual" ]]; then
    echo "FAIL: schema_migrations in ${1} does not match the current image's own migration files" >&2
    echo "expected:" >&2
    echo "$expected" >&2
    echo "actual:" >&2
    echo "$actual" >&2
    exit 1
  fi
  echo "OK: schema_migrations in ${1} contains every migration file the current image ships"
}

# --- current image: git backend + Postgres index/auth -----------------------

echo "=== current image, scenario A: git backend + Postgres index/auth ==="

CONTAINER_NAME="mm-upgrade-smoke-1-0-new-git-$$"
"$ENGINE" run --detach --name "$CONTAINER_NAME" --network host \
  --read-only --tmpfs /tmp \
  --volume "${DATA}:/data" \
  --env HOST=127.0.0.1 \
  --env "PORT=${GIT_PORT}" \
  --env VAULT_REMOTE=file:///data/remote.git \
  --env "DATABASE_URL=${GIT_DATABASE_URL}" \
  --env "PUBLIC_URL=${PUBLIC_URL}" \
  --env EMBEDDING_PROVIDER=none \
  "$CURRENT_IMAGE" >/dev/null
mm_wait_healthz "$GIT_PORT" "$CONTAINER_NAME"
echo "OK: current image is up (git + Postgres index)"

NEW_GIT_URL="http://127.0.0.1:${GIT_PORT}"

read_resp=$(mm_mcp_call "$NEW_GIT_URL" memory_read \
  "$(jq -n --arg p "$GIT_NOTE_PATH" '{items: [$p]}')" "$GIT_TOKEN")
mm_require_ok "$read_resp" "memory_read ${GIT_NOTE_PATH} on the current image"
version=$(jq -r '.result.structuredContent.result[0].version' <<<"$read_resp")
if [[ "$version" != "$GIT_NOTE_VERSION_OLD" ]]; then
  echo "FAIL: ${GIT_NOTE_PATH} changed version across the upgrade: ${GIT_NOTE_VERSION_OLD} -> ${version}" >&2
  exit 1
fi
echo "OK: the 0.2.0 token still verifies, ${GIT_NOTE_PATH} read back unchanged"

echo "fetching tools/list from the current image..."
NEW_TOOLS_JSON="${WORKDIR}/new-tools.json"
mcp_list_tools "$NEW_GIT_URL" "$GIT_TOKEN" >"$NEW_TOOLS_JSON"

NEW_GIT_NOTE_PATH="personal/fact/upgrade-smoke-1-0-git-new.md"
NEW_GIT_NOTE_BODY=$'---\ntitle: 1.0 upgrade smoke new note (git)\ndescription: Written through the current image, after the 1.0 upgrade.\ntype: fact\n---\nWritten by scripts/upgrade-smoke-1.0.sh against the current image (git backend), after the upgrade.\n'

resp=$(mm_mcp_call "$NEW_GIT_URL" memory_write \
  "$(jq -n --arg path "$NEW_GIT_NOTE_PATH" --arg content "$NEW_GIT_NOTE_BODY" \
    '{path: $path, content: $content, if_version: "new"}')" "$GIT_TOKEN")
mm_require_ok "$resp" "memory_write ${NEW_GIT_NOTE_PATH} on the current image"
new_version=$(jq -r '.result.structuredContent.version' <<<"$resp")
new_id=$(jq -r '.result.structuredContent.id' <<<"$resp")
echo "OK: memory_write still creates a note on the current image (git backend)"

# An update (any if_version other than "new") must carry `id`/`created`/
# `updated` already present in its frontmatter (`_prepare_write_content`
# only ever inserts placeholders for a create) - the stale-version probe
# below reuses the id the create above was just given, plus two
# syntactically valid timestamps (overwritten server-side regardless of
# what they say, same as every other update).
STALE_GIT_NOTE_BODY=$(jq -r -n --arg id "$new_id" --arg content "$NEW_GIT_NOTE_BODY" \
  '$content | sub(
    "^---\\n";
    "---\nid: " + $id + "\ncreated: 2025-01-01T00:00:00Z\nupdated: 2025-01-01T00:00:00Z\n"
  )')
conflict_resp=$(mm_mcp_call "$NEW_GIT_URL" memory_write \
  "$(jq -n --arg path "$NEW_GIT_NOTE_PATH" --arg content "$STALE_GIT_NOTE_BODY" \
    '{path: $path, content: $content, if_version: "0000000000000000000000000000000000000000000000000000000000000000"}')" "$GIT_TOKEN")
error_kind=$(jq -r '.result.structuredContent.error // "none"' <<<"$conflict_resp")
if [[ "$error_kind" != "VersionConflict" ]]; then
  echo "FAIL: a stale if_version did not report VersionConflict on the current image: ${conflict_resp}" >&2
  exit 1
fi
echo "OK: a stale if_version still reports VersionConflict on the current image (git backend)"

archive_resp=$(mm_mcp_call "$NEW_GIT_URL" memory_archive \
  "$(jq -n --arg path "$NEW_GIT_NOTE_PATH" --arg if_version "$new_version" '{path: $path, if_version: $if_version}')" "$GIT_TOKEN")
mm_require_ok "$archive_resp" "memory_archive ${NEW_GIT_NOTE_PATH} on the current image"
echo "OK: memory_archive still archives a note on the current image (git backend)"

check_schema_migrations "$GIT_DB_NAME" git

"$ENGINE" rm -f "$CONTAINER_NAME" >/dev/null
CONTAINER_NAME=""

# --- current image: postgres backend ----------------------------------------

echo "=== current image, scenario B: postgres backend ==="

CONTAINER_NAME="mm-upgrade-smoke-1-0-new-pg-$$"
"$ENGINE" run --detach --name "$CONTAINER_NAME" --network host \
  --read-only --tmpfs /tmp \
  --env HOST=127.0.0.1 \
  --env "PORT=${PG_PORT}" \
  --env STORAGE_BACKEND=postgres \
  --env "DATABASE_URL=${PG_DATABASE_URL}" \
  --env "DATABASE_APP_ROLE=${APP_ROLE}" \
  --env "PUBLIC_URL=${PUBLIC_URL}" \
  --env EMBEDDING_PROVIDER=none \
  "$CURRENT_IMAGE" >/dev/null
mm_wait_healthz "$PG_PORT" "$CONTAINER_NAME"
echo "OK: current image is up (postgres backend)"

NEW_PG_URL="http://127.0.0.1:${PG_PORT}"

read_resp=$(mm_mcp_call "$NEW_PG_URL" memory_read \
  "$(jq -n --arg p "$PG_NOTE_PATH" '{items: [$p]}')" "$PG_TOKEN")
mm_require_ok "$read_resp" "memory_read ${PG_NOTE_PATH} on the current image"
version=$(jq -r '.result.structuredContent.result[0].version' <<<"$read_resp")
if [[ "$version" != "$PG_NOTE_VERSION_OLD" ]]; then
  echo "FAIL: ${PG_NOTE_PATH} changed version across the upgrade: ${PG_NOTE_VERSION_OLD} -> ${version}" >&2
  exit 1
fi
echo "OK: the 0.2.0 personal token still verifies, ${PG_NOTE_PATH} read back unchanged"

# `personal/feedback/disliked-emoji-in-commit-messages.md` (examples/vault)
# migrated into $MAPPED_OID's personal namespace above - a personal
# namespace is always shown as `me` for its own owner (migrate_git.py),
# so the migrated note must be findable there too, not just the note
# written directly against the postgres backend above.
search_resp=$(mm_mcp_call "$NEW_PG_URL" memory_search \
  "$(jq -n '{query: "emoji"}')" "$PG_TOKEN")
mm_require_ok "$search_resp" "memory_search on the current image"
found_under_me=$(jq -r '
  [.result.structuredContent.results[] | select(.path | startswith("me/"))] | length > 0
' <<<"$search_resp")
if [[ "$found_under_me" != "true" ]]; then
  echo "FAIL: memory_search did not find the migrated personal note under me/: ${search_resp}" >&2
  exit 1
fi
echo "OK: memory_search finds the migrated personal note under me/ for its mapped owner"

NEW_PG_NOTE_PATH="me/fact/upgrade-smoke-1-0-pg-new.md"
NEW_PG_NOTE_BODY=$'---\ntitle: 1.0 upgrade smoke new note (postgres)\ndescription: Written through the current image, after the 1.0 upgrade.\ntype: fact\n---\nWritten by scripts/upgrade-smoke-1.0.sh against the current image (postgres backend), after the upgrade.\n'

resp=$(mm_mcp_call "$NEW_PG_URL" memory_write \
  "$(jq -n --arg path "$NEW_PG_NOTE_PATH" --arg content "$NEW_PG_NOTE_BODY" \
    '{path: $path, content: $content, if_version: "new"}')" "$PG_TOKEN")
mm_require_ok "$resp" "memory_write ${NEW_PG_NOTE_PATH} on the current image"
new_version=$(jq -r '.result.structuredContent.version' <<<"$resp")
new_id=$(jq -r '.result.structuredContent.id' <<<"$resp")
echo "OK: memory_write still creates a note on the current image (postgres backend)"

# Same reasoning as the git-backend probe above: an update must carry
# `id`/`created`/`updated` already present, never inserted for you the
# way a create's are.
STALE_PG_NOTE_BODY=$(jq -r -n --arg id "$new_id" --arg content "$NEW_PG_NOTE_BODY" \
  '$content | sub(
    "^---\\n";
    "---\nid: " + $id + "\ncreated: 2025-01-01T00:00:00Z\nupdated: 2025-01-01T00:00:00Z\n"
  )')
conflict_resp=$(mm_mcp_call "$NEW_PG_URL" memory_write \
  "$(jq -n --arg path "$NEW_PG_NOTE_PATH" --arg content "$STALE_PG_NOTE_BODY" \
    '{path: $path, content: $content, if_version: "0000000000000000000000000000000000000000000000000000000000000000"}')" "$PG_TOKEN")
error_kind=$(jq -r '.result.structuredContent.error // "none"' <<<"$conflict_resp")
if [[ "$error_kind" != "VersionConflict" ]]; then
  echo "FAIL: a stale if_version did not report VersionConflict on the current image: ${conflict_resp}" >&2
  exit 1
fi
echo "OK: a stale if_version still reports VersionConflict on the current image (postgres backend)"

archive_resp=$(mm_mcp_call "$NEW_PG_URL" memory_archive \
  "$(jq -n --arg path "$NEW_PG_NOTE_PATH" --arg if_version "$new_version" '{path: $path, if_version: $if_version}')" "$PG_TOKEN")
mm_require_ok "$archive_resp" "memory_archive ${NEW_PG_NOTE_PATH} on the current image"
echo "OK: memory_archive still archives a note on the current image (postgres backend)"

check_schema_migrations "$PG_DB_NAME" postgres

"$ENGINE" rm -f "$CONTAINER_NAME" >/dev/null
CONTAINER_NAME=""

# --- tool contract: old vs. current tools/list, over the wire --------------

echo "comparing old vs. current tools/list for breaking changes (ADR-0017)..."
uv run python - "$OLD_TOOLS_JSON" "$NEW_TOOLS_JSON" <<'PY'
import json
import sys

from tests.contract_snapshot import compare_snapshots, normalize_schema


def to_snapshot(resp: dict) -> dict:
    entries = {}
    for tool in resp["result"]["tools"]:
        entry = {"input": normalize_schema(tool["inputSchema"])}
        if "outputSchema" in tool:
            entry["output"] = normalize_schema(tool["outputSchema"])
        entry["annotations"] = tool.get("annotations", {})
        entries[tool["name"]] = entry
    return {"format": 1, "tools": entries}


old = to_snapshot(json.load(open(sys.argv[1])))
new = to_snapshot(json.load(open(sys.argv[2])))
findings = compare_snapshots(old, new)
breaking = [f for f in findings if f.kind == "breaking"]
if breaking:
    print(
        "FAIL: breaking change(s) in the MCP tool contract between "
        f"{sys.argv[1]} and {sys.argv[2]}:",
        file=sys.stderr,
    )
    for finding in breaking:
        print(f"  {finding}", file=sys.stderr)
    sys.exit(1)
print(f"OK: tools/list comparison found {len(findings)} change(s), none breaking")
PY

echo "upgrade-smoke-1.0: PASS"
