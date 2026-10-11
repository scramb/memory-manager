#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Brings up the local Open WebUI stack (#141, WP-42):
# deploy/openwebui/docker-compose.yml, plus any extra compose files given
# as arguments - #313's own `oidc-git-overlay.yml`/`postgres-entra-
# overlay.yml`, each proving a second, real login mode against the mock
# IdP (`tests/mock_idp`), or none at all for the default stack. Same
# compose-engine selection as scripts/smoke-compose.sh - `podman compose`/
# `docker compose`, whichever is on PATH, or $COMPOSE_ENGINE if set.
#
#   scripts/openwebui-up.sh
#   scripts/openwebui-up.sh deploy/openwebui/oidc-git-overlay.yml
#   scripts/openwebui-up.sh deploy/openwebui/postgres-entra-overlay.yml
#
# Creates deploy/openwebui/.env from its .env.example once, filled with a
# fresh, random WEBUI_SECRET_KEY, deploy/openwebui/.env-memory-manager plus
# its plaintext-password sibling once, and (#313) deploy/openwebui/
# .env-oidc-entra once - all from freshly generated secrets, none
# overwritten afterwards (those values must stay stable across restarts,
# docker-compose.yml's header comment explains why) - then brings the
# stack up in phases and polls every container until every long-running
# one (postgres, memory-manager, scripted-model, open-webui, and - only
# when an overlay adds it - mock-idp) reports healthy and every one-shot
# (volume-perms, vault-init, and - postgres-entra-overlay.yml only -
# db-role-init) has exited 0 - podman-compose 1.3 does not block `up -d`
# on `condition: service_healthy` the way `docker compose up` does, so
# this script is the thing that actually waits. Which services exist at
# all is read back from the merged config itself (`compose ... config
# --services`), not hard-coded per overlay - an overlay this script has
# never heard of still gets waited for correctly as long as its services
# are named in the known lists below or carry a healthcheck/clean exit the
# same way the existing ones do.
#
# More than two phases, not one `up -d --build`: docker-compose.yml
# deliberately drops `depends_on` on the one-shots (volume-perms,
# vault-init, db-role-init) from whatever needs them to have finished -
# podman-compose 1.3 turns every `depends_on` entry into a permanent
# `podman --requires=...` on the container regardless of its `condition`,
# and a one-shot that has already exited 0 can never satisfy that again,
# so a container requiring it gets stuck in `Created` ("internal libpod
# error") the moment anything tries to start it (confirmed against
# podman_compose.py's `container_to_args` and by reproducing the stuck
# state). This script is the thing that enforces the real order instead:
# phase 1 starts and waits for the dependency-free one-shots, phase 1b (if
# postgres-entra-overlay.yml added `db-role-init`) starts postgres alone,
# waits for it healthy, then starts and waits for `db-role-init` itself
# (memory-manager's own startup needs the role it creates to already
# exist before `memory-manager` ever starts), phase 2 starts everything
# else once all of the above have finished.
#
# Does NOT tear the stack down on failure (no `trap ... EXIT`, unlike
# scripts/smoke-compose.sh): leaves it running for inspection. `make
# openwebui-down` (with the same extra compose files) is the separate,
# explicit teardown.
set -euo pipefail
cd "$(dirname "$0")/.."

# Absolute, not relative: podman-compose 1.3's own `-f` resolution looks
# for the project directory before the current working directory is
# applied, and fails with a plain `FileNotFoundError` on a relative path
# here - confirmed by running the equivalent `podman compose -f
# deploy/openwebui/docker-compose.yml ... config` by hand. Extra compose
# files (this script's own arguments, e.g. `deploy/openwebui/
# oidc-git-overlay.yml`) are resolved the same way, relative to the
# repository root the `cd` above already put this script in.
COMPOSE_FILE="$(pwd)/deploy/openwebui/docker-compose.yml"
FILE_ARGS=(-f "$COMPOSE_FILE")
for extra in "$@"; do
  FILE_ARGS+=(-f "$(pwd)/${extra}")
done
PROJECT="mm-openwebui"
TIMEOUT_SECONDS="${OPENWEBUI_UP_TIMEOUT_SECONDS:-600}"

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

# Every service the merged config (base file plus whatever extra compose
# files were given above) actually defines - the one thing this script
# reads back rather than hard-codes, so an overlay's own services are
# waited for correctly without this script needing to already know about
# every overlay that will ever exist.
mapfile -t MERGED_SERVICES < <("${COMPOSE[@]}" "${FILE_ARGS[@]}" -p "$PROJECT" config --services)

