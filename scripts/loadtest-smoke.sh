#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Loads a synthetic vault into the Postgres backend and runs the k6 search/
# read/write scenarios against `LOADTEST_REPLICAS` server replicas (#108,
# #124, #269, WP-21/WP-32): waits for `mm-pg` (and `mm-valkey`, if
# `LOADTEST_SHARED_STATE=valkey`), generates `LOADTEST_NOTES` notes
# (`loadtest.generate`, #107), bulk-loads them - under RLS, with every
# synthetic principal registered - into a dedicated `mm_loadtest` database
# (`loadtest.load` - never the shared `mm` database other tests/worktrees
# use), reindexes it (or, with `LOADTEST_EMBEDDINGS=stub`, fills `chunks`
# and its ADR-0016 vector index directly via `loadtest.load --with-chunks`
# instead - see that flag's own section below), analyzes `chunks`/`notes`
# (planner statistics `mm_frequent_lexemes`, #117, needs to bound full-text
# search for a very frequent term), starts `LOADTEST_REPLICAS` `memory-
# manager serve --http` processes directly - not via `uv run`, so
# `$SERVER_PIDS` below are the real processes the EXIT trap kills - each
# with `DATABASE_APP_ROLE` set (ADR-0008 addendum, #116: `serve` refuses
# postgres mode without it, and runs `db.rls.check_app_role`/
# `grant_app_role` against it at startup - `loadtest.load`'s own
# `ensure_app_role` already made sure the role exists and the owner is a
# member of it) - then, before any concurrency, times one search/read/write
# call sequentially against the first replica, still idle (#108 round 2) so
# a red k6 threshold can be told apart from latency that is already there
# with no load at all - then runs `loadtest/k6/smoke.js` in a container
# against every replica's own base URL (`loadtest/k6/lib.js`'s own round
# robin, #269 - no local load balancer is needed). k6's own exit code is
# this script's exit code: a red threshold is a FAIL, never silently
# retried or loosened.
#
# Four opt-in switches, all no-ops at their default (#109, #269, WP-21/
# WP-32):
# - `LOADTEST_RLS_VARIANT` (`r2`, default, or `r1`): `r1` applies
#   `loadtest/r1_approximation.sql` to the freshly analyzed `mm_loadtest`
#   database, right before the servers start - the DB-level approximation
#   of ADR-0008's R1 (owner decision 2026-10-07, #109's own issue body).
# - `LOADTEST_RESULTS_DIR`: if set, a writable directory this script mounts
#   into the k6 container for `--summary-export` (with
#   `--summary-trend-stats` widened to also carry p99/max) and where it
#   writes `pg_stat_user_functions` plus this run's own step/isolated-call
#   timings (now also the replica count, kill delay, shared-state mode and
#   embedding mode, #269) once the measured run is done, before the EXIT
#   trap tears the database and every server down. Left unset, neither file
#   is written and k6 runs exactly as before.
# - `LOADTEST_REPLICAS` (default 1): how many `serve --http` processes to
#   start, one per port starting at `LOADTEST_PORT`. `1` behaves exactly
#   like before this was added - one replica, no round robin to speak of.
# - `LOADTEST_KILL_AFTER` (default unset = no kill): seconds into the
#   *measured* k6 phase (after `MM_WARMUP_DURATION`, which this script now
#   always sets explicitly - see below) at which the last-started replica
#   is sent `SIGKILL` (not a graceful shutdown - that is `tests/
#   test_shutdown.py`'s own scenario), mirroring `tests/e2e/
#   test_replicas.py`'s own kill. Requires `LOADTEST_REPLICAS >= 2` (one
#   replica must keep answering after the kill). `loadtest/k6/lib.js`'s own
#   per-VU circuit breaker (#269) is what keeps the killed replica's lost
#   traffic down to the handful of requests already in flight to it,
#   rather than its full round-robin share for whatever is left of the run -
#   without that, `http_req_failed{phase:measure}: rate<0.01` could never
#   hold with a replica gone for any real fraction of the measured phase.
# - `LOADTEST_SHARED_STATE` (`postgres`, default, or `valkey`): which
#   `auth.shared_state.SharedState` backend every replica shares rate-limit
#   windows and pending-login state through (ADR-0009 §2). `postgres` needs
#   nothing extra - every replica's own `DATABASE_URL` already points at
#   the same database, so `PostgresSharedState` is already shared across
#   them. `valkey` additionally waits for `mm-valkey` and sets `VALKEY_URL`
#   on every replica (`make valkey-up` starts it; this script never does).
# - `LOADTEST_EMBEDDINGS` (`none`, default, or `stub`): `none` keeps today's
#   behaviour exactly - `EMBEDDING_PROVIDER=none` everywhere, `reindex
#   --full`, no vector data, `loadtest/k6/smoke.js`'s own `search_vector_only`
#   scenario (#269, #266) still runs but against plain full-text, which a
#   lexical-marker-free vector-only query was built to never match - a
#   latency sample with an empty result, not a failure. `stub` instead
#   loads `chunks` and the ADR-0016 HNSW index directly (`loadtest.load
#   --with-chunks`, #267 - skipping `reindex --full`, which would otherwise
#   re-embed every chunk through the real indexing path and overwrite
#   `--with-chunks`' own `vector_key`-keyed vectors with ones keyed by chunk
#   text instead, #268's own embedding stub (`loadtest.embedding_stub`)
#   serves `EMBEDDING_URL` for query-time embedding, and one `memory-manager
#   worker` process (#217) runs alongside the replicas so the `write`
#   scenario's own new/edited notes still get their chunks embedded (the
#   `embed_note` job the write path enqueues, #218/#219, never runs
#   otherwise - no replica itself claims jobs).
set -euo pipefail
cd "$(dirname "$0")/.."

