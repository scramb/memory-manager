# Connecting Claude Code to memory-manager

How to add the `memory-manager` stdio server as an MCP server in Claude Code, so the
`memory_*` tools (see `src/memory_manager/mcp/instructions.py`) become available in a
session. This guide covers the local stdio transport. For a deployed server, add it over HTTP
instead (`claude mcp add --transport http memory https://memory.example.com/mcp`). Claude Code
then runs the OAuth login in the browser, or uses a static token passed with
`--header "Authorization: Bearer <token>"` (see `memory-manager token create`).

Checked against `claude --version` **2.1.280** on **2026-10-06**. Command syntax also recorded
in [`docs/research/mcp-auth-and-connectors.md`](../research/mcp-auth-and-connectors.md#5-claude-code-k1).

## Prerequisites

- A **Git remote for the vault** the server can push to and pull from — any host it can reach
  over SSH or HTTPS (for example `git@memory.example.com:you/memory-vault.git`), or a local bare
  repository for trying this out (`git init --bare /path/to/vault-remote.git`).
- `uv` installed (<https://docs.astral.sh/uv/>) and a checkout of this repository, or the
  package installed with `uv tool install --from /path/to/memory-manager memory-manager`
  (`uv tool install memory-manager` once the package is published).
- Optional: a PostgreSQL 16+ database with `pgvector` for full-text/vector search
  (`memory_search`). Without it, note read/write still work; search degrades.
- Optional: an embedding provider (Ollama or an OpenAI-compatible endpoint) for vector search
  on top of full-text. Without one, search is full-text only.

## Environment variables

The server reads these from the process environment (`src/memory_manager/config.py`):

| Variable | Required | Meaning |
|---|---|---|
| `VAULT_REMOTE` | yes | Git remote URL the vault clones from and pushes to |
| `VAULT_DIR` | yes | local working copy path for the clone |
| `VAULT_BRANCH` | no | branch to track (default `main`) |
| `VAULT_SSH_KEY_FILE` | no | SSH private key file, for an SSH remote |
| `VAULT_HTTPS_TOKEN` | no | token for an HTTPS remote |
| `VAULT_POLL_SECONDS` | no | how often to poll the remote for changes made outside this process (default `60`) |
| `DATABASE_URL` | no | Postgres connection string; without it, search degrades to none and reindexing is skipped |
| `EMBEDDING_PROVIDER` | no | `none` (default), `ollama`, or `openai` |
| `EMBEDDING_URL` / `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` / `EMBEDDING_DIMENSIONS` | no | only used when `EMBEDDING_PROVIDER` is not `none` |
| `MEMORY_CLIENT` | no | commit author identity for this process's writes (default `claude-code`) |

## Add the server

From any directory, point Claude Code at the command that starts the server over stdio. Using
a checkout of this repository with `uv run --directory`:

```sh
claude mcp add --scope user memory \
  -e VAULT_REMOTE=git@memory.example.com:you/memory-vault.git \
  -e VAULT_DIR=/home/you/.local/state/memory-manager/vault \
  -- uv run --directory /path/to/memory-manager memory-manager serve --stdio
```

With a `uv tool install`ed `memory-manager` instead, drop `uv run --directory ...` and call the
installed binary directly:

```sh
claude mcp add --scope user memory \
  -e VAULT_REMOTE=git@memory.example.com:you/memory-vault.git \
  -e VAULT_DIR=/home/you/.local/state/memory-manager/vault \
  -- memory-manager serve --stdio
```

`--scope user` makes the server available in every project for this user; use `--scope local`
(the default) to add it for the current project only, or `--scope project` to share it with
collaborators via a committed `.mcp.json` (not applicable to a server carrying your own
`VAULT_*` credentials — those stay in `--scope local`/`user`). Add `DATABASE_URL`/`EMBEDDING_*`
with further `-e` flags if you have Postgres set up.

## Verify the connection

```sh
claude mcp list
```

prints one line per configured server, ending in `✔ Connected` once Claude Code has started
the process and completed the MCP handshake:

```
memory: uv run --directory /path/to/memory-manager memory-manager serve --stdio - ✔ Connected
```

A connected but unresponsive-looking server, or `✘ Failed`, means the process exited or wrote
something other than JSON-RPC to stdout — check with `/mcp` inside a Claude Code session, which
lists the same status plus any error Claude Code captured, and read the server's own log lines
(always on **stderr**; a stray line on stdout corrupts the stdio transport, see `cli.py`'s
`_serve`).

## Troubleshooting

- **stdout must be clean.** The stdio transport is the process's stdout; any `print()`,
  warning, or stray library log line that lands there breaks the connection. This server sends
  all logging to stderr (`logging.basicConfig(stream=sys.stderr)` in `cli.py`); if you wrap the
  command (a shell script, a different launcher), make sure that wrapper does the same.
- **`VAULT_REMOTE is required but not set` / similar.** A required `VAULT_*` or `EMBEDDING_*`
  variable is missing or malformed; the exact message names the variable
  (`src/memory_manager/config.py`).
- **Connects, but tools time out or are missing.** Run `/mcp` in the session for Claude Code's
  own diagnosis, and check the server's stderr log (`claude mcp list` does not show logs; a
  terminal running the same command directly does).
- **Instructions look truncated.** Claude Code truncates server `instructions` and tool
  descriptions past 2,048 characters; this server's `INSTRUCTIONS`
  (`src/memory_manager/mcp/instructions.py`) is kept under that limit on purpose — if you see
  truncation, something changed upstream, not here.
- **Removing a server:** `claude mcp remove <name>` (match the `--scope` it was added with).

## Add the memory skill too

The connection above only makes the `memory_*` tools *available*; the
[Claude Code skill and `CLAUDE.md` snippet](../../integrations/claude-code/README.md) are what
make Claude actually use them by the project's rules (one note per topic, `if_version` on every
write, never store secrets, and so on). Install the skill with:

```sh
mkdir -p ~/.claude/skills
cp -r integrations/claude-code/skills/memory ~/.claude/skills/memory
```

or copy it into a single project's `.claude/skills/memory/` instead. See
[`integrations/claude-code/README.md`](../../integrations/claude-code/README.md) for both
options.
