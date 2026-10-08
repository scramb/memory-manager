#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Loads a synthetic vault into the Postgres backend and runs the k6 search/
# read/write scenarios against one server replica (#108, #124, WP-21): waits
# for `mm-pg`, generates `LOADTEST_NOTES` notes (`loadtest.generate`, #107),
# bulk-loads them - under RLS, with every synthetic principal registered -
# into a dedicated `mm_loadtest` database (`loadtest.load` - never the
# shared `mm` database other tests/worktrees use), reindexes it, analyzes
# `chunks`/`notes` (planner statistics `mm_frequent_lexemes`, #117, needs to
# bound full-text search for a very frequent term), starts one
# `memory-manager serve --http` directly - not via `uv run`, so
# `$SERVER_PID` below is the real process the EXIT trap kills - with
# `DATABASE_APP_ROLE` set (ADR-0008 addendum, #116: `serve` refuses postgres
# mode without it, and runs `db.rls.check_app_role`/`grant_app_role` against
# it at startup - `loadtest.load`'s own `ensure_app_role` already made sure
# the role exists and the owner is a member of it) - then, before any
# concurrency, times one search/read/write call sequentially against the
# idle server (#108 round 2) so a red k6 threshold can be told apart from
# latency that is already there with no load at all - then runs
# `loadtest/k6/smoke.js` in a container against it. k6's own exit code is
# this script's exit code: a red threshold is a FAIL, never silently
# retried or loosened.
#
# Two opt-in switches, both no-ops at their default (#109, WP-21):
# - `LOADTEST_RLS_VARIANT` (`r2`, default, or `r1`): `r1` applies
#   `loadtest/r1_approximation.sql` to the freshly analyzed `mm_loadtest`
#   database, right before the server starts - the DB-level approximation
#   of ADR-0008's R1 (owner decision 2026-10-07, #109's own issue body).
# - `LOADTEST_RESULTS_DIR`: if set, a writable directory this script mounts
#   into the k6 container for `--summary-export` (with
#   `--summary-trend-stats` widened to also carry p99/max) and where it
#   writes `pg_stat_user_functions` plus this run's own step/isolated-call
#   timings once the measured run is done, before the EXIT trap tears the
#   database and server down. Left unset, neither file is written and k6
#   runs exactly as before.
set -euo pipefail
cd "$(dirname "$0")/.."

LOADTEST_NOTES="${LOADTEST_NOTES:-10000}"
LOADTEST_PORT="${LOADTEST_PORT:-18080}"
K6_IMAGE="${K6_IMAGE:-docker.io/grafana/k6:2.3.0}"
LOADTEST_RLS_VARIANT="${LOADTEST_RLS_VARIANT:-r2}"
LOADTEST_RESULTS_DIR="${LOADTEST_RESULTS_DIR:-}"

case "$LOADTEST_RLS_VARIANT" in
  r2|r1) ;;
  *)
    echo "FAIL: LOADTEST_RLS_VARIANT must be 'r2' or 'r1', got '${LOADTEST_RLS_VARIANT}'" >&2
    exit 1
    ;;
esac

ADMIN_URL="${MM_TEST_DATABASE_URL:-postgresql://mm:mm@localhost:55432/mm}"
DB_NAME="mm_loadtest"
APP_ROLE="mm_loadtest_app"
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

drop_app_role() {
  # Only safe once $DB_NAME is gone: the role's own table/function grants
  # (`db.rls.grant_app_role`, run by `serve` at startup) live inside that
  # database and would otherwise still make it a dependent object.
  podman exec mm-pg psql -U mm -d mm -v ON_ERROR_STOP=1 \
    -c "drop role if exists ${APP_ROLE};" \
    >/dev/null 2>&1 || true
}

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  drop_database
  drop_app_role
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

# Step timings (#109, WP-21): wall-clock seconds for generate/load/reindex,
# via `date +%s.%N` - only ever reported, never gated on, so a slow step
# here is a number in `LOADTEST_RESULTS_DIR`, not a script failure.
GENERATE_START=$(date +%s.%N)
echo "generating ${LOADTEST_NOTES} synthetic notes..."
.venv/bin/python -m loadtest.generate \
  --notes "$LOADTEST_NOTES" --users 200 --groups 20 --seed 1 \
  --out "${WORKDIR}/vault-out"