LOADTEST_NOTES="${LOADTEST_NOTES:-10000}"
LOADTEST_PORT="${LOADTEST_PORT:-18080}"
K6_IMAGE="${K6_IMAGE:-docker.io/grafana/k6:2.3.0}"
LOADTEST_RLS_VARIANT="${LOADTEST_RLS_VARIANT:-r2}"
LOADTEST_RESULTS_DIR="${LOADTEST_RESULTS_DIR:-}"
LOADTEST_REPLICAS="${LOADTEST_REPLICAS:-1}"
LOADTEST_KILL_AFTER="${LOADTEST_KILL_AFTER:-}"
LOADTEST_SHARED_STATE="${LOADTEST_SHARED_STATE:-postgres}"
LOADTEST_EMBEDDINGS="${LOADTEST_EMBEDDINGS:-none}"

# One source for the embedding model name (#291): `loadtest.load
# --with-chunks` stamps this onto every chunk's own `model` column, and the
# `serve --http`/worker replicas below query with this same value as their
# own `EMBEDDING_MODEL` - a mismatch between the two (as this script used to
# have, loader hardcoded, server env independently set) makes every vector
# leg filter on a `model` no stored chunk carries, finding nothing. Only
# used when `LOADTEST_EMBEDDINGS=stub`, same as everywhere else it appears
# below.
EMBEDDING_MODEL_VALUE="loadtest-stub"

case "$LOADTEST_RLS_VARIANT" in
  r2|r1) ;;
  *)
    echo "FAIL: LOADTEST_RLS_VARIANT must be 'r2' or 'r1', got '${LOADTEST_RLS_VARIANT}'" >&2
    exit 1
    ;;
esac

case "$LOADTEST_SHARED_STATE" in
  postgres|valkey) ;;
  *)
    echo "FAIL: LOADTEST_SHARED_STATE must be 'postgres' or 'valkey', got '${LOADTEST_SHARED_STATE}'" >&2
    exit 1
    ;;
esac

case "$LOADTEST_EMBEDDINGS" in
  none|stub) ;;
  *)
    echo "FAIL: LOADTEST_EMBEDDINGS must be 'none' or 'stub', got '${LOADTEST_EMBEDDINGS}'" >&2
    exit 1
    ;;
esac

if ! [[ "$LOADTEST_REPLICAS" =~ ^[0-9]+$ ]] || [[ "$LOADTEST_REPLICAS" -lt 1 ]]; then
  echo "FAIL: LOADTEST_REPLICAS must be a positive integer, got '${LOADTEST_REPLICAS}'" >&2
  exit 1
