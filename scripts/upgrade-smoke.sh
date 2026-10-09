#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Upgrade smoke test (#277, WP-34): proves the path from the last published
# 0.1.x image to the current code (the image this worktree builds), through
# a real podman container for both sides, including the migrate-to-Postgres
# and export-back-to-Git round trip.
#
# Steps, in order:
#   1. A throwaway bare Git remote (`scripts/smoke-container.sh`'s own
#      pattern: created by the image's own `git`, never the host's, so
#      ownership stays the container's uid 10001 throughout - the host's git
#      would hit "dubious ownership" the moment a container tried to read a
#      host-owned repo, GIT_CONFIG_GLOBAL=/dev/null leaves no safe.directory
#      escape hatch) is seeded with one commit copying `examples/vault`
#      (read-only bind mount; `examples/vault` itself is never modified).
#   2. $UPGRADE_FROM (`ghcr.io/scramb/memory-manager:<tag>`, default: the
#      last released version from `.release-please-manifest.json`) serves
#      that remote with MM_ALLOW_UNAUTHENTICATED=1 (no DATABASE_URL, so /mcp
#      itself has no bearer-token auth to begin with); two notes are written
#      through real `memory_write` MCP calls.
#   3. That container stops (the clone/remote on the shared volume survive);
#      $CURRENT_IMAGE (default memory-manager:dev, `make image`'s own tag -
#      the current worktree's code, built the same way `make smoke` does)
#      serves the very same volume; `memory_read` must return both notes
#      with the exact `version` their `memory_write` call reported.
#   4. `migrate git-to-postgres --dry-run`, then for real, against a
#      throwaway `mm_upgrade_smoke` database on `mm-pg` (`make db-up`) -
#      both runs are `$CURRENT_IMAGE` containers too (`--network host` to
#      reach `mm-pg`'s published port), so this exercises what will ship,
#      not the dev venv. Every note's `if_version` (`vault.note.version`,
#      sha256 of the exact bytes) is compared before/after: content is never
#      touched by the import, only each note's stored path's namespace
#      segment is, so a mismatch here would mean the import corrupted
#      something.
#   5. `export` reads `mm_upgrade_smoke` back into a tar.gz + manifest.json
#      archive (`docs/guides/migrate-git-to-postgres.md`'s "Rollback"). Its
#      `vault/` entries are rewritten with each namespace's *stored* alias,
#      not the Git-side namespace name - the 'user' kind's is a generated
#      `u-<id>` (`mm_ensure_personal_ns()`), 'org' is always fixed to `org`
#      regardless of what the Git namespace was called. Both are looked up
#      from `namespaces` and renamed back before the final `diff -r` against
#      the source vault, so that diff is over note content only, the actual
#      round-trip guarantee this script is proving - not a pre-known,
#      deterministic renaming this script already introduced on purpose.
set -euo pipefail
cd "$(dirname "$0")/.."

UPGRADE_FROM="${UPGRADE_FROM:-$(jq -r '.["."]' .release-please-manifest.json)}"
OLD_IMAGE="ghcr.io/scramb/memory-manager:${UPGRADE_FROM}"
CURRENT_IMAGE="${CURRENT_IMAGE:-memory-manager:dev}"
PORT="${UPGRADE_SMOKE_PORT:-18095}"

if [[ -n "${CONTAINER_ENGINE:-}" ]]; then
  ENGINE="$CONTAINER_ENGINE"
elif command -v podman >/dev/null 2>&1; then
  ENGINE=podman
elif command -v docker >/dev/null 2>&1; then
  ENGINE=docker
else
  echo "FAIL: neither podman nor docker found on PATH" >&2
  exit 1
fi

ADMIN_URL="${MM_TEST_DATABASE_URL:-postgresql://mm:mm@localhost:55432/mm}"
DB_NAME="mm_upgrade_smoke"
DATABASE_URL="${ADMIN_URL%/*}/${DB_NAME}"

WORKDIR=$(mktemp -d)
DATA="${WORKDIR}/data"
EXPORT_DIR="${WORKDIR}/export"
mkdir -p "$DATA"
chmod 0777 "$DATA"

CONTAINER_NAME=""

drop_database() {
  podman exec mm-pg psql -U mm -d mm -v ON_ERROR_STOP=1 \
    -c "select pg_terminate_backend(pid) from pg_stat_activity where datname = '${DB_NAME}' and pid <> pg_backend_pid();" \
    -c "drop database if exists ${DB_NAME};" \
    >/dev/null 2>&1 || true
}