GENERATE_END=$(date +%s.%N)

LOAD_START=$(date +%s.%N)
echo "loading the synthetic vault into ${DB_NAME}..."
.venv/bin/python -m loadtest.load \
  --vault "${WORKDIR}/vault-out" \
  --admin-url "$ADMIN_URL" \
  --db-name "$DB_NAME" \
  --app-role "$APP_ROLE" \
  --base-url "$BASE_URL" \
  --context-out "${WORKDIR}/k6-context.json"
LOAD_END=$(date +%s.%N)

REINDEX_START=$(date +%s.%N)
echo "reindexing ${DB_NAME}..."
STORAGE_BACKEND=postgres DATABASE_URL="$DATABASE_URL" EMBEDDING_PROVIDER=none \
  .venv/bin/memory-manager reindex --full
REINDEX_END=$(date +%s.%N)

echo "analyzing ${DB_NAME} (chunks, notes) so mm_frequent_lexemes has fresh planner stats (#117)..."
podman exec mm-pg psql -U mm -d "$DB_NAME" -v ON_ERROR_STOP=1 -c "analyze chunks, notes;" >/dev/null

if [[ "$LOADTEST_RLS_VARIANT" == "r1" ]]; then
  echo "applying the R1 approximation (loadtest/r1_approximation.sql, #109) to ${DB_NAME}..."
  podman exec -i mm-pg psql -U mm -d "$DB_NAME" -v ON_ERROR_STOP=1 \
    < loadtest/r1_approximation.sql >/dev/null
fi

echo "enabling track_functions=all on ${DB_NAME} (pg_stat_user_functions, #109)..."
podman exec mm-pg psql -U mm -d "$DB_NAME" -v ON_ERROR_STOP=1 \
  -c "alter database \"${DB_NAME}\" set track_functions = 'all';" >/dev/null

echo "starting memory-manager serve --http on 127.0.0.1:${LOADTEST_PORT}..."
STORAGE_BACKEND=postgres DATABASE_URL="$DATABASE_URL" DATABASE_APP_ROLE="$APP_ROLE" \
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
BASELINE_READ_PATH=$(jq -r '.tokens[0].read_paths[0]' "${WORKDIR}/k6-context.json")
BASELINE_QUERY=$(head -n1 "${WORKDIR}/vault-out/queries.jsonl" | jq -r '.query')
BASELINE_WRITE_PATH="me/fact/baseline-$(date +%s).md"

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
BASELINE_SEARCH_S=$(mcp_call "$search_payload")
echo "  memory_search: ${BASELINE_SEARCH_S}s"

read_payload=$(jq -nc --arg p "$BASELINE_READ_PATH" \
  '{jsonrpc:"2.0",id:2,method:"tools/call",params:{name:"memory_read",arguments:{items:[$p]}}}')
BASELINE_READ_S=$(mcp_call "$read_payload")
echo "  memory_read:   ${BASELINE_READ_S}s"

write_payload=$(jq -nc --arg p "$BASELINE_WRITE_PATH" \
  --arg c $'---\ntitle: Load test baseline\ndescription: Isolated single-call timing, safe to discard.\ntype: fact\n---\nbaseline\n' \
  '{jsonrpc:"2.0",id:3,method:"tools/call",params:{name:"memory_write",arguments:{path:$p,content:$c,if_version:"new"}}}')
BASELINE_WRITE_S=$(mcp_call "$write_payload")
echo "  memory_write:  ${BASELINE_WRITE_S}s"

# k6's own image runs as a non-root, non-host uid (`k6`, see its Dockerfile) -
# `--userns=keep-id` maps it to the same uid/gid *numbers* this script runs
# as, but `$WORKDIR` itself is `mktemp -d`'s private `0700` and its files are
# the host user's default-umask ones, neither readable by that other uid.
# Nothing here is sensitive (synthetic notes, throwaway tokens scoped to
# $DB_NAME only), so this simply opens it up for the container to read.
chmod -R a+rX "$WORKDIR"

