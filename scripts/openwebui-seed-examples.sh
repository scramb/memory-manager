#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
#
# `vault-init`'s entrypoint under
# deploy/openwebui/examples-vault-overlay.yml (#151, WP-44): seeds the bare
# "remote" the memory-manager service clones from (file:///data/remote.git,
# the same default compose-init.sh uses) with a verbatim copy of
# `examples/vault` (mounted read-only at /examples by the overlay) instead
# of compose-init.sh's own single welcome note.
#
# Plain `git add`/`commit`/`push`, never `memory-manager import markdown`
# (compose-init.sh's own approach): the small-model filter benchmark's
# question set (tests/e2e/clients/small_model_questions.yaml) is written
# against examples/vault's own notes by their exact `source_path` and
# frontmatter, which an import pass would regenerate - same reasoning and
# pattern as scripts/upgrade-smoke.sh's own "seed a throwaway remote with a
# copy of examples/vault" step.
#
# Idempotent, same rules as compose-init.sh: a no-op when VAULT_REMOTE does
# not point at the local default (an operator-configured remote is the
# operator's own vault), and a no-op again once `main` already carries a
# commit (a restart of the same stack, not a first start).
#
# Runs inside the memory-manager image itself (vault-init's `image:` in
# docker-compose.yml), so `git` is already on PATH.
set -eu

REMOTE_DIR=/data/remote.git
DEFAULT_REMOTE="file://${REMOTE_DIR}"

if [ "${VAULT_REMOTE:-}" != "$DEFAULT_REMOTE" ]; then
  echo "openwebui-seed-examples: VAULT_REMOTE=${VAULT_REMOTE:-<unset>} is not the local default ($DEFAULT_REMOTE) - nothing to seed"
  exit 0
fi

mkdir -p "$REMOTE_DIR"
if [ ! -e "$REMOTE_DIR/HEAD" ]; then
  git init --quiet --bare --initial-branch=main "$REMOTE_DIR"
fi

if git --git-dir="$REMOTE_DIR" rev-parse --verify --quiet refs/heads/main >/dev/null; then
  echo "openwebui-seed-examples: $REMOTE_DIR already has a 'main' branch - nothing to seed"
  exit 0
fi

SEED_DIR=/tmp/openwebui-seed-examples-clone
cleanup() {
  rm -rf "$SEED_DIR"
}
trap cleanup EXIT
rm -rf "$SEED_DIR"

git clone --quiet "$DEFAULT_REMOTE" "$SEED_DIR"
cp -r /examples/. "$SEED_DIR/"
cd "$SEED_DIR"
git -c user.name="openwebui-seed-examples" -c user.email="openwebui-seed-examples@example.invalid" \
  add -A
git -c user.name="openwebui-seed-examples" -c user.email="openwebui-seed-examples@example.invalid" \
  commit --quiet -m "seed: copy examples/vault for the WP-44 small-model benchmark"
git push --quiet origin main

echo "openwebui-seed-examples: seeded $REMOTE_DIR with a copy of examples/vault"
