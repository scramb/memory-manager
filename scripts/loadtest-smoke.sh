#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Loads a synthetic vault into the Postgres backend and runs the k6 search/
# read/write scenarios against one server replica (#108, WP-21): waits for
# `mm-pg`, generates `LOADTEST_NOTES` notes (`loadtest.generate`, #107),
# bulk-loads them into a dedicated `mm_loadtest` database (`loadtest.load` -
# never the shared `mm` database other tests/worktrees use), reindexes it,
# analyzes `chunks`/`notes` (planner statistics `mm_frequent_lexemes`, #117,
# needs to bound full-text search for a very frequent term), starts one
# `memory-manager serve --http` directly - not via `uv run`, so
# `$SERVER_PID` below is the real process the EXIT trap kills - then, before
# any concurrency, times one search/read/write call sequentially against the
# idle server (#108 round 2) so a red k6 threshold can be told apart from
# latency that is already there with no load at all - then runs
# `loadtest/k6/smoke.js` in a container against it. k6's own exit code is
# this script's exit code: a red threshold is a FAIL, never silently
# retried or loosened.
set -euo pipefail
cd "$(dirname "$0")/.."

LOADTEST_NOTES="${LOADTEST_NOTES:-10000}"
LOADTEST_PORT="${LOADTEST_PORT:-18080}"
K6_IMAGE="${K6_IMAGE:-docker.io/grafana/k6:2.3.0}"

ADMIN_URL="${MM_TEST_DATABASE_URL:-postgresql://mm:mm@localhost:55432/mm}"
DB_NAME="mm_loadtest"
BASE_URL="http://127.0.0.1:${LOADTEST_PORT}/mcp"
DATABASE_URL="${ADMIN_URL%/*}/${DB_NAME}"

WORKDIR=$(mktemp -d)
SERVER_LOG="${WORKDIR}/server.log"
SERVER_PID=""

drop_database() {
  # `mm-pg`'s own `psql`, not a Python/asyncpg round trip - this runs from
  # the EXIT trap, where a half-broken venv must not stop the database from
  # being dropped. Only ever touches $DB_NAME - every other database on
  # `mm-pg` (including another worktree's own test databases) is untouched.
  podman exec mm-pg psql -U mm -d mm -v ON_ERROR_STOP=1 \
    -c "select pg_terminate_backend(pid) from pg_stat_activity where datname = '${DB_NAME}' and pid <> pg_backend_pid();" \
    -c "drop database if exists ${DB_NAME};" \
    >/dev/null 2>&1 || true
}

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  drop_database
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

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

echo "generating ${LOADTEST_NOTES} synthetic notes..."
.venv/bin/python -m loadtest.generate \
  --notes "$LOADTEST_NOTES" --users 200 --groups 20 --seed 1 \
  --out "${WORKDIR}/vault-out"

echo "loading the synthetic vault into ${DB_NAME}..."
.venv/bin/python -m loadtest.load \
  --vault "${WORKDIR}/vault-out" \
  --admin-url "$ADMIN_URL" \
  --db-name "$DB_NAME" \
  --base-url "$BASE_URL" \
  --context-out "${WORKDIR}/k6-context.json"

echo "reindexing ${DB_NAME}..."
STORAGE_BACKEND=postgres DATABASE_URL="$DATABASE_URL" EMBEDDING_PROVIDER=none \
  .venv/bin/memory-manager reindex --full

echo "analyzing ${DB_NAME} (chunks, notes) so mm_frequent_lexemes has fresh planner stats (#117)..."
podman exec mm-pg psql -U mm -d "$DB_NAME" -v ON_ERROR_STOP=1 -c "analyze chunks, notes;" >/dev/null

echo "starting memory-manager serve --http on 127.0.0.1:${LOADTEST_PORT}..."
STORAGE_BACKEND=postgres DATABASE_URL="$DATABASE_URL" \
  HOST=127.0.0.1 PORT="$LOADTEST_PORT" PUBLIC_URL="http://127.0.0.1:${LOADTEST_PORT}" \
  EMBEDDING_PROVIDER=none LOG_LEVEL=WARNING \
  RATE_LIMIT_MCP_PER_MINUTE=1000000 RATE_LIMIT_MCP_BURST=100000 \
  RATE_LIMIT_WRITE_PER_MINUTE=1000000 RATE_LIMIT_WRITE_BURST=100000 \
  .venv/bin/memory-manager serve --http >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