# `MM_MEASURE_DURATION` (`loadtest/k6/smoke.js`'s own default `60s`) is
# forwarded into the container unconditionally - unset, the container's own
# default applies, same behaviour as before this was added.
PODMAN_RUN_ARGS=(
  run --rm --network host --userns=keep-id
  --env MM_CONTEXT_FILE=/data/k6-context.json
  --env MM_QUERIES_FILE=/data/vault-out/queries.jsonl
  --env "MM_MEASURE_DURATION=${MM_MEASURE_DURATION:-60s}"
  --volume "$(pwd)/loadtest/k6:/scripts:ro,Z"
  --volume "${WORKDIR}:/data:ro,Z"
)
K6_RUN_ARGS=(run)
if [[ -n "$LOADTEST_RESULTS_DIR" ]]; then
  mkdir -p "$LOADTEST_RESULTS_DIR"
  # Same reasoning as `$WORKDIR`'s own `chmod -R a+rX` above: the k6
  # container's own uid (baked into its image, not remapped to the host
  # uid by `--userns=keep-id`) is neither this directory's owner nor its
  # group, only a supplementary group member - without this, `k6` can read
  # `$WORKDIR` fine but `--summary-export` into `$LOADTEST_RESULTS_DIR`
  # fails with EACCES. Nothing written here is sensitive (a latency
  # summary and function-call counts), so opening it up is the same
  # trade-off `$WORKDIR` already makes.
  chmod 777 "$LOADTEST_RESULTS_DIR"
  PODMAN_RUN_ARGS+=(--volume "${LOADTEST_RESULTS_DIR}:/results:rw,Z")
  # p99/max alongside the trend stats smoke.js's own thresholds already use
  # (avg/med/p95) - the baseline (#109) reports p50/p95, this just keeps the
  # export from silently dropping the extra percentiles later analysis asks
  # for.
  K6_RUN_ARGS+=(--summary-trend-stats 'avg,med,p(95),p(99),max')
  K6_RUN_ARGS+=(--summary-export=/results/summary.json)
fi
K6_RUN_ARGS+=(/scripts/smoke.js)

echo "running k6 scenarios against ${BASE_URL}..."
set +e
podman "${PODMAN_RUN_ARGS[@]}" "$K6_IMAGE" "${K6_RUN_ARGS[@]}"
k6_exit=$?
set -e

if [[ -n "$LOADTEST_RESULTS_DIR" ]]; then
  # Written unconditionally on `k6_exit`, including non-zero (#109: "a red
  # threshold at 100k notes is a result, not an abort" - k6 still writes
  # `--summary-export` on a red threshold, and so does this).
  echo "writing pg_stat_user_functions and run metadata to ${LOADTEST_RESULTS_DIR} (#109)..."
  podman exec mm-pg psql -U mm -d "$DB_NAME" -v ON_ERROR_STOP=1 \
    -c "copy (select funcname, calls, total_time, self_time from pg_stat_user_functions order by funcname) to stdout with csv header" \
    > "${LOADTEST_RESULTS_DIR}/pg_stat_user_functions.csv"
  jq -n \
    --arg variant "$LOADTEST_RLS_VARIANT" \
    --argjson notes "$LOADTEST_NOTES" \
    --argjson generate_s "$(awk "BEGIN { print ${GENERATE_END} - ${GENERATE_START} }")" \
    --argjson load_s "$(awk "BEGIN { print ${LOAD_END} - ${LOAD_START} }")" \
    --argjson reindex_s "$(awk "BEGIN { print ${REINDEX_END} - ${REINDEX_START} }")" \
    --argjson baseline_search_s "$BASELINE_SEARCH_S" \
    --argjson baseline_read_s "$BASELINE_READ_S" \
    --argjson baseline_write_s "$BASELINE_WRITE_S" \
    --argjson k6_exit "$k6_exit" \
    '{
      rls_variant: $variant,
      notes: $notes,
      step_seconds: {generate: $generate_s, load: $load_s, reindex: $reindex_s},
      isolated_baseline_seconds: {
        search: $baseline_search_s,
        read: $baseline_read_s,
        write: $baseline_write_s
      },
      k6_exit: $k6_exit
    }' > "${LOADTEST_RESULTS_DIR}/timings.json"
fi

if [[ "$k6_exit" -ne 0 ]]; then
  echo "FAIL: k6 exited ${k6_exit} - server log:" >&2
  cat "$SERVER_LOG" >&2
fi

exit "$k6_exit"
