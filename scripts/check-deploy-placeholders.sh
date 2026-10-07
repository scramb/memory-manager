#!/usr/bin/env bash
# Fails if deploy/ (and, once it exists, charts/) contain a hostname or
# IPv4 address that is not a documented placeholder - this is a public
# repository (CLAUDE.md: "no operator-specific values ... in code,
# manifests, examples"), so deployment artefacts must stay generic.
set -euo pipefail
cd "$(dirname "$0")/.."

fail=0
err() { echo "ERROR: $*" >&2; fail=1; }

dirs=()
for d in deploy charts; do
  [[ -d "$d" ]] && dirs+=("$d")
done
if [[ ${#dirs[@]} -eq 0 ]]; then
  echo "no deploy/ or charts/ directory to check"
  exit 0
fi

# Everything below is either a documented placeholder suffix
# (*.example.com/.example.org), a genuinely generic name (localhost,
# svc.cluster.local), part of a Kubernetes/Flux/Gateway API/CNPG/
# External Secrets kind's own `apiVersion`/annotation key, or an image
# registry - present in every deployment of this kind, never a specific
# operator's own infrastructure. The second group is source/doc file
# extensions a comment's own cross-reference ("see config.py") ends in -
# never a hostname's TLD either.
allowed_suffixes=(
  example.com example.org localhost svc.cluster.local
  github.com ghcr.io docker.io
  kubernetes.io k8s.io fluxcd.io cnpg.io external-secrets.io
  monitoring.coreos.com githubusercontent.com
  py sh yaml yml md json toml lock txt cfg ini
)

# Exact, whole-token exceptions that happen to have the same dotted shape
# as a hostname but are neither one nor a file reference: the SPDX
# license identifier every file starts with, and the conventional
# placeholder for "some released semver tag" in rollout docs.
allowed_tokens=(AGPL-3.0-only X.Y.Z)

is_allowed_host() {
  local host="$1" suffix token
  for token in "${allowed_tokens[@]}"; do
    [[ "$host" == "$token" ]] && return 0
  done
  for suffix in "${allowed_suffixes[@]}"; do
    if [[ "$host" == "$suffix" || "$host" == *".$suffix" ]]; then
      return 0
    fi
  done
  return 1
}

# A DNS label sequence with at least one dot - the shape of both a real
# hostname and a git remote's "<repo>.git" suffix - optionally followed by
# a single `/<label>` path segment, the shape of a Kubernetes qualified
# name (`<DNS subdomain>/<name>`, e.g. `kubernetes.io/ingress.class`): the
# API convention itself guarantees the part after the slash is never an
# operator-specific hostname, only the part before it can be, so that is
# the only part `is_allowed_host` below ever checks.
host_regex='[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+(/[A-Za-z0-9][A-Za-z0-9.-]*)?'
# Exactly four dot-separated numeric groups - what actually makes a token
# an IPv4 address, as opposed to e.g. a "0.0.0" placeholder version number
# (two dots, three groups) that `host_regex` would also match.
ipv4_regex='^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'

# Helm templates (`charts/**/templates/*.yaml`, `_helpers.tpl`) hold Go
# template expressions like `{{ .Values.database.cnpg.enabled }}` or
# `{{ include "memory-manager.fullname" . }}` - code, not literal file
# content, and never a hostname by themselves (whatever they render to at
# install time is checked once rendered, not here). Stripped before
# either regex runs below; a no-op on deploy/'s plain Kustomize YAML,
# which never contains `{{ }}` in the first place.
strip_templating() {
  sed -E 's/\{\{-?[^}]*-?\}\}//g' "$1"
}

while IFS=: read -r file line match; do
  [[ -z "${match:-}" ]] && continue
  # A bare number sequence (an IPv4 address, or a version string like
  # "0.0.0") is never a hostname by itself - checked separately below.
  [[ "$match" =~ ^[0-9.]+$ ]] && continue
  # "<name>.git" is a git remote's path suffix, not a hostname - the
  # remote's actual host (if any) is its own, separately matched token.
  [[ "$match" == *.git ]] && continue
  # Only the part before a Kubernetes qualified name's "/" is ever
  # checked against the allowlist (see host_regex's own comment above).
  if is_allowed_host "${match%%/*}"; then
    continue
  fi
  err "$file:$line: hostname not in the placeholder allowlist: $match"
done < <(
  find "${dirs[@]}" -type f \( -name '*.yaml' -o -name '*.yml' -o -name '*.md' -o -name '*.tpl' \) -print0 \
    | while IFS= read -r -d '' f; do
        strip_templating "$f" | grep -noE "$host_regex" | sed "s#^#${f}:#"
      done || true
)

while IFS=: read -r file line match; do
  [[ -z "${match:-}" ]] && continue
  [[ "$match" =~ $ipv4_regex ]] || continue
  if [[ "$match" == "0.0.0.0" || "$match" == "127.0.0.1" ]]; then
    continue
  fi
  err "$file:$line: IPv4 address other than 0.0.0.0/127.0.0.1: $match"
done < <(
  find "${dirs[@]}" -type f \( -name '*.yaml' -o -name '*.yml' -o -name '*.md' -o -name '*.tpl' \) -print0 \
    | while IFS= read -r -d '' f; do
        strip_templating "$f" | grep -noE '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}' | sed "s#^#${f}:#"
      done || true
)

[[ $fail -eq 0 ]] && echo "deploy placeholder check OK"
exit $fail