fi

if [[ -n "$LOADTEST_KILL_AFTER" ]]; then
  if ! [[ "$LOADTEST_KILL_AFTER" =~ ^[0-9]+$ ]]; then
    echo "FAIL: LOADTEST_KILL_AFTER must be a non-negative integer (seconds), got '${LOADTEST_KILL_AFTER}'" >&2
    exit 1
  fi
  if [[ "$LOADTEST_REPLICAS" -lt 2 ]]; then
    echo "FAIL: LOADTEST_KILL_AFTER needs LOADTEST_REPLICAS >= 2 (one replica must keep serving after the kill)" >&2
    exit 1
  fi
fi

ADMIN_URL="${MM_TEST_DATABASE_URL:-postgresql://mm:mm@localhost:55432/mm}"
DB_NAME="mm_loadtest"
APP_ROLE="mm_loadtest_app"
BASE_URL="http://127.0.0.1:${LOADTEST_PORT}/mcp"  # replica 0 - the isolated baseline's own target
DATABASE_URL="${ADMIN_URL%/*}/${DB_NAME}"

# Fixed, documented offsets from `LOADTEST_PORT` (never a second,
# independently-configurable port knob this issue never asked for) - large
# enough that even a generous `LOADTEST_REPLICAS` never collides with the
# embedding stub's or worker's own port.
EMBEDDING_STUB_PORT=$((LOADTEST_PORT + 1000))
WORKER_PORT=$((LOADTEST_PORT + 2000))

# `loadtest/k6/smoke.js`'s own default (10s) - set explicitly here (not left
# implicit) so this script's own kill-delay arithmetic below never silently
# drifts from the value k6 actually uses.
WARMUP_SECONDS=10

WORKDIR=$(mktemp -d)
SERVER_PIDS=()
SERVER_LOGS=()
STUB_PID=""
WORKER_PID=""

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
  for pid in "${SERVER_PIDS[@]}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  if [[ -n "$WORKER_PID" ]] && kill -0 "$WORKER_PID" 2>/dev/null; then
    kill "$WORKER_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
  fi
  if [[ -n "$STUB_PID" ]] && kill -0 "$STUB_PID" 2>/dev/null; then
    kill "$STUB_PID" 2>/dev/null || true
    wait "$STUB_PID" 2>/dev/null || true
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

VALKEY_URL_VALUE=""
if [[ "$LOADTEST_SHARED_STATE" == "valkey" ]]; then
  echo "waiting for mm-valkey..."
  valkey_ready=0
  for _ in $(seq 1 30); do
    if podman exec mm-valkey valkey-cli ping >/dev/null 2>&1; then
      valkey_ready=1
      break
    fi
    sleep 1
  done
  if [[ "$valkey_ready" -ne 1 ]]; then
    echo "FAIL: mm-valkey did not become ready (see 'make valkey-up')" >&2
    exit 1
  fi
  VALKEY_URL_VALUE="redis://127.0.0.1:6379"
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

# One base URL per replica (#269) - `loadtest.load` writes the whole list
# into the k6 context file; `loadtest/k6/lib.js` round-robins over it.
BASE_URL_ARGS=()
for ((i = 0; i < LOADTEST_REPLICAS; i++)); do
  BASE_URL_ARGS+=(--base-url "http://127.0.0.1:$((LOADTEST_PORT + i))/mcp")
done

LOAD_ARGS=(
  --vault "${WORKDIR}/vault-out"
  --admin-url "$ADMIN_URL"
  --db-name "$DB_NAME"
  --app-role "$APP_ROLE"
  --context-out "${WORKDIR}/k6-context.json"
  # `loadtest.load`'s own default (50 of 200 personal namespaces, #291
  # rework round 2): too few of `queries.jsonl`'s own marker notes land in
  # a namespace one of those 50 tokens can actually read (`loadtest.
  # generate`'s Zipf-weighted namespace picker makes most personal
  # namespaces own only a handful of notes each) to reliably draw >=50
  # *visible* lexical and >=50 *visible* vector-only queries for `search.
  # js`'s own deterministic correctness scenarios below - measured at only
  # 28 visible vector-only queries out of 80 total with 50 tokens, against
  # 77 with every personal namespace tokened. `200` (every personal
  # namespace) makes every marker note's own namespace visible to *some*
  # token, so visibility is a property of the vault, not of which 50
  # aliases happened to get sampled.
  --tokens 200
)
LOAD_ARGS+=("${BASE_URL_ARGS[@]}")
if [[ "$LOADTEST_EMBEDDINGS" == "stub" ]]; then
  LOAD_ARGS+=(
    --with-chunks --results-out "${WORKDIR}/chunk-results.json"
    --embedding-model "$EMBEDDING_MODEL_VALUE"
  )
