# memory-manager

**Status: pre-alpha.** Under active development, not yet usable.

A self-hosted, production-grade long-term memory for Claude that claude.ai (web/mobile) and
Claude Code share through one remote MCP server. Human-readable Markdown in Git is the source
of truth; Postgres (pgvector + full text) is a derived, rebuildable search index. A small set of
well-described tools plus built-in usage rules keeps the memory curated instead of cluttered.

## Architecture

```
claude.ai ──(HTTPS + OAuth)──┐
Claude Code ──(HTTP/stdio)───┼──► MCP server ──► write queue ──► Git vault (Markdown, source of truth) ──► remote
                             │        │                               │
                             │        └──► search ◄── indexer ◄───────┘ (after commit / webhook / poll)
                             │                 │
                             │           Postgres (pgvector + tsvector)
                             └── embeddings: pluggable (Ollama/bge-m3 | OpenAI-compatible | none)
```

See [`docs/PLAN.md`](./docs/PLAN.md) for the full goal, scope and architecture, and
[`docs/TASKS.md`](./docs/TASKS.md) for the current work backlog.

## Quickstart

Brings up memory-manager, Postgres+pgvector and a local, throwaway vault remote - good enough to
try the server without a real Git host or embedding provider yet.

```sh
podman compose up -d    # or: docker compose up -d
```

No `.env` file is needed for this - every value the stack needs to boot is a plain literal in
`compose.yaml`. The first start builds the image and runs `vault-init`, a one-shot container that
creates a local bare "remote" (`file:///data/remote.git`, in the `vault-remote` volume) and seeds
it with a welcome note - point `VAULT_REMOTE` in `compose.yaml` at a real Git remote instead to
skip this (a real deployment is expected to adapt its own copy of the file). Wait for `/readyz`
to answer:

```sh
curl http://localhost:8080/readyz
```

Create a bearer token (printed once - store it) and point a client at the server:

```sh
podman compose exec memory-manager \
  memory-manager token create me --scope memory:read --scope memory:write --namespace '*'

claude mcp add --transport http memory http://localhost:8080/mcp \
  --header "Authorization: Bearer <token>"
```

Embeddings are optional (full-text search works without one, `EMBEDDING_PROVIDER=none`). For a
local embedding provider, copy `.env.example` to `.env` and set `EMBEDDING_PROVIDER=ollama` there
(picked up automatically if `.env` exists, ignored if it does not), then start the `ollama`
profile too:

```sh
cp .env.example .env    # edit EMBEDDING_PROVIDER=ollama in it first
podman compose --profile ollama up -d
```

Tear the stack down again (including its volumes - the local vault remote and the Postgres
index are both gone with it):

```sh
podman compose down -v
```

`make up`/`make down` do the same with Podman directly.

## License

AGPL-3.0-only (see [`LICENSE`](./LICENSE)). In plain words:

- Self-hosting an unmodified copy for yourself, your team or your company carries no extra
  obligations beyond the license itself.
- Offering a **modified** version of this server as a network service to others requires
  publishing the source of that modified version to its users (AGPL §13).

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for how to set up the project, the branch/commit/PR
rules and the DCO sign-off required on every commit.