contains() {
  local needle="$1"
  shift
  local item
  for item in "$@"; do
    [[ "$item" == "$needle" ]] && return 0
  done
  return 1
}

# Every one-shot (exit 0 and done) this project can ever define - only the
# ones `MERGED_SERVICES` actually reports are started at all, so a plain
# `scripts/openwebui-up.sh` with no overlay never sees `db-role-init`.
ONESHOT_SERVICES=()
for candidate in volume-perms vault-init db-role-init; do
  contains "$candidate" "${MERGED_SERVICES[@]}" && ONESHOT_SERVICES+=("$candidate")
done

# Every long-running service (has its own healthcheck) this project can
# ever define, same "only if present" reasoning - `mock-idp` is the only
# one an overlay adds today (#313's own two overlays).
LONG_RUNNING_SERVICES=()
for candidate in postgres memory-manager scripted-model open-webui mock-idp; do
  contains "$candidate" "${MERGED_SERVICES[@]}" && LONG_RUNNING_SERVICES+=("$candidate")
done

env_file="deploy/openwebui/.env"
example_file="deploy/openwebui/.env.example"
if [[ ! -f "$env_file" ]]; then
  secret=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  sed "s/^WEBUI_SECRET_KEY=.*/WEBUI_SECRET_KEY=${secret}/" "$example_file" > "$env_file"
  echo "created ${env_file} with a fresh, random WEBUI_SECRET_KEY"
else
  echo "reusing existing ${env_file} (WEBUI_SECRET_KEY must stay stable)"
fi

# memory-manager's own AS secrets (#142, docker-compose.yml's header
# comment): an argon2id admin password hash and a Fernet client-secret
# encryption key, generated once inside the already-built memory-manager
# image (never locally - a dev venv may not even have `argon2-cffi`/
# `cryptography` on PATH in the same versions) and never regenerated -
# ADMIN_PASSWORD_HASH and OAUTH_CLIENT_SECRET_KEY must stay stable across
# restarts for the same reason WEBUI_SECRET_KEY above does.
mm_env_file="deploy/openwebui/.env-memory-manager"
admin_password_file="deploy/openwebui/.admin-password"
if [[ ! -f "$mm_env_file" ]]; then
  "${COMPOSE[@]}" "${FILE_ARGS[@]}" -p "$PROJECT" build memory-manager
  admin_password=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
  admin_password_hash=$(printf '%s' "$admin_password" \
    | podman run --rm -i memory-manager:dev memory-manager hash-password)
  oauth_client_secret_key=$(podman run --rm memory-manager:dev python3 -c \
    "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  {
    echo "ADMIN_PASSWORD_HASH=${admin_password_hash}"
    echo "OAUTH_CLIENT_SECRET_KEY=${oauth_client_secret_key}"
  } > "$mm_env_file"
  printf '%s\n' "$admin_password" > "$admin_password_file"
  chmod 600 "$mm_env_file" "$admin_password_file"
  echo "created ${mm_env_file} and ${admin_password_file} with fresh secrets"
else
  echo "reusing existing ${mm_env_file} (ADMIN_PASSWORD_HASH must stay stable)"
fi

# #313 (WP-42): the mock IdP's own client/tenant/subject values, read by
# oidc-git-overlay.yml/postgres-entra-overlay.yml's own `env_file:` entry
# on `memory-manager` - generated unconditionally (harmless when neither
# overlay is in play, same as the two files above) so a later `openwebui-
# up.sh <overlay>` run never has to special-case "first run with an
# overlay" versus "first run at all". Only `OIDC_CLIENT_SECRET`/
# `ENTRA_CLIENT_SECRET` are actually secret; every other value here is a
# fixed test literal kept out of either overlay's own compose YAML only
# because an OIDC issuer URL's multi-segment shape is a token
# scripts/check-deploy-placeholders.sh cannot tell apart from an
# operator's own hostname (see oidc-git-overlay.yml's own header comment).
oidc_entra_env_file="deploy/openwebui/.env-oidc-entra"
if [[ ! -f "$oidc_entra_env_file" ]]; then
  oidc_client_secret=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
  entra_client_secret=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
  {
    echo "OIDC_CLIENT_ID=openwebui-git-client"
    echo "OIDC_CLIENT_SECRET=${oidc_client_secret}"
    echo "OIDC_ISSUER=http://localhost:18082/openwebui-git/v2.0"
    echo "OIDC_ALLOWED_SUBJECTS=openwebui-git-user-a,openwebui-git-user-b"
    echo 'LOGIN_NAMESPACE_MAP={"openwebui-git-user-a": ["demo-a"], "openwebui-git-user-b": ["demo-b"]}'
    echo "ENTRA_TENANT_ID=openwebui-pg"
    echo "ENTRA_CLIENT_ID=openwebui-pg-client"
    echo "ENTRA_CLIENT_SECRET=${entra_client_secret}"
    echo "ENTRA_AUTHORITY=http://localhost:18082"
    echo "ENTRA_GRAPH_URL=http://localhost:18082/graph/v1.0"
  } > "$oidc_entra_env_file"
  chmod 600 "$oidc_entra_env_file"
  echo "created ${oidc_entra_env_file} with fresh client secrets"
