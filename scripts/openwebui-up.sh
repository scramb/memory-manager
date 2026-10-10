#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Brings up the local Open WebUI stack (#141, WP-42):
# deploy/openwebui/docker-compose.yml. Same compose-engine selection as
# scripts/smoke-compose.sh - `podman compose`/`docker compose`, whichever
# is on PATH, or $COMPOSE_ENGINE if set.
#
# Creates deploy/openwebui/.env from its .env.example once, filled with a
# fresh, random WEBUI_SECRET_KEY (never overwritten afterwards - that key
# must stay stable across restarts, docker-compose.yml's header comment
# explains why), then brings the stack up in two phases and polls every
# container until the long-running ones (postgres, memory-manager,
# scripted-model, open-webui) report healthy and the one-shots
# (volume-perms, vault-init) have exited 0 - podman-compose 1.3 does not
# block `up -d` on `condition: service_healthy` the way `docker compose up`
# does, so this script is the thing that actually waits.
#
# Two phases, not one `up -d --build`: docker-compose.yml deliberately
# drops `depends_on` on the one-shots (volume-perms, vault-init) from
# whatever needs them to have finished - podman-compose 1.3 turns every
# `depends_on` entry into a permanent `podman --requires=...` on the
# container regardless of its `condition`, and a one-shot that has already
# exited 0 can never satisfy that again, so a container requiring it gets
# stuck in `Created` ("internal libpod error") the moment anything tries to
# start it (confirmed against podman_compose.py's `container_to_args` and
# by reproducing the stuck state). This script is the thing that enforces
# the real order instead: phase 1 starts and waits for the one-shots, phase
# 2 starts everything else only once they have exited 0.
#
# Does NOT tear the stack down on failure (no `trap ... EXIT`, unlike
# scripts/smoke-compose.sh): leaves it running for inspection. `make
# openwebui-down` is the separate, explicit teardown.
set -euo pipefail
cd "$(dirname "$0")/.."

# Absolute, not relative: podman-compose 1.3's own `-f` resolution looks
# for the project directory before the current working directory is
# applied, and fails with a plain `FileNotFoundError` on a relative path
# here - confirmed by running the equivalent `podman compose -f
# deploy/openwebui/docker-compose.yml ... config` by hand.
COMPOSE_FILE="$(pwd)/deploy/openwebui/docker-compose.yml"
PROJECT="mm-openwebui"
TIMEOUT_SECONDS="${OPENWEBUI_UP_TIMEOUT_SECONDS:-600}"

# Containers with no healthcheck of their own by design (one-shot, exit 0
# and done) versus ones this script waits for `(healthy)` on - see
# docker-compose.yml.
ONESHOT_SERVICES=(volume-perms vault-init)
LONG_RUNNING_SERVICES=(postgres memory-manager scripted-model open-webui)

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

env_file="deploy/openwebui/.env"
example_file="deploy/openwebui/.env.example"
if [[ ! -f "$env_file" ]]; then
  secret=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  sed "s/^WEBUI_SECRET_KEY=.*/WEBUI_SECRET_KEY=${secret}/" "$example_file" > "$env_file"
  echo "created ${env_file} with a fresh, random WEBUI_SECRET_KEY"
else
  echo "reusing existing ${env_file} (WEBUI_SECRET_KEY must stay stable)"
fi

dump_logs() {
  echo "--- compose logs: ${ONESHOT_SERVICES[*]} ${LONG_RUNNING_SERVICES[*]} ---" >&2
  "${COMPOSE[@]}" -f "$COMPOSE_FILE" -p "$PROJECT" logs \
    "${ONESHOT_SERVICES[@]}" "${LONG_RUNNING_SERVICES[@]}" >&2 || true
}

container_name() {
  # podman-compose 1.3's own container naming, confirmed against a
  # throwaway project: "<project>_<service>_1" (`com.docker.compose.*`
  # labels carry the same project/service, used here only to name the
  # container to `podman inspect` next, not to enumerate it).
  echo "${PROJECT}_${1}_1"
}

oneshot_done() {
  local name
  name=$(container_name "$1")
  local status exit_code
  status=$(podman inspect "$name" --format '{{.State.Status}}' 2>/dev/null) || return 1
  [[ "$status" == "exited" ]] || return 1
  exit_code=$(podman inspect "$name" --format '{{.State.ExitCode}}' 2>/dev/null) || return 1
  [[ "$exit_code" == "0" ]]
}

long_running_healthy() {
  local name
  name=$(container_name "$1")
  local health
  health=$(podman inspect "$name" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null) || return 1
  [[ "$health" == "healthy" ]]
}

wait_for_oneshot() {
  local service="$1" start now elapsed
  start=$(date +%s)
  elapsed=0
  while (( elapsed < TIMEOUT_SECONDS )); do
    oneshot_done "$service" && return 0
    sleep 2
    now=$(date +%s)
    elapsed=$((now - start))
  done
  return 1
}

# Phase 1: volume-perms, then vault-init, each started on its own and
# waited for - see the header comment and docker-compose.yml's own
# comments on vault-init/memory-manager for why the compose file no longer
# encodes this order through `depends_on` itself.
for oneshot in "${ONESHOT_SERVICES[@]}"; do
  if ! "${COMPOSE[@]}" -f "$COMPOSE_FILE" -p "$PROJECT" up -d --build "$oneshot"; then
    echo "FAIL: compose up did not start one-shot '${oneshot}'" >&2
    dump_logs
    exit 1
  fi
  if ! wait_for_oneshot "$oneshot"; then
    echo "FAIL: one-shot '${oneshot}' did not exit 0 within ${TIMEOUT_SECONDS}s" >&2
    dump_logs
    exit 1
  fi
done

# Phase 2: everything else, now that both one-shots have exited 0. Scoped
# to the long-running services only, not a plain `up -d --build` - that
# would also try to recreate the already-finished one-shots and fail each
# time with "container name ... is already in use" (harmless, but noisy
# and worth avoiding).
if ! "${COMPOSE[@]}" -f "$COMPOSE_FILE" -p "$PROJECT" up -d --build "${LONG_RUNNING_SERVICES[@]}"; then
  echo "FAIL: compose up did not bring the stack up" >&2
  dump_logs
  exit 1
fi

start=$(date +%s)
elapsed=0
while (( elapsed < TIMEOUT_SECONDS )); do
  all_ready=true
  for service in "${ONESHOT_SERVICES[@]}"; do
    oneshot_done "$service" || all_ready=false
  done
  for service in "${LONG_RUNNING_SERVICES[@]}"; do
    long_running_healthy "$service" || all_ready=false
  done
  if [[ "$all_ready" == true ]]; then
    break
  fi
  sleep 3
  now=$(date +%s)
  elapsed=$((now - start))
done

if [[ "$all_ready" != true ]]; then
  echo "FAIL: stack did not reach healthy/exited-0 within ${TIMEOUT_SECONDS}s" >&2
  dump_logs
  exit 1
fi

echo "OK: all services healthy/exited-0 after ${elapsed}s"
