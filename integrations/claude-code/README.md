# Claude Code integration

Two small pieces that make Claude Code use the memory-manager tools correctly, plus a guide
for connecting the server itself.

- [`skills/memory/SKILL.md`](./skills/memory/SKILL.md) — a Claude Code skill. It embeds the
  MCP server's own `instructions` text (so the rules never drift between the two) and adds a
  quick-reference table of the `memory_*` tools.
- [`CLAUDE.snippet.md`](./CLAUDE.snippet.md) — the same rules, condensed to fit a project's
  `CLAUDE.md`, for setups that prefer a standing instruction over a skill that Claude decides
  to invoke.
- [`../../docs/guides/claude-code.md`](../../docs/guides/claude-code.md) — how to add the
  memory-manager stdio server to Claude Code with `claude mcp add`, and how to verify it
  connected.

Both files are generated from [`src/memory_manager/mcp/instructions.py`](../../src/memory_manager/mcp/instructions.py)'s
`INSTRUCTIONS` constant, embedded verbatim between `<!-- BEGIN memory-manager instructions -->`
and `<!-- END memory-manager instructions -->` markers.
[`tests/test_integration_docs.py`](../../tests/test_integration_docs.py) fails if either file
falls out of sync with that constant, so a change to the rules only has to be made in one
place.

## Installing the skill

Copy the `memory` folder into a skills directory Claude Code reads:

```sh
# For one project only:
mkdir -p .claude/skills
cp -r integrations/claude-code/skills/memory .claude/skills/memory

# For every project of this user:
mkdir -p ~/.claude/skills
cp -r integrations/claude-code/skills/memory ~/.claude/skills/memory
```

Restart (or start) a `claude` session in that project; the skill shows up as `memory` when
Claude Code lists what is available.

## Using the snippet instead

If you would rather not rely on Claude deciding to invoke a skill, paste
[`CLAUDE.snippet.md`](./CLAUDE.snippet.md) into the project's `CLAUDE.md` — it is kept under 25
lines so it stays a small addition, not a new section to maintain.

## Connecting the server

See [`docs/guides/claude-code.md`](../../docs/guides/claude-code.md) for `claude mcp add`,
environment variables and troubleshooting. The skill and snippet above only cover the usage
rules; they assume the server is already connected.