else
  echo "reusing existing ${oidc_entra_env_file} (client secrets must stay stable)"
fi

dump_logs() {
  echo "--- compose logs: ${ONESHOT_SERVICES[*]} ${LONG_RUNNING_SERVICES[*]} ---" >&2
  "${COMPOSE[@]}" "${FILE_ARGS[@]}" -p "$PROJECT" logs \
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

wait_for_long_running() {
  local service="$1" start now elapsed
  start=$(date +%s)
  elapsed=0
  while (( elapsed < TIMEOUT_SECONDS )); do
    long_running_healthy "$service" && return 0
    sleep 2
    now=$(date +%s)
    elapsed=$((now - start))
  done
  return 1
}

# Phase 1: the dependency-free one-shots (volume-perms, then vault-init),
# each started on its own and waited for - see the header comment and
# docker-compose.yml's own comments on vault-init/memory-manager for why
# the compose file no longer encodes this order through `depends_on`
# itself.
for oneshot in "${ONESHOT_SERVICES[@]}"; do
  [[ "$oneshot" == "db-role-init" ]] && continue  # phase 1b, below - needs postgres first
  if ! "${COMPOSE[@]}" "${FILE_ARGS[@]}" -p "$PROJECT" up -d --build "$oneshot"; then
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

# Phase 1b (postgres-entra-overlay.yml only): postgres has no one-shot
# dependency of its own, so it can start on its own here, ahead of
# memory-manager - `db-role-init` then runs once postgres is healthy, and
# memory-manager (phase 2) only ever starts once `db-role-init` has
# exited 0, so the role it creates always exists before memory-manager's
# own startup looks for it (see the header comment for why this cannot be
# a plain `depends_on` on either side instead).
if contains "db-role-init" "${ONESHOT_SERVICES[@]}"; then
  if ! "${COMPOSE[@]}" "${FILE_ARGS[@]}" -p "$PROJECT" up -d --build postgres; then
    echo "FAIL: compose up did not start 'postgres' ahead of 'db-role-init'" >&2
    dump_logs
    exit 1
  fi
  if ! wait_for_long_running postgres; then
    echo "FAIL: 'postgres' did not become healthy within ${TIMEOUT_SECONDS}s" >&2
    dump_logs
    exit 1
  fi
  if ! "${COMPOSE[@]}" "${FILE_ARGS[@]}" -p "$PROJECT" up -d --build db-role-init; then
    echo "FAIL: compose up did not start one-shot 'db-role-init'" >&2
    dump_logs
    exit 1
  fi
  if ! wait_for_oneshot db-role-init; then
    echo "FAIL: one-shot 'db-role-init' did not exit 0 within ${TIMEOUT_SECONDS}s" >&2
    dump_logs
    exit 1
  fi
fi

# Phase 2: everything else, now that every one-shot has exited 0. postgres
# itself may already be running from phase 1b - podman-compose 1.3 still
# tries to re-create it as `db-role-init`'s/`memory-manager`'s own
# `depends_on` dependency regardless of whether `postgres` is in this
# call's own argument list, which logs a harmless (observed live,
# non-fatal) "container name ... is already in use" line for it; the
# polling loop below is what actually decides pass/fail, not this
# command's own exit status.
if ! "${COMPOSE[@]}" "${FILE_ARGS[@]}" -p "$PROJECT" up -d --build "${LONG_RUNNING_SERVICES[@]}"; then
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
