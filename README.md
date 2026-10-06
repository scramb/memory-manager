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

## License

AGPL-3.0-only (see [`LICENSE`](./LICENSE)). In plain words:

- Self-hosting an unmodified copy for yourself, your team or your company carries no extra
  obligations beyond the license itself.
- Offering a **modified** version of this server as a network service to others requires
  publishing the source of that modified version to its users (AGPL §13).

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for how to set up the project, the branch/commit/PR
rules and the DCO sign-off required on every commit.
