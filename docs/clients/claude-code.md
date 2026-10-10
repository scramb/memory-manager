# Connecting Claude Code to memory-manager

Claude Code adds the `memory-manager` server either locally over stdio (its own process, own
`VAULT_*` credentials) or against a deployed server over HTTP with OAuth or a static token. The
examples use `https://memory.example.com`; replace it with your `PUBLIC_URL`.

Checked against `claude --version` **2.1.280** on **2026-10-06** (stdio) and the HTTP+OAuth flow
on **2026-10-07** against memory-manager **0.1.2**. Command syntax also recorded in
[`docs/research/mcp-auth-and-connectors.md`](../research/mcp-auth-and-connectors.md#5-claude-code-k1).

## Tested version

- stdio: `claude --version` 2.1.280 on 2026-10-06.
- HTTP + OAuth: verified 2026-10-07 against memory-manager 0.1.2 — Claude Code connected and
  found notes written from claude.ai through `memory_search`.

## Setup

`memory-manager connect claude-code --url <url>` merges an HTTP entry into `~/.claude.json`
(`.mcp.json` with `--scope project`) for you — it prints a diff, backs up the file it changes,
and is a no-op if the server is already configured the way it would write it; pass
`--transport stdio`, `--token-env <VAR>`/`--inline-token`, or `--with-instructions` for the
`.claude/rules/memory-manager.md` file described below (`docs/research/clients/claude-code.md`
for the exact file format this relies on). The manual `claude mcp add` steps below do the same
thing through Claude Code's own CLI instead.

### Global

stdio, against a local checkout or an installed binary. Before connecting, the server needs:

- A **Git remote for the vault** it can push to and pull from — any host it can reach over SSH
  or HTTPS (for example `git@memory.example.com:you/memory-vault.git`), or a local bare
  repository for trying this out (`git init --bare /path/to/vault-remote.git`).
- `uv` installed (<https://docs.astral.sh/uv/>) and a checkout of this repository, or the
  package installed with `uv tool install --from /path/to/memory-manager memory-manager`
  (`uv tool install memory-manager` once the package is published).
- Optional: a PostgreSQL 16+ database with `pgvector` for full-text/vector search
  (`memory_search`). Without it, note read/write still work; search degrades.
- Optional: an embedding provider (Ollama or an OpenAI-compatible endpoint) for vector search on
  top of full-text. Without one, search is full-text only.

With a checkout of this repository, using `uv run --directory`:

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

`--scope user` makes the server available in every project for this user. Add
`DATABASE_URL`/`EMBEDDING_*` with further `-e` flags if you have Postgres set up — see
"Environment variables" below for the full table.

HTTP, against a deployed server:

```sh
claude mcp add --transport http --scope user memory https://memory.example.com/mcp
```

Then run `/mcp` in a session, choose `memory`, and select **Authenticate** to run the OAuth login
in the browser. Without a browser, create a static token instead and pass it as a header:

```sh
memory-manager token create my-laptop --scope memory:read --scope memory:write --namespace '*'
claude mcp add --transport http --scope user memory https://memory.example.com/mcp \
  --header "Authorization: Bearer <token>"
```

### Project

`--scope project` shares the server with collaborators via a committed `.mcp.json`, without
credentials in it:

```sh
claude mcp add --transport http --scope project memory https://memory.example.com/mcp
```

This does not apply to the stdio form carrying `VAULT_*` credentials — those stay in
`--scope local`/`user`. Each collaborator authenticates (or supplies a header) themselves; see
"Org rollout" below.

## Environment variables

The stdio form reads these from the process environment (`src/memory_manager/config.py`),
passed with `-e` on `claude mcp add`:

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

## Auth variants

| Variant | Notes | Availability |
|---|---|---|
| OAuth over HTTP (own CIMD) | `/mcp` → **Authenticate**, or `claude mcp login <name>`; Claude Code uses its own CIMD and loopback redirect, not the claude.ai callback | Default for HTTP |
| Pre-registered client | `--client-id`, `--client-secret` (masked prompt), `--callback-port` to fix the loopback port for a pre-registered redirect | When the AS requires a known client |
| Static token | `--header "Authorization: Bearer <token>"`, created with `memory-manager token create` | No browser available |
| `headersHelper` | A script emits headers at connect time; configuring an `Authorization` header yourself turns a 401/403 into a plain failure instead of triggering OAuth | Custom auth |
| stdio credentials | `VAULT_*`/`DATABASE_URL`/`EMBEDDING_*` passed with `-e`, taken from the environment, no MCP auth flow | stdio only |

`--no-browser` prints the sign-in URL instead of opening it, for SSH sessions. Claude Code
refreshes on 401 and retries once; a server is flagged as needing auth on 401 or 403.

## Instructions file

Claude Code loads server `instructions` automatically at session start, truncated at
**2,048 characters** (this server's `INSTRUCTIONS` is kept under that limit on purpose). MCP
prompts, including `memory_guide`, appear as slash commands
(`/servername:promptname (MCP)` or `/mcp__server__prompt`). To keep the usage rules in reach even
when `instructions` is not read, install the skill described in
[`../../integrations/claude-code/README.md`](../../integrations/claude-code/README.md).

## Known limits

- `instructions` and tool descriptions truncated at **2,048 characters** each.
- Tool output: `MAX_MCP_OUTPUT_TOKENS` (documented as 25,000 tokens) and `MCP_TOOL_TIMEOUT`.
- stdio transport requires a clean stdout: any stray `print()` or library log line on stdout
  breaks the connection (see "Troubleshooting").

## Org rollout

`--scope project` writes a shared `.mcp.json` collaborators can commit, carrying only the server
URL and transport — no credentials. Each collaborator then authenticates with their own OAuth
session or supplies their own static token/header; the `.mcp.json` itself stays free of secrets.

## Troubleshooting

- **stdout must be clean.** The stdio transport is the process's stdout; any `print()`, warning,
  or stray library log line that lands there breaks the connection. This server sends all
  logging to stderr; a wrapping shell script or launcher must do the same.
- **`VAULT_REMOTE is required but not set` / similar.** A required `VAULT_*` or `EMBEDDING_*`
  variable is missing or malformed; the exact message names the variable
  (`src/memory_manager/config.py`).
- **Connects, but tools time out or are missing.** Run `/mcp` in the session for Claude Code's
  own diagnosis, and check the server's stderr log (`claude mcp list` does not show logs).
- **Instructions look truncated.** Expected past 2,048 characters; this server's own
  `INSTRUCTIONS` is kept under that limit.
- **Removing a server:** `claude mcp remove <name>` (match the `--scope` it was added with).

`doctor --client claude-code` is planned (#138) and will cover both transports once it ships.
In the meantime, `claude mcp list` prints one line per configured server, ending in
`✔ Connected` once the MCP handshake completes.

## Check across clients

Save a fact in claude.ai ("Remember: my favourite editor is Zed"). Then ask Claude Code ("Which
editor do I prefer?"). Claude Code should call `memory_search` and find the note.
