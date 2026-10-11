# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Shared container-engine detection, /healthz polling, MCP tool calls and
# throwaway-database helpers for the upgrade smoke tests (#202, WP-59),
# factored out of scripts/upgrade-smoke.sh so scripts/upgrade-smoke-1.0.sh
# does not duplicate them - both scripts source this file and keep their
# own upgrade path (which images, which env, which notes) to themselves.
#
# Sourced, never executed directly - every function below reads or writes
# plain globals in the *caller's* shell (ENGINE above all - every function
# that shells out to a container engine uses it) and assumes the caller
# already has `set -euo pipefail` active and its own cleanup trap for the
# resources it creates (a container, a database). The admin connection for
# every `mm_pg_*` function below is always `podman exec mm-pg psql -U mm`
# (`make db-up`'s own container/role) - these two scripts are local/CI-only
# tooling against that fixed container, not a general Postgres helper.

# Sets ENGINE ("podman" or "docker") - CONTAINER_ENGINE overrides detection,
# podman preferred when both are present (this is a podman-first node,
# CLAUDE.md "Umgebung"); docker is still accepted so a CI runner that only
# has docker keeps working.
mm_detect_engine() {
  if [[ -n "${CONTAINER_ENGINE:-}" ]]; then
    ENGINE="$CONTAINER_ENGINE"
  elif command -v podman >/dev/null 2>&1; then
    ENGINE=podman
  elif command -v docker >/dev/null 2>&1; then
    ENGINE=docker
  else
    echo "FAIL: neither podman nor docker found on PATH" >&2
    return 1
  fi
}

# Polls "http://127.0.0.1:$1/healthz" for up to 30s, printing $ENGINE's logs
# for $2 (the container name) and failing if it never returns 200.
mm_wait_healthz() {
  local port="$1" container_name="$2"
  local status=""
  for _ in $(seq 1 30); do
    if status=$(curl --silent --show-error --output /dev/null \
        --write-out '%{http_code}' "http://127.0.0.1:${port}/healthz" 2>/dev/null); then
      [[ "$status" == "200" ]] && return 0
    fi
    sleep 1
  done
  echo "FAIL: GET /healthz did not return 200 within 30s (got ${status:-<none>})" >&2
  "$ENGINE" logs "$container_name" >&2 || true
  return 1
}

mm_mcp_body() {
  # $1 tool name, $2 arguments (already valid JSON)
  jq -n --arg tool "$1" --argjson arguments "$2" \
    '{jsonrpc: "2.0", id: 1, method: "tools/call", params: {name: $tool, arguments: $arguments}}'
}

# A response may come back as a plain JSON body or as a one-event SSE
# stream ("event: message\ndata: {...}\n\n") depending on how the MCP SDK
# picked between the Accept header's two offered media types; unwrap the
# latter down to its one `data:` line before anything tries to `jq` it.
mm_mcp_unwrap() {
  if jq -e . >/dev/null 2>&1 <<<"$1"; then
    printf '%s' "$1"
  else
    sed -n 's/^data: //p' <<<"$1" | tail -n1
  fi
}

mm_mcp_call() {
  # $1 base url, $2 tool name, $3 arguments json, $4 optional bearer token
  # (omit for a server with no DATABASE_URL at all, i.e. no bearer auth)
  local raw
  local -a headers=(
    --header 'Content-Type: application/json'
    --header 'Accept: application/json, text/event-stream'
  )
  if [[ -n "${4:-}" ]]; then
    headers+=(--header "Authorization: Bearer $4")
  fi
  raw=$(curl --silent --show-error --fail \
    "${headers[@]}" \
    --data "$(mm_mcp_body "$2" "$3")" \
    "$1/mcp")
  mm_mcp_unwrap "$raw"
}

mm_require_ok() {
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

# Waits up to 30s for `make db-up`'s own mm-pg container to answer
# `pg_isready`.
mm_pg_wait_ready() {
  local ready=0
  for _ in $(seq 1 30); do
    if podman exec mm-pg pg_isready -U mm >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1
  done
  if [[ "$ready" -ne 1 ]]; then
    echo "FAIL: mm-pg did not become ready (see 'make db-up')" >&2
    return 1
  fi
}

# Drops $1 if it already exists (a previous run's leftover) and creates it
# fresh, owned by $2.
mm_pg_create_database() {
  local db_name="$1" owner="$2"
  podman exec mm-pg psql -U mm -d mm -v ON_ERROR_STOP=1 \
    -c "drop database if exists ${db_name};" \
    -c "create database ${db_name} owner ${owner};" \
    >/dev/null
}

# Terminates every other backend connected to $1 and drops it - best
# effort (`|| true`), this only ever runs from a cleanup trap.
mm_pg_drop_database() {
  local db_name="$1"
  podman exec mm-pg psql -U mm -d mm -v ON_ERROR_STOP=1 \
    -c "select pg_terminate_backend(pid) from pg_stat_activity where datname = '${db_name}' and pid <> pg_backend_pid();" \
    -c "drop database if exists ${db_name};" \
    >/dev/null 2>&1 || true
}