fi

LOAD_START=$(date +%s.%N)
echo "loading the synthetic vault into ${DB_NAME}..."
.venv/bin/python -m loadtest.load "${LOAD_ARGS[@]}"
LOAD_END=$(date +%s.%N)

REINDEX_START=$(date +%s.%N)
if [[ "$LOADTEST_EMBEDDINGS" == "stub" ]]; then
  # `loadtest.load --with-chunks` (#267) already bulk-inserted `notes` and
  # `chunks`, vectors included - a `reindex --full` here would only re-embed
  # every chunk through the real indexing path (against the stub, #268),
  # overwriting those `vector_key`-keyed vectors with ones keyed by chunk
  # text instead, for no benefit at this script's own scale.
  echo "skipping reindex --full: loadtest.load --with-chunks already populated notes/chunks (#267)"
else
  echo "reindexing ${DB_NAME}..."
  STORAGE_BACKEND=postgres DATABASE_URL="$DATABASE_URL" EMBEDDING_PROVIDER=none \
    .venv/bin/memory-manager reindex --full
fi
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

# `EMBEDDING_ENV`/`SHARED_STATE_ENV` are assembled once and spread into every
# replica's own `env` call below (and, for the embedding half, the worker's
# too) - never duplicated per replica.
EMBEDDING_ENV=(EMBEDDING_PROVIDER=none)
if [[ "$LOADTEST_EMBEDDINGS" == "stub" ]]; then
  EMBEDDING_URL_VALUE="http://127.0.0.1:${EMBEDDING_STUB_PORT}/v1"
  EMBEDDING_ENV=(
    EMBEDDING_PROVIDER=openai
    EMBEDDING_URL="$EMBEDDING_URL_VALUE"
    EMBEDDING_MODEL="$EMBEDDING_MODEL_VALUE"
    EMBEDDING_DIMENSIONS=1024
  )

  echo "starting the embedding stub (#268) on 127.0.0.1:${EMBEDDING_STUB_PORT}..."
  .venv/bin/python -m loadtest.embedding_stub \
    --queries "${WORKDIR}/vault-out/queries.jsonl" --port "$EMBEDDING_STUB_PORT" --dimension 1024 \
    >"${WORKDIR}/embedding-stub.log" 2>&1 &
  STUB_PID=$!

  echo "waiting for the embedding stub's GET /healthz..."
  stub_status=""
  for _ in $(seq 1 30); do
    if stub_status=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
        "http://127.0.0.1:${EMBEDDING_STUB_PORT}/healthz" 2>/dev/null); then
      [[ "$stub_status" == "200" ]] && break
    fi
    sleep 1
  done
  if [[ "$stub_status" != "200" ]]; then
    echo "FAIL: embedding stub GET /healthz did not return 200 (got ${stub_status:-<none>})" >&2
    cat "${WORKDIR}/embedding-stub.log" >&2
    exit 1
  fi
  echo "OK: embedding stub GET /healthz -> 200"

  # `memory-manager worker` (#217) connects as the owner, like `reindex`
  # above - never `DATABASE_APP_ROLE` - and is the only process that ever
  # claims the `embed_note` jobs (#218/#219) the write scenario's own new/
  # edited notes enqueue.
  echo "starting one memory-manager worker on 127.0.0.1:${WORKER_PORT} against ${EMBEDDING_URL_VALUE}..."
  env STORAGE_BACKEND=postgres DATABASE_URL="$DATABASE_URL" "${EMBEDDING_ENV[@]}" \
    WORKER_PORT="$WORKER_PORT" LOG_LEVEL=WARNING \
    .venv/bin/memory-manager worker >"${WORKDIR}/worker.log" 2>&1 &
  WORKER_PID=$!

  echo "waiting for the worker's GET /readyz..."
  worker_status=""
  for _ in $(seq 1 30); do
    if worker_status=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
        "http://127.0.0.1:${WORKER_PORT}/readyz" 2>/dev/null); then
      [[ "$worker_status" == "200" ]] && break
    fi
    sleep 1
  done
  if [[ "$worker_status" != "200" ]]; then
    echo "FAIL: worker GET /readyz did not return 200 (got ${worker_status:-<none>})" >&2
    cat "${WORKDIR}/worker.log" >&2
    exit 1
  fi
  echo "OK: worker GET /readyz -> 200"