echo "waiting for GET /readyz..."
status=""
for _ in $(seq 1 60); do
  if status=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
      "http://127.0.0.1:${LOADTEST_PORT}/readyz" 2>/dev/null); then
    [[ "$status" == "200" ]] && break
  fi
  sleep 1
done
if [[ "$status" != "200" ]]; then
  echo "FAIL: GET /readyz did not return 200 (got ${status:-<none>})" >&2
  cat "$SERVER_LOG" >&2
  exit 1
fi
echo "OK: GET /readyz -> 200"

echo "isolated baseline (one sequential call each, idle server, no concurrency):"
# `--write-out '%{time_total}'` is curl's own wall-clock timer for the
# request - nothing k6-side, so this is a sanity check independent of the
# load tool: if this is already slow, the k6 thresholds below never had a
# chance regardless of load modelling.
BASELINE_TOKEN=$(jq -r '.tokens[0].token' "${WORKDIR}/k6-context.json")
BASELINE_READ_PATH=$(jq -r '.read_paths[0]' "${WORKDIR}/k6-context.json")
BASELINE_QUERY=$(head -n1 "${WORKDIR}/vault-out/queries.jsonl" | jq -r '.query')
BASELINE_WRITE_PATH="loadtest-writes/fact/baseline-$(date +%s).md"

mcp_call() {
  curl --silent --show-error --output /dev/null --write-out '%{time_total}' \
    --header 'Content-Type: application/json' \
    --header 'Accept: application/json, text/event-stream' \
    --header 'MCP-Protocol-Version: 2025-11-25' \
    --header "Authorization: Bearer ${BASELINE_TOKEN}" \
    --data "$1" \
    "$BASE_URL"
}

search_payload=$(jq -nc --arg q "$BASELINE_QUERY" \
  '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:"memory_search",arguments:{query:$q,limit:5}}}')
echo "  memory_search: $(mcp_call "$search_payload")s"

read_payload=$(jq -nc --arg p "$BASELINE_READ_PATH" \
  '{jsonrpc:"2.0",id:2,method:"tools/call",params:{name:"memory_read",arguments:{items:[$p]}}}')
echo "  memory_read:   $(mcp_call "$read_payload")s"

write_payload=$(jq -nc --arg p "$BASELINE_WRITE_PATH" \
  --arg c $'---\ntitle: Load test baseline\ndescription: Isolated single-call timing, safe to discard.\ntype: fact\n---\nbaseline\n' \
  '{jsonrpc:"2.0",id:3,method:"tools/call",params:{name:"memory_write",arguments:{path:$p,content:$c,if_version:"new"}}}')
echo "  memory_write:  $(mcp_call "$write_payload")s"

echo "running k6 scenarios against ${BASE_URL}..."
# k6's own image runs as a non-root, non-host uid (`k6`, see its Dockerfile) -
# `--userns=keep-id` maps it to the same uid/gid *numbers* this script runs
# as, but `$WORKDIR` itself is `mktemp -d`'s private `0700` and its files are
# the host user's default-umask ones, neither readable by that other uid.
# Nothing here is sensitive (synthetic notes, throwaway tokens scoped to
# $DB_NAME only), so this simply opens it up for the container to read.
chmod -R a+rX "$WORKDIR"

set +e
podman run --rm --network host --userns=keep-id \
  --env MM_CONTEXT_FILE=/data/k6-context.json \
  --env MM_QUERIES_FILE=/data/vault-out/queries.jsonl \
  --volume "$(pwd)/loadtest/k6:/scripts:ro,Z" \
  --volume "${WORKDIR}:/data:ro,Z" \
  "$K6_IMAGE" run /scripts/smoke.js
k6_exit=$?
set -e

if [[ "$k6_exit" -ne 0 ]]; then
  echo "FAIL: k6 exited ${k6_exit} - server log:" >&2
  cat "$SERVER_LOG" >&2
fi

exit "$k6_exit"
