# Client: Claude Code (config file format, for `connect`/#137)

Retrieved · Tested version: official docs retrieved 2026-10-10 (`code.claude.com/docs/en/mcp`,
`/memory`, `/settings`, `/claude-directory`, `/env-vars`); local observation against
`claude --version` **2.1.280**, installed on this machine.

This note only covers what `clients/claude_code.py` needs to merge the memory-manager server
into Claude Code's own config without clobbering it - transport/auth behaviour at the protocol
level (CIMD, DCR, `headersHelper`, truncation limits, ...) is already covered by
[`../mcp-auth-and-connectors.md`](../mcp-auth-and-connectors.md#5-claude-code-k1) and
[`../../clients/claude-code.md`](../../clients/claude-code.md); this is the part that document
does not go into: the exact file(s), their shape, and what is safe to assume when editing one
by hand instead of through `claude mcp add`.

## Summary

Claude Code keeps server entries in two places depending on scope. **User** scope (every
project, this machine) is a top-level `mcpServers` object in `~/.claude.json`, or
`$CLAUDE_CONFIG_DIR/.claude.json` when that variable is set [CC1], [CC4]. **Local** scope (this
project, this machine, not shared) is the *same* file, under
`projects["<absolute project path>"].mcpServers` [CC1]. **Project** scope (shared with
collaborators) is a separate `<project>/.mcp.json`, `{"mcpServers": {...}}` at the top level,
and needs interactive approval the first time a collaborator opens the project [CC1], [CC2].
Precedence where more than one scope defines the same server name: local > project > user,
whole entry replaced by name, not merged field by field [CC1]. `connect` only ever touches user
or project scope (#137's "Nicht dabei" excludes `--scope local`), so it never has to merge
*inside* `projects[...]`, only read it (to warn when a local-scope entry would shadow what it
just wrote).

## Entry shapes

```json
{"type": "http", "url": "https://memory.example.com/mcp", "headers": {"Authorization": "Bearer ${MEMORY_MANAGER_TOKEN}"}}
{"type": "stdio", "command": "memory-manager", "args": ["serve", "--stdio"], "env": {}}
```

`type` is mandatory on every entry - an HTTP entry without it is read as a (broken) stdio entry
and silently skipped, not an error [CC1]. `${VAR}` and `${VAR:-default}` are expanded inside
`command`, `args`, `env`, `url` and `headers` values, in every scope including user [CC1],
[CC3]; an unset `VAR` is left as the literal `${VAR}` string plus a warning, it does not blank
out. A `headers.Authorization` entry disables Claude Code's own OAuth fallback for that
server - set one only when `--token-env` was actually requested, never unconditionally [CC1].

## File format

Observed on a freshly `claude mcp add`-managed `~/.claude.json`, and relied on by
`jsonconfig.py`'s round-trip check:

- 2-space indent, `ensure_ascii=False` (non-ASCII unescaped), **no trailing newline**.
- Mode `0600` for `~/.claude.json`; `.mcp.json` is mode `0644` (meant to be committed).
- `json.dumps(json.loads(raw), indent=2, ensure_ascii=False) == raw` holds byte-exact on a file
  Claude Code itself last wrote [CC1 local observation]. A hand-edited or older-version file is
  not guaranteed to round-trip the same way - `jsonconfig.py` detects the file's own indent
  instead of assuming 2, and only *warns* (does not block) when even that does not round-trip.
- Claude Code keeps its own backups of `~/.claude.json` under `~/.claude/backups/` (last five)
  and rewrites the file continuously as the session runs [CC1 local observation] - a reason
  `connect` re-reads the file right before its own write and aborts on a mismatch rather than
  trusting a single read from earlier in the process.

## Alternative considered: `claude mcp add-json`

Rejected as the implementation for `connect claude-code`: it requires the `claude` binary
installed (memory-manager's own CLI should not depend on another vendor's CLI being present),
and it does not expose a diff, a backup, or a concurrent-change check under this project's own
control (CLAUDE.md: never overwrite silently) - `claude mcp add`/`add-json` just writes,
trusting Claude Code's own backup directory as the only safety net.

## Open questions

1. Does Claude Code hold a lock, or re-read the file immediately before its own writes while a
   session is running? Not verified from the docs or from local observation (a plain two-writer
   race was not reproduced locally) - `connect`'s own re-read-before-write guards against the
   case where it loses that race regardless of the answer.
2. Whether `$CLAUDE_CONFIG_DIR` can point at a path that does not yet exist on first use (the
   docs page name the variable but not this edge) - `connect` creates the parent directory
   itself either way.

## Sources

- [CC1] Anthropic, "Model Context Protocol (MCP)", <https://code.claude.com/docs/en/mcp>,
  retrieved 2026-10-10
- [CC2] Anthropic, "Manage Claude's memory", <https://code.claude.com/docs/en/memory>, retrieved
  2026-10-10
- [CC3] Anthropic, "Claude Code settings", <https://code.claude.com/docs/en/settings>, retrieved
  2026-10-10
- [CC4] Anthropic, "Claude Code directory structure",
  <https://code.claude.com/docs/en/claude-directory>, retrieved 2026-10-10
- [CC5] Anthropic, "Environment variables", <https://code.claude.com/docs/en/env-vars>, retrieved
  2026-10-10
- Local observation: `claude --version` → `2.1.280 (Claude Code)` on this machine, 2026-10-10.
