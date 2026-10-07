# memory-manager

**Status: v0.1.0 — first release.**

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

## Features

- Vault: Markdown notes in Git as the source of truth, Postgres fully rebuildable from it.
- Writes carry `if_version`; conflicts surface the current content instead of overwriting it.
- Deletes are soft (archived to `_archive/`, never hard-deleted).
- Hybrid search: full-text + `pgvector`, fused with RRF; embeddings are optional (Ollama,
  OpenAI-compatible, or none — full-text-only fallback).
- MCP server over stdio (Claude Code) and Streamable HTTP (claude.ai and Claude Code), one
  instance for both.
- Embedded OAuth 2.1 authorization server with OIDC and password login, client ID metadata
  documents (CIMD), per-client and per-IP limits, and an audit log for every write.
- Static scoped bearer tokens as a lighter-weight alternative to OAuth.
- Vault import/export for migration and backup.
- Container images signed (cosign) with an SBOM published alongside each release.
- Deployment via Helm chart, Kustomize base, or a Flux example.

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

## Connect

Remote setup for both clients (OAuth, verified 2026-10-07): [`docs/guides/remote-connect.md`](./docs/guides/remote-connect.md).

- **Claude Code**: see [`docs/guides/claude-code.md`](./docs/guides/claude-code.md) for the
  stdio transport (`claude mcp add`) and the environment variables the server reads.
- **claude.ai**: add memory-manager as a custom connector under *Settings → Connectors → Add
  custom connector*, with the URL `https://memory.example.com/mcp` (your own deployment's
  hostname, Streamable HTTP). OAuth login happens in the browser against the server's embedded
  authorization server; no token to copy and paste.

## Deploy

- Kubernetes manifests (Kustomize base): [`deploy/README.md`](./deploy/README.md)
- Helm chart: [`charts/memory-manager/README.md`](./charts/memory-manager/README.md)
- GitOps example with Flux: [`deploy/flux/README.md`](./deploy/flux/README.md)
- Exposing a self-hosted instance without a public ingress (Cloudflare Tunnel):
  [`docs/guides/cloudflare-tunnel.md`](./docs/guides/cloudflare-tunnel.md)
- Cutting and verifying a release (image and chart signatures, SBOM):
  [`docs/releasing.md`](./docs/releasing.md)

## Security

- Policy, scope and how to report a vulnerability: [`SECURITY.md`](./SECURITY.md)
- Review against the OWASP Top 10 for LLM Applications, threat model and findings:
  [`docs/security-review.md`](./docs/security-review.md)

## Comparison

A sourced comparison against mem0, Basic Memory and Graphiti/Zep lives in
[`docs/research/comparison.md`](./docs/research/comparison.md). Compact excerpt:

| | **memory-manager** | **mem0 (OSS)** | **Basic Memory** | **Graphiti (OSS) / Zep (Cloud)** |
|---|---|---|---|---|
| License | AGPL-3.0-only | Apache-2.0 | AGPL-3.0 | Graphiti Apache-2.0 · Zep Cloud proprietary SaaS |
| Source of truth | Markdown files in a Git repo; Postgres is a derived index | Vector store rows (pgvector, Qdrant, …) | Markdown files on disk; SQLite/Postgres index | Graph DB (Neo4j/FalkorDB/Neptune); episodes as provenance |
| Human-editable | Yes (plain Markdown, Git history) | No (DB/dashboard only) | Yes (Markdown, Obsidian-compatible) | No (graph) |
| Search | FTS + pgvector, fused with RRF; FTS-only fallback | Semantic + BM25 + entity matching, fused | FTS + vector hybrid (FastEmbed default), optional reranker | Semantic + BM25 + graph traversal, with reranking |
| Write model | Explicit, curated tool writes with `if_version` | LLM extraction by default (`infer=True`), ADD-only | Explicit tool writes; optional `expected_checksum` | LLM extraction of entities/facts from episodes |
| LLM required | No (embeddings optional: Ollama/OpenAI-compatible/none) | Yes for default extraction | No (local FastEmbed embeddings) | Yes (structured-output LLM + embedder) |
| MCP | stdio + Streamable HTTP, one server for claude.ai and Claude Code | Hosted MCP only; self-hosted server has no MCP; OpenMemory sunset | stdio, streamable-http, SSE | Graphiti: experimental HTTP/stdio · Zep: hosted Context MCP |
| OAuth for claude.ai | Embedded OAuth 2.1 AS | Hosted platform only | Cloud only (WorkOS AuthKit) | Zep Cloud (OAuth 2.1 + PKCE); Graphiti MCP: none documented |
| Self-host footprint | 1 container + Postgres 16/pgvector + Git remote | FastAPI + Postgres/pgvector + dashboard + LLM key | `uvx basic-memory` (Python 3.12), SQLite | Graphiti: graph DB + LLM key · Zep BYOC: Enterprise plan only |

## License

AGPL-3.0-only (see [`LICENSE`](./LICENSE)). In plain words:

- Self-hosting an unmodified copy for yourself, your team or your company carries no extra
  obligations beyond the license itself.
- Offering a **modified** version of this server as a network service to others requires
  publishing the source of that modified version to its users (AGPL §13).

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for how to set up the project, the branch/commit/PR
rules and the DCO sign-off required on every commit.