fi

for ((i = 0; i < LOADTEST_REPLICAS; i++)); do
  port=$((LOADTEST_PORT + i))
  log="${WORKDIR}/server-${i}.log"
  SERVER_LOGS+=("$log")
  echo "starting memory-manager serve --http replica ${i} on 127.0.0.1:${port}..."
  SERVER_ENV=(
    STORAGE_BACKEND=postgres
    DATABASE_URL="$DATABASE_URL"
    DATABASE_APP_ROLE="$APP_ROLE"
    HOST=127.0.0.1
    PORT="$port"
    PUBLIC_URL="http://127.0.0.1:${port}"
    LOG_LEVEL=WARNING
    # Generous enough that the load test's own call rates (search/read/
    # write, loadtest/k6/smoke.js) never trip a rate limit - this is a
    # latency/survival smoke test, never a 429 test.
    RATE_LIMIT_MCP_PER_MINUTE=1000000 RATE_LIMIT_MCP_BURST=100000
    RATE_LIMIT_WRITE_PER_MINUTE=1000000 RATE_LIMIT_WRITE_BURST=100000
  )
  SERVER_ENV+=("${EMBEDDING_ENV[@]}")
  if [[ -n "$VALKEY_URL_VALUE" ]]; then
    SERVER_ENV+=(VALKEY_URL="$VALKEY_URL_VALUE")
  fi
  env "${SERVER_ENV[@]}" .venv/bin/memory-manager serve --http >"$log" 2>&1 &
  SERVER_PIDS+=("$!")
done

for ((i = 0; i < LOADTEST_REPLICAS; i++)); do
  port=$((LOADTEST_PORT + i))
  echo "waiting for replica ${i} GET /readyz..."
  status=""
  for _ in $(seq 1 60); do
    if status=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
        "http://127.0.0.1:${port}/readyz" 2>/dev/null); then
      [[ "$status" == "200" ]] && break
    fi
    sleep 1
  done
  if [[ "$status" != "200" ]]; then
    echo "FAIL: replica ${i} GET /readyz did not return 200 (got ${status:-<none>})" >&2
    cat "${SERVER_LOGS[$i]}" >&2
    exit 1
  fi
  echo "OK: replica ${i} GET /readyz -> 200"
done

echo "isolated baseline (one sequential call each, against replica 0, no concurrency):"
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

