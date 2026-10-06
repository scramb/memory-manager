#!/usr/bin/env bash
# Smoke-tests the memory-manager container image (#41): starts it read-only
# with the vault on a volume, waits for GET /healthz to answer 200 with a
# body carrying version/commit/source (ADR-0002 S13), and checks it runs as
# the non-root uid 10001. Engine: $CONTAINER_ENGINE if set, else podman if
# available, else docker - CI's buildx job sets CONTAINER_ENGINE=docker
# (the image it builds is loaded into docker, not podman); everywhere else
# on this project uses podman (CLAUDE.md).
set -euo pipefail

IMAGE="${1:-memory-manager:dev}"
PORT="${SMOKE_PORT:-18080}"

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

NAME="mm-smoke-$$"
WORKDIR=$(mktemp -d)
BODY_FILE="${WORKDIR}/healthz-body.json"

cleanup() {
  "$ENGINE" rm -f "$NAME" >/dev/null 2>&1 || true
  # The vault clone and the bare remote were both written by the image's
  # uid 10001 (or root's mapped namespace range under rootless podman) -
  # the host user that owns $WORKDIR cannot unlink them directly, so this
  # deletes them from inside a throwaway root container instead.
  "$ENGINE" run --rm --user 0:0 --entrypoint rm \
    --volume "${WORKDIR}:/cleanup" "$IMAGE" -rf /cleanup/data >/dev/null 2>&1 || true
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

# A local bare repository, reached over file:// - the smoke test clones a
# vault without any real network, SSH key or HTTPS credential involved.
# Created through the image itself (as its own uid 10001), not the host's
# git: git refuses to clone from a directory a *different* uid owns
# ("dubious ownership"), and GIT_CONFIG_GLOBAL=/dev/null (vault/git.py)
# leaves no `safe.directory` escape hatch for a host-owned one.
mkdir -p "${WORKDIR}/data"
chmod 0777 "${WORKDIR}/data"
"$ENGINE" run --rm --entrypoint git \
  --volume "${WORKDIR}/data:/data" \
  "$IMAGE" init --quiet --bare --initial-branch=main /data/remote.git

echo "starting ${ENGINE} container from ${IMAGE}..."
"$ENGINE" run --detach --name "$NAME" \
  --read-only --tmpfs /tmp \
  --publish "${PORT}:8080" \
  --volume "${WORKDIR}/data:/data" \
  --env VAULT_REMOTE=file:///data/remote.git \
  --env MM_ALLOW_UNAUTHENTICATED=1 \
  "$IMAGE" >/dev/null

status=""
for _ in $(seq 1 30); do
  if status=$(curl --silent --show-error --output "$BODY_FILE" \
      --write-out '%{http_code}' "http://127.0.0.1:${PORT}/healthz" 2>/dev/null); then
    [[ "$status" == "200" ]] && break
  fi
  sleep 1
done

if [[ "$status" != "200" ]]; then
  echo "FAIL: GET /healthz did not return 200 within 30s (got ${status:-<none>})" >&2
  "$ENGINE" logs "$NAME" >&2 || true
  exit 1
fi

body=$(cat "$BODY_FILE")
echo "healthz: $body"

for field in version commit source; do
  if ! grep -q "\"${field}\"" "$BODY_FILE"; then
    echo "FAIL: /healthz body is missing '${field}': ${body}" >&2
    exit 1
  fi
done

uid=$("$ENGINE" exec "$NAME" id -u)
if [[ "$uid" != "10001" ]]; then
  echo "FAIL: the process runs as uid ${uid}, expected 10001" >&2
  exit 1
fi

echo "OK: GET /healthz -> 200, uid=${uid}"