cleanup() {
  if [[ -n "$CONTAINER_NAME" ]]; then
    "$ENGINE" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  fi
  # Same reasoning as scripts/smoke-container.sh's own cleanup: the clone
  # and the bare remote were written by the image's uid 10001, the host
  # user cannot unlink them directly.
  "$ENGINE" run --rm --user 0:0 --entrypoint rm \
    --volume "${WORKDIR}:/cleanup" "$CURRENT_IMAGE" -rf /cleanup/data >/dev/null 2>&1 || true
  drop_database
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

wait_healthz() {
  local status=""
  for _ in $(seq 1 30); do
    if status=$(curl --silent --show-error --output /dev/null \
        --write-out '%{http_code}' "http://127.0.0.1:${PORT}/healthz" 2>/dev/null); then
      [[ "$status" == "200" ]] && return 0
    fi
    sleep 1
  done
  echo "FAIL: GET /healthz did not return 200 within 30s (got ${status:-<none>})" >&2
  "$ENGINE" logs "$CONTAINER_NAME" >&2 || true
  return 1
}

mcp_body() {
  # $1 tool name, $2 arguments (already valid JSON)
  jq -n --arg tool "$1" --argjson arguments "$2" \
    '{jsonrpc: "2.0", id: 1, method: "tools/call", params: {name: $tool, arguments: $arguments}}'
}

# A response may come back as a plain JSON body or as a one-event SSE
# stream ("event: message\ndata: {...}\n\n") depending on how the MCP SDK
# picked between the Accept header's two offered media types; unwrap the
# latter down to its one `data:` line before anything tries to `jq` it.
mcp_unwrap() {
  if jq -e . >/dev/null 2>&1 <<<"$1"; then
    printf '%s' "$1"
  else
    sed -n 's/^data: //p' <<<"$1" | tail -n1
  fi
}

mcp_call() {
  # $1 base url, $2 tool name, $3 arguments json
  local raw
  raw=$(curl --silent --show-error --fail \
    --header 'Content-Type: application/json' \
    --header 'Accept: application/json, text/event-stream' \
    --data "$(mcp_body "$2" "$3")" \
    "$1/mcp")
  mcp_unwrap "$raw"
}

require_ok() {
  # $1 response json, $2 label for the error message
  #
  # Not `.result.isError // "null"`: jq's `//` falls back on `false` too,
  # not just `null`/absent, so that would always take the "null" branch for
  # the one value (`false`) this check actually wants to recognize.
  local is_error
  is_error=$(jq -r 'if .result == null then "no-result" else (.result.isError // false) end' <<<"$1")
  if [[ "$is_error" != "false" ]]; then
    echo "FAIL: $2 returned an error: $1" >&2
    exit 1
  fi
}

NOTE1_PATH="personal/fact/upgrade-smoke-note-1.md"
NOTE1_BODY=$'---\ntitle: Upgrade smoke note 1\ndescription: Written through the old image, before the upgrade.\ntype: fact\n---\nWritten by scripts/upgrade-smoke.sh against the old image, before switching to the current one.\n'
NOTE2_PATH="work/fact/upgrade-smoke-note-2.md"
NOTE2_BODY=$'---\ntitle: Upgrade smoke note 2\ndescription: Written through the old image, before the upgrade.\ntype: fact\n---\nA second note, in a different namespace, written the same way as the first.\n'

echo "UPGRADE_FROM=${UPGRADE_FROM} (${OLD_IMAGE}) -> CURRENT_IMAGE=${CURRENT_IMAGE}"

# --- 1. seed a throwaway remote with a copy of examples/vault --------------

echo "creating a throwaway Git remote (bare)..."
"$ENGINE" run --rm --entrypoint git \
  --volume "${DATA}:/data" \
  "$OLD_IMAGE" init --quiet --bare --initial-branch=main /data/remote.git

echo "seeding the remote with a copy of examples/vault..."
"$ENGINE" run --rm --entrypoint bash \
  --volume "${DATA}:/data" \
  --volume "$(pwd)/examples/vault:/examples:ro" \
  "$OLD_IMAGE" -c '
    set -euo pipefail
    git clone --quiet /data/remote.git /data/seed
    cp -r /examples/. /data/seed/
    cd /data/seed
    git -c user.name="upgrade-smoke" -c user.email="upgrade-smoke@example.invalid" add -A
    git -c user.name="upgrade-smoke" -c user.email="upgrade-smoke@example.invalid" \
      commit --quiet -m "seed: copy examples/vault for the upgrade smoke test"
    git push --quiet origin main
  '

# --- 2. old image: serve + write two notes ----------------------------------

echo "starting the old image (${OLD_IMAGE})..."
CONTAINER_NAME="mm-upgrade-smoke-old-$$"
"$ENGINE" run --detach --name "$CONTAINER_NAME" \
  --read-only --tmpfs /tmp \
  --publish "${PORT}:8080" \
  --volume "${DATA}:/data" \
  --env VAULT_REMOTE=file:///data/remote.git \
  --env MM_ALLOW_UNAUTHENTICATED=1 \
  "$OLD_IMAGE" >/dev/null
wait_healthz
echo "OK: old image is up"

OLD_URL="http://127.0.0.1:${PORT}"

resp1=$(mcp_call "$OLD_URL" memory_write \
  "$(jq -n --arg path "$NOTE1_PATH" --arg content "$NOTE1_BODY" \
    '{path: $path, content: $content, if_version: "new"}')")
require_ok "$resp1" "memory_write ${NOTE1_PATH} on the old image"
VERSION1=$(jq -r '.result.structuredContent.version' <<<"$resp1")

resp2=$(mcp_call "$OLD_URL" memory_write \
  "$(jq -n --arg path "$NOTE2_PATH" --arg content "$NOTE2_BODY" \
    '{path: $path, content: $content, if_version: "new"}')")
require_ok "$resp2" "memory_write ${NOTE2_PATH} on the old image"
VERSION2=$(jq -r '.result.structuredContent.version' <<<"$resp2")

echo "OK: wrote ${NOTE1_PATH} (${VERSION1:0:12}...) and ${NOTE2_PATH} (${VERSION2:0:12}...) through the old image"

"$ENGINE" rm -f "$CONTAINER_NAME" >/dev/null
CONTAINER_NAME=""

# --- 3. current image: serve the same volume, read both notes back ---------

echo "starting the current image (${CURRENT_IMAGE})..."
CONTAINER_NAME="mm-upgrade-smoke-current-$$"
"$ENGINE" run --detach --name "$CONTAINER_NAME" \
  --read-only --tmpfs /tmp \
  --publish "${PORT}:8080" \
  --volume "${DATA}:/data" \
  --env VAULT_REMOTE=file:///data/remote.git \
  --env MM_ALLOW_UNAUTHENTICATED=1 \
  "$CURRENT_IMAGE" >/dev/null
wait_healthz
echo "OK: current image is up, on the same vault the old image just wrote to"

CURRENT_URL="http://127.0.0.1:${PORT}"

read_resp=$(mcp_call "$CURRENT_URL" memory_read \
  "$(jq -n --arg p1 "$NOTE1_PATH" --arg p2 "$NOTE2_PATH" '{items: [$p1, $p2]}')")
require_ok "$read_resp" "memory_read on the current image"

unchanged=$(jq --arg p1 "$NOTE1_PATH" --arg v1 "$VERSION1" --arg p2 "$NOTE2_PATH" --arg v2 "$VERSION2" '
  .result.structuredContent.result as $items
  | (($items[] | select(.path == $p1) | .version) == $v1)
    and (($items[] | select(.path == $p2) | .version) == $v2)
' <<<"$read_resp")
if [[ "$unchanged" != "true" ]]; then
  echo "FAIL: the current image did not read both notes back unchanged: ${read_resp}" >&2
  exit 1
fi
echo "OK: both notes read back through the current image with their original version"

"$ENGINE" rm -f "$CONTAINER_NAME" >/dev/null
CONTAINER_NAME=""

# --- 4. migrate git-to-postgres: dry run, then for real ---------------------

echo "waiting for mm-pg..."
ready=0
for _ in $(seq 1 30); do
  if podman exec mm-pg pg_isready -U mm >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" -ne 1 ]]; then
  echo "FAIL: mm-pg did not become ready (see 'make db-up')" >&2
  exit 1
fi

podman exec mm-pg psql -U mm -d mm -v ON_ERROR_STOP=1 \
  -c "drop database if exists ${DB_NAME};" \
  -c "create database ${DB_NAME} owner mm;" \
  >/dev/null

MAP_ARGS=(--map personal=user:oid-example --map work=project:work --map shared=org:org)

echo "migrate git-to-postgres --dry-run..."
dry_run_out=$("$ENGINE" run --rm \
  --volume "${DATA}:/data" \
  "$CURRENT_IMAGE" memory-manager migrate git-to-postgres \
  --vault /data/vault "${MAP_ARGS[@]}" --dry-run)
echo "$dry_run_out"
echo "$dry_run_out" | grep -q '0 problem(s)' || {
  echo "FAIL: migrate --dry-run reported a problem" >&2
  exit 1
}

echo "migrate git-to-postgres (real import)..."
import_out=$("$ENGINE" run --rm --network host \
  --volume "${DATA}:/data" \
  --env "DATABASE_URL=${DATABASE_URL}" \
  "$CURRENT_IMAGE" memory-manager migrate git-to-postgres \
  --vault /data/vault "${MAP_ARGS[@]}")
echo "$import_out"
echo "$import_out" | grep -q '3 namespace(s) imported, 0 refused' || {
  echo "FAIL: migrate git-to-postgres did not import all 3 namespaces cleanly" >&2
  exit 1
}

# --- if_version, before (the local git vault) vs. after (vault_notes) ------

# `memory_write` saves through `tempfile.mkstemp` (`vault/repo.py`'s
# `_atomic_write`), mode 0600 - unlike the examples/vault files git itself
# checked out (0644), the two notes this script just wrote are only
# readable by the uid that wrote them (the container's 10001, not this
# script's own host user), so this reads them back through a
# $CURRENT_IMAGE container too, the same uid that owns them.
BEFORE_JSON="${WORKDIR}/before.json"
"$ENGINE" run --rm --interactive \
  --volume "${DATA}:/data" \
  --entrypoint python \
  "$CURRENT_IMAGE" - /data/vault <<'PY' >"$BEFORE_JSON"
import json
import sys
from pathlib import Path

from memory_manager.vault.note import parse, version
from memory_manager.vault.paths import iter_md_files

root = Path(sys.argv[1])
result = {}
for path in iter_md_files(root):
    data = path.read_bytes()
    note = parse(data)
    result[note.id] = version(data)
json.dump(result, sys.stdout)
PY

AFTER_JSON="${WORKDIR}/after.json"
podman exec mm-pg psql -U mm -d "$DB_NAME" -At -c \
  "select coalesce(json_object_agg(id, version), '{}'::json) from vault_notes;" \
  >"$AFTER_JSON"

uv run python - "$BEFORE_JSON" "$AFTER_JSON" <<'PY'
import json
import sys

before = json.load(open(sys.argv[1]))
after = json.load(open(sys.argv[2]))
missing = sorted(set(before) - set(after))
extra = sorted(set(after) - set(before))
mismatched = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
if missing or extra or mismatched:
    print(
        f"FAIL: if_version mismatch after migration - "
        f"missing={missing} extra={extra} mismatched={mismatched}",
        file=sys.stderr,
    )
    sys.exit(1)
print(f"OK: if_version identical for all {len(before)} notes before/after migration")
PY

# --- 5. export back to a fresh Git vault, diff against the source ----------

echo "export (rollback archive)..."
"$ENGINE" run --rm --network host \
  --volume "${DATA}:/data" \
  --env "DATABASE_URL=${DATABASE_URL}" \
  --env STORAGE_BACKEND=postgres \
  "$CURRENT_IMAGE" memory-manager export --out /data/rollback.tar.gz --force

mkdir -p "$EXPORT_DIR"
tar -xzf "${DATA}/rollback.tar.gz" -C "$EXPORT_DIR"

# `export` wrote every note under the *stored* alias, not the Git-side
# namespace name - look both up and rename the export's directories back,
# so the diff below is over note content only (migrate_git.py's docstring;
# docs/guides/migrate-git-to-postgres.md's "--map syntax" table).
PERSONAL_ALIAS=$(podman exec mm-pg psql -U mm -d "$DB_NAME" -At -c \
  "select alias from namespaces where kind = 'user' and external_key = 'oid-example';")

rename_namespace_dir() {
  # $1 stored alias, $2 git namespace name
  for prefix in "vault" "vault/_archive"; do
    if [[ -d "${EXPORT_DIR}/${prefix}/$1" ]]; then
      mv "${EXPORT_DIR}/${prefix}/$1" "${EXPORT_DIR}/${prefix}/$2"
    fi
  done
}

rename_namespace_dir "$PERSONAL_ALIAS" personal
rename_namespace_dir org shared
# 'work' kept its own name as the default project alias - nothing to rename.

# A plain host `diff -r` can't read the two notes `memory_write` wrote
# (0600, owned by the container's uid, same reasoning as the if_version
# check above) - compared through a $CURRENT_IMAGE container instead,
# `/export` (host-owned, just extracted, world-readable) bind-mounted
# alongside `/data`.
"$ENGINE" run --rm --interactive \
  --volume "${DATA}:/data" \
  --volume "${EXPORT_DIR}:/export:ro" \
  --entrypoint python \
  "$CURRENT_IMAGE" - /export/vault /data/vault <<'PY'
import sys
from pathlib import Path

exported_root = Path(sys.argv[1])
source_root = Path(sys.argv[2])


def note_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


exported_files = note_files(exported_root)
source_files = note_files(source_root)
only_exported = sorted(exported_files - source_files)
only_source = sorted(source_files - exported_files)
mismatched = sorted(
    rel
    for rel in exported_files & source_files
    if (exported_root / rel).read_bytes() != (source_root / rel).read_bytes()
)

if only_exported or only_source or mismatched:
    print(
        f"FAIL: the exported vault differs from the source vault - "
        f"only_in_export={only_exported} only_in_source={only_source} "
        f"content_mismatch={mismatched}",
        file=sys.stderr,
    )
    sys.exit(1)
print(
    f"OK: the exported vault is identical to the source vault "
    f"({len(exported_files)} note file(s), namespace renames accounted for)"
)
PY

echo "upgrade-smoke: PASS"
