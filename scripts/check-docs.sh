#!/usr/bin/env bash
# Cheap structural checks for the planning skeleton. Runs in CI and locally.
set -euo pipefail
cd "$(dirname "$0")/.."
fail=0
err() { echo "ERROR: $*" >&2; fail=1; }

# Required planning files
for f in CLAUDE.md docs/PLAN.md docs/TASKS.md; do
  [[ -f "$f" ]] || err "missing $f"
done

# No loose Markdown in the repository root beyond the OSS hygiene files
allowed='^(README|CONTRIBUTING|CODE_OF_CONDUCT|SECURITY|CHANGELOG|CLAUDE)\.md$'
for f in *.md; do
  [[ "$f" =~ $allowed ]] || err "loose root Markdown file: $f (move it under docs/)"
done

# ADRs: file name pattern and a Status line
for f in docs/adr/*.md; do
  [[ -e "$f" ]] || continue
  [[ "$(basename "$f")" =~ ^[0-9]{4}-[a-z0-9-]+\.md$ ]] || err "bad ADR file name: $f"
  grep -qE '^Status: (Proposed|Accepted|Superseded by ADR-[0-9]{4})' "$f" || err "ADR without valid Status line: $f"
done

# PLAN must point to TASKS at the very top
head -n 5 docs/PLAN.md | grep -q 'docs/TASKS.md\|TASKS.md' || err "docs/PLAN.md must reference TASKS.md in its first lines"

# Relative links in docs must resolve
while IFS= read -r line; do
  file="${line%%:*}"; target="${line#*:}"
  target="${target%%#*}"
  [[ -z "$target" ]] && continue
  [[ -e "$(dirname "$file")/$target" ]] || err "broken link in $file -> $target"
done < <(grep -oHE '\]\((\.\.?/)[^)]+\)' CLAUDE.md docs -r --include='*.md' | sed -E 's/\]\(([^)]+)\)/\1/')

# Every task line in TASKS.md carries an ID
if grep -nE '^\s*- \[[ x]\] ' docs/TASKS.md | grep -vE '\[[ x]\] (T-[0-9]{3}|#[0-9]+) ' | grep -vE '^\S+:\s{2,}' ; then
  err "top-level task lines in docs/TASKS.md must start with T-NNN or #NN"
fi

[[ $fail -eq 0 ]] && echo "docs OK"
exit $fail
