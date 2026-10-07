#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Smoke-tests the compose quickstart (#42, WP-12): brings the stack up with
# `podman compose`/`docker compose` (whichever is on PATH, or
# $COMPOSE_ENGINE if set) - deliberately with no `.env` present, so this
# proves the "works without .env" claim rather than just assuming it - times
# how long GET /readyz takes to return 200, creates a token inside the
# running container and calls /mcp's `initialize` + `tools/list` with it,
# re-runs `vault-init` to prove it is idempotent, then tears the stack down
# again (`down -v` - nothing from this run is meant to survive it).
#
# Idempotence is checked via `compose run --rm vault-init`, not a second
# `up -d --build` on the already-running stack: podman-compose 1.3 errors
# with "container name already in use" on that (it does not recreate an
# existing container the way `docker compose up` does) - `run --rm` starts
# a one-off container under a fresh name instead, sidestepping that
# limitation entirely while still exercising the same idempotent-seed code
# path `up` ran the first time.
#
# Any failure before teardown dumps `vault-init`'s and `memory-manager`'s
# own container logs to stderr (`dump_logs`) - CI only ever shows this
# script's own stdout/stderr otherwise, never what the failing container
# itself printed.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${SMOKE_PORT:-8080}"
TIMEOUT_SECONDS="${SMOKE_TIMEOUT_SECONDS:-300}"
BASE_URL="http://127.0.0.1:${PORT}"

# A leftover `.env` from a previous manual run would mask exactly the bug
# this script exists to catch (compose.yaml must work with none at all).
rm -f .env

if [[ -n "${COMPOSE_ENGINE:-}" ]]; then
  read -r -a COMPOSE <<<"$COMPOSE_ENGINE"
elif command -v podman >/dev/null 2>&1; then
  COMPOSE=(podman compose)
elif command -v docker >/dev/null 2>&1; then
  COMPOSE=(docker compose)
else
  echo "FAIL: neither podman nor docker found on PATH" >&2
  exit 1
fi

echo "using compose engine: ${COMPOSE[*]}"

down() {
  "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
}
trap down EXIT

down # a stale stack from a previous, interrupted run must not linger

dump_logs() {
  echo "--- compose logs: vault-init, memory-manager ---" >&2
  "${COMPOSE[@]}" logs vault-init memory-manager >&2 || true
}

start=$(date +%s)
if ! "${COMPOSE[@]}" up -d --build; then
  echo "FAIL: compose up did not bring the stack up" >&2
  dump_logs
  exit 1
fi

status=""
elapsed=0
while (( elapsed < TIMEOUT_SECONDS )); do
  if status=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
      "${BASE_URL}/readyz" 2>/dev/null); then
    [[ "$status" == "200" ]] && break
  fi
  sleep 2
  now=$(date +%s)
  elapsed=$((now - start))
done

if [[ "$status" != "200" ]]; then
  echo "FAIL: GET /readyz did not return 200 within ${TIMEOUT_SECONDS}s (got ${status:-<none>}, elapsed ${elapsed}s)" >&2
  dump_logs
  exit 1
fi

echo "OK: GET /readyz -> 200 after ${elapsed}s"

token=$("${COMPOSE[@]}" exec -T memory-manager \
  memory-manager token create smoke-test --scope memory:read --scope memory:write --namespace '*')
token=$(echo "$token" | tr -d '\r' | tail -n1)

init_response=$(curl --silent --show-error \
  --header "Authorization: Bearer ${token}" \
  --header "Content-Type: application/json" \
  --header "Accept: application/json, text/event-stream" \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke-compose","version":"0"}}}' \
  "${BASE_URL}/mcp")
echo "initialize: ${init_response}"
if ! grep -q '"protocolVersion"' <<<"$init_response"; then
  echo "FAIL: /mcp initialize did not return a protocolVersion: ${init_response}" >&2
  exit 1
fi

tools_response=$(curl --silent --show-error \
  --header "Authorization: Bearer ${token}" \
  --header "Content-Type: application/json" \
  --header "Accept: application/json, text/event-stream" \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  "${BASE_URL}/mcp")
echo "tools/list: ${tools_response}"
if ! grep -q '"tools"' <<<"$tools_response"; then
  echo "FAIL: /mcp tools/list did not return a tools array: ${tools_response}" >&2
  exit 1
fi

echo "OK: /mcp initialize + tools/list answered with a valid token"

vault_remote_sha() {
  # --no-deps: `volume-perms` (a one-shot dependency, just like vault-init
  # itself) already ran during `up` above - without this, podman-compose
  # tries to start it again for this `run` and errors with "container name
  # already in use" (the same class of bug `up -d --build` run twice hits,
  # see the module docstring) instead of just reusing the already-exited one.
  "${COMPOSE[@]}" run --rm --no-deps --entrypoint git vault-init \
    --git-dir=/data/remote.git rev-parse refs/heads/main
}

before_sha=$(vault_remote_sha)
"${COMPOSE[@]}" run --rm --no-deps vault-init
after_sha=$(vault_remote_sha)

if [[ "$before_sha" != "$after_sha" ]]; then
  echo "FAIL: re-running vault-init changed refs/heads/main (${before_sha} -> ${after_sha}) - it should have been a no-op" >&2
  exit 1
fi

echo "OK: re-running vault-init left refs/heads/main at ${after_sha} (idempotent)"
echo "elapsed: ${elapsed}s"