# `MM_WARMUP_DURATION` is now always set explicitly (`WARMUP_SECONDS` above),
# rather than left to `loadtest/k6/smoke.js`'s own default - the kill-delay
# arithmetic below needs to know exactly what k6 uses, not assume it stays
# in sync with a literal duplicated here. `MM_MEASURE_DURATION` keeps its
# own pre-existing, independent forwarding (`loadtest/k6/smoke.js`'s own
# default `60s` unless overridden) - unset, the container's own default
# applies, same behaviour as before this was added.
PODMAN_RUN_ARGS=(
  run --rm --network host --userns=keep-id
  --env MM_CONTEXT_FILE=/data/k6-context.json
  --env MM_QUERIES_FILE=/data/vault-out/queries.jsonl
  --env "MM_WARMUP_DURATION=${WARMUP_SECONDS}s"
  --env "MM_MEASURE_DURATION=${MM_MEASURE_DURATION:-60s}"
  # `#291` rework round 2: `smoke.js` only enables the deterministic
  # vector-only correctness scenario/gate (and its own `search_correctness_
  # hit_rate{check:vector}`/`...samples{check:vector}` thresholds) when this
  # is `stub` - `none` has no vector capability at all (`EMBEDDING_
  # PROVIDER=none`), so a vector-only query structurally never matches and
  # gating on it would fail every run regardless of search correctness,
  # breaking this script's own "every switch is a no-op at its default"
  # promise for `LOADTEST_EMBEDDINGS=none`. The lexical correctness
  # scenario/gate runs unconditionally in both modes.
  --env "MM_EMBEDDINGS_MODE=${LOADTEST_EMBEDDINGS}"
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

echo "running k6 scenarios against ${LOADTEST_REPLICAS} replica(s) starting at 127.0.0.1:${LOADTEST_PORT}..."
set +e
if [[ -n "$LOADTEST_KILL_AFTER" ]]; then
  kill_index=$((LOADTEST_REPLICAS - 1))
  kill_pid="${SERVER_PIDS[$kill_index]}"
  kill_delay=$((WARMUP_SECONDS + LOADTEST_KILL_AFTER))

  podman "${PODMAN_RUN_ARGS[@]}" "$K6_IMAGE" "${K6_RUN_ARGS[@]}" &
  k6_bg_pid=$!

  (
    sleep "$kill_delay"
    if kill -0 "$kill_pid" 2>/dev/null; then
      kill -9 "$kill_pid" 2>/dev/null || true
      echo "killed replica ${kill_index} (pid ${kill_pid}, SIGKILL) ${kill_delay}s into the k6 run (#269)"
    fi
  ) &
  killer_bg_pid=$!

  wait "$k6_bg_pid"
  k6_exit=$?
  wait "$killer_bg_pid" 2>/dev/null || true
else
  podman "${PODMAN_RUN_ARGS[@]}" "$K6_IMAGE" "${K6_RUN_ARGS[@]}"
  k6_exit=$?
fi
set -e

if [[ -n "$LOADTEST_RESULTS_DIR" ]]; then
  # Written unconditionally on `k6_exit`, including non-zero (#109: "a red
  # threshold at 100k notes is a result, not an abort" - k6 still writes
  # `--summary-export` on a red threshold, and so does this).
  echo "writing pg_stat_user_functions and run metadata to ${LOADTEST_RESULTS_DIR} (#109, #269)..."
  podman exec mm-pg psql -U mm -d "$DB_NAME" -v ON_ERROR_STOP=1 \
    -c "copy (select funcname, calls, total_time, self_time from pg_stat_user_functions order by funcname) to stdout with csv header" \
    > "${LOADTEST_RESULTS_DIR}/pg_stat_user_functions.csv"
  if [[ -f "${WORKDIR}/chunk-results.json" ]]; then
    cp "${WORKDIR}/chunk-results.json" "${LOADTEST_RESULTS_DIR}/chunk-results.json"
  fi
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
    --argjson replicas "$LOADTEST_REPLICAS" \
    --argjson kill_after "${LOADTEST_KILL_AFTER:-null}" \
    --arg shared_state "$LOADTEST_SHARED_STATE" \
    --arg embeddings "$LOADTEST_EMBEDDINGS" \
    --argjson rate_limit_mcp_per_minute 1000000 \
    --argjson rate_limit_mcp_burst 100000 \
    --argjson rate_limit_write_per_minute 1000000 \
    --argjson rate_limit_write_burst 100000 \
    '{
      rls_variant: $variant,
      notes: $notes,
      replicas: $replicas,
      kill_after_seconds: $kill_after,
      shared_state: $shared_state,
      embeddings: $embeddings,
      rate_limits: {
        mcp_per_minute: $rate_limit_mcp_per_minute,
        mcp_burst: $rate_limit_mcp_burst,
        write_per_minute: $rate_limit_write_per_minute,
        write_burst: $rate_limit_write_burst
      },
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
  echo "FAIL: k6 exited ${k6_exit} - server logs:" >&2
  for log in "${SERVER_LOGS[@]}"; do
    echo "--- ${log} ---" >&2
    cat "$log" >&2
  done
fi

exit "$k6_exit"
