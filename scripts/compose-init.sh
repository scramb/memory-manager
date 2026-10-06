#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
#
# `vault-init`'s one-shot entrypoint in compose.yaml (#42, WP-12): makes
# sure the local bare "remote" the memory-manager service clones from
# (file:///data/remote.git, compose.yaml's default VAULT_REMOTE) exists
# and carries one note, so a brand-new stack has something to read on its
# first /mcp call instead of an empty vault.
#
# Idempotent: a no-op when VAULT_REMOTE does not point at the local
# default (an operator-configured remote is the operator's own vault,
# nothing to seed), and a no-op again once `main` already carries a
# commit - a restart of the same stack, not a first start.
#
# Runs inside the memory-manager image itself (vault-init's `image:` in
# compose.yaml), so `git` and the `memory-manager` CLI are both already on
# PATH; the welcome note is turned into a real, ADR-0005-valid note by the
# same `memory-manager import markdown` code path a human would use, not
# hand-rolled frontmatter.
set -eu

REMOTE_DIR=/data/remote.git
DEFAULT_REMOTE="file://${REMOTE_DIR}"

if [ "${VAULT_REMOTE:-}" != "$DEFAULT_REMOTE" ]; then
  echo "vault-init: VAULT_REMOTE=${VAULT_REMOTE:-<unset>} is not the local default ($DEFAULT_REMOTE) - nothing to seed"
  exit 0
fi

mkdir -p "$REMOTE_DIR"
if [ ! -e "$REMOTE_DIR/HEAD" ]; then
  git init --quiet --bare --initial-branch=main "$REMOTE_DIR"
fi

if git --git-dir="$REMOTE_DIR" rev-parse --verify --quiet refs/heads/main >/dev/null; then
  echo "vault-init: $REMOTE_DIR already has a 'main' branch - nothing to seed"
  exit 0
fi

SEED_DIR=$(mktemp -d)
cleanup() {
  rm -rf "$SEED_DIR" "$VAULT_DIR"
}
trap cleanup EXIT

cat > "$SEED_DIR/welcome.md" <<'NOTE'
# Welcome to memory-manager

This is the first note in your vault, written by `vault-init` the first
time this stack came up. Notes live at `<namespace>/<type>/<slug>.md`;
feel free to edit or delete this one once you have real notes.
NOTE

export VAULT_REMOTE="$DEFAULT_REMOTE"
export VAULT_DIR="/tmp/vault-init-clone"
rm -rf "$VAULT_DIR"

memory-manager import markdown "$SEED_DIR" --namespace shared --type reference --apply

echo "vault-init: seeded $REMOTE_DIR with a welcome note"
