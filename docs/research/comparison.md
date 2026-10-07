# memory-manager compared with mem0, Basic Memory and Zep/Graphiti

Retrieved: 2026-10-07. Versions checked on the day: mem0ai 2.2.1 on PyPI [M1], basic-memory 0.23.2 (released 2026-08-25) [B1],
graphiti-core 0.30.2 (released 2026-09-08) [G1], Graphiti MCP server 1.1.0 [G3], zep-cloud SDK 3.30.0 [Z1].
memory-manager's embedded OAuth 2.1 server is merged into `main` [MM2]; v0.1.0 is its first release.

## 1. Comparison table

| | **memory-manager** | **mem0 (OSS)** | **Basic Memory** | **Graphiti (OSS) / Zep (Cloud)** |
|---|---|---|---|---|
| License | AGPL-3.0-only | Apache-2.0 | AGPL-3.0 | Graphiti Apache-2.0 · Zep Cloud proprietary SaaS |
| Source of truth | Markdown files in a Git repo; Postgres is a derived index | Vector store rows (pgvector, Qdrant, …) | Markdown files on disk; SQLite/Postgres index | Graph DB (Neo4j/FalkorDB/Neptune); episodes as provenance |
| Human-editable | Yes (plain Markdown, Git history) | No (DB/dashboard only) | Yes (Markdown, Obsidian-compatible) | No (graph) |
| Search | FTS + pgvector, fused with RRF; FTS-only fallback | Semantic + BM25 + entity matching, fused | FTS + vector hybrid (FastEmbed default), optional reranker | Semantic + BM25 + graph traversal, with reranking |
| Write model | Explicit, curated tool writes with `if_version` | LLM extraction by default (`infer=True`), ADD-only | Explicit tool writes; optional `expected_checksum` | LLM extraction of entities/facts from episodes |
| LLM required | No (embeddings optional: Ollama/OpenAI-compatible/none) | Yes for default extraction | No (local FastEmbed embeddings) | Yes (structured-output LLM + embedder) |
| MCP | stdio + Streamable HTTP, one server for claude.ai and Claude Code | Hosted MCP only; self-hosted server has no MCP; OpenMemory sunset | stdio, streamable-http, SSE | Graphiti: experimental HTTP/stdio · Zep: hosted Context MCP |
| OAuth for claude.ai | Embedded OAuth 2.1 AS (in progress, WP-11) | Hosted platform only | Cloud only (WorkOS AuthKit) | Zep Cloud (OAuth 2.1 + PKCE); Graphiti MCP: none documented |
| Self-host footprint | 1 container + Postgres 16/pgvector + Git remote | FastAPI + Postgres/pgvector + dashboard + LLM key | `uvx basic-memory` (Python 3.12), SQLite | Graphiti: graph DB + LLM key · Zep BYOC: Enterprise plan only |

## 2. Details

### memory-manager
- Markdown in Git is the source of truth. Postgres (pgvector + tsvector) is derived and can be rebuilt. Search fuses `ts_rank` and vector results with RRF. Embedding providers are Ollama, OpenAI-compatible or none [MM1].
- Writes go through a serial write queue. The queue compares `if_version` against the current content hash and returns a conflict together with the current content. It also runs a secret scan and writes an audit record [MM1].
- The MCP server runs over Streamable HTTP and stdio and serves claude.ai and Claude Code from one instance [MM1]. claude.ai custom connectors offer OAuth, authless, or static request headers (beta, limited orgs), which makes an embedded OAuth server the practical route [C1]. That OAuth server is on `main` as of v0.1.0 [MM2].
- Limits: some built-in rules exist (`memory_guide` prompt, server instructions), but nothing extracts memories automatically: Claude must decide to write. There is a single vault per server [MM1].

### mem0 (open source) and OpenMemory
- License is Apache-2.0 [M2]. v3 changed extraction to a "single-pass ADD-only" model: one LLM call, no UPDATE/DELETE, so memories accumulate. Retrieval fuses semantic, BM25 and entity signals [M2][M3]. Note: the docs call this "v3" while the Python package is numbered 2.2.1 (the TS SDK is ts-v3.3.1). I could not establish exactly how the docs' "v3" maps to a PyPI version [M1][M3].
- Graph memory was removed from the OSS SDK in v3 and is now a Platform-only feature [M3]. `Memory.add(..., infer=True)` uses an LLM by default to extract facts. `infer=False` stores the raw input [M4].
- The self-hosted server is FastAPI plus `pgvector/pgvector:pg17` plus a Next.js dashboard. Auth is on by default (JWT for the dashboard, `X-API-Key` for the API). `.env` needs `OPENAI_API_KEY` at minimum, and telemetry defaults to `true` [M5][M6].
- MCP: the documented Mem0 MCP is hosted (`https://mcp.mem0.ai/mcp`, OAuth or API key), and memories live in the Mem0 account [M7]. MCP for the self-hosted server is an open PRD (#6370) [M8].
- **OpenMemory** (local MCP + UI) was sunset (#4923) and removed from the monorepo on 2026-07-29. A read-only snapshot remains in `mem0ai/openmemory`, and `server/` is named as the supported self-host path [M9]. The `mem0ai/openmemory` repo name now hosts an unrelated session-porting CLI [M10].
- Limits: the published benchmark numbers come from the managed platform with "proprietary optimizations not available in the open-source SDK" [M2]. Content is not human-editable outside the dashboard or API.

### Basic Memory (basicmachines-co)
- License is AGPL-3.0 [B2]. Knowledge is stored as Markdown files with frontmatter, wikilinks and observations. The same files work in Obsidian, and the index lives in SQLite (Postgres is optional) [B2].
- Search is hybrid full-text + vector. FastEmbed `bge-small-en-v1.5` is the default local embedder, with OpenAI or LiteLLM (custom `api_base`) as alternatives. Semantic search is on when its dependencies are present. A cross-encoder reranker is optional [B2][B3].
- MCP transports are `stdio` (default), `streamable-http` and `sse` [B4]. Remote HTTPS with sign-in is the hosted Basic Memory Cloud ($15/mo, WorkOS AuthKit). The OSS HTTP transport has no OAuth server; I found only cloud-CLI OAuth code, so this is my reading and not a documented statement [B2][B5].
- Writes are explicit tool calls (`write_note`, `edit_note`, `move_note`, `delete_note`). `write_note` refuses to overwrite by default and accepts `expected_checksum` for optimistic concurrency [B6]. `delete_note` is "permanent and cannot be undone" [B7].
- Limits: there is no built-in Git. Cross-device sync for local installs is "Manual (Git, Syncthing, etc.)" [B2]. 0.23 needs `--prerelease=allow` because it depends on a FastMCP 4 pre-release [B2]. Sharing with claude.ai in practice means Cloud or exposing HTTP yourself.

### Graphiti (open source) and Zep (Cloud)
- **Graphiti** is Apache-2.0 [G1]. It is a temporal knowledge graph: episodes in, LLM-extracted entities and facts out, with bi-temporal validity and automatic fact invalidation. Retrieval is hybrid semantic + BM25 + graph traversal [G2].
- Graphiti requires Neo4j 5.26, FalkorDB, or Neptune + OpenSearch (Kuzu is deprecated). It also needs an LLM with structured output (OpenAI by default; Anthropic, Gemini, Groq, or Ollama via OpenAI-compatible) [G2].
- The **Graphiti MCP server** calls itself "experimental". It defaults to HTTP at `/mcp/` (stdio optional), runs FalkorDB in a combined container, and offers `add_memory`, `add_triplet` (which bypasses extraction), search, and delete tools [G3]. The README documents no auth mechanism [G3].
- **Zep Community Edition** has been deprecated since 2025-04-02 and is unsupported. Its code moved to `legacy/` [Z2][Z3].
- **Zep Cloud** offers a Context MCP Server at `https://api.getzep.com/mcp` with OAuth 2.1 + PKCE through an identity provider. It works with Claude, Claude Code, ChatGPT and Cursor, and write tools are disabled by default on some connections [Z4]. Self-hosting (BYOC) is Enterprise-only. The free tier is 10k credits/month with 1 MCP seat [Z5].
- Limits: none of the stored content is human-editable files. Every ingest costs LLM calls, and the README warns about 429 rate limits [G2].

## 3. When to choose which

- **memory-manager**: choose it if you want one self-hosted memory that claude.ai (web/mobile) and Claude Code both use through OAuth. It also fits when every memory should be a reviewable Markdown file with Git history, writes should be deliberate and conflict-checked, and no LLM should be needed on the server. v0.1.0 is its first release, so expect rough edges.
- **Basic Memory**: choose it for a mature, local-first Markdown knowledge base, especially alongside Obsidian, with a single user on stdio clients. It is also the closest in philosophy. To reach claude.ai from it you either pay for Basic Memory Cloud or put your own auth in front of its HTTP transport.
- **mem0**: choose it when you build an application or agent that should learn user facts from conversations automatically, with LLM extraction, and when an opaque vector store is acceptable. For Claude clients over MCP today, that effectively means the hosted platform.
- **Graphiti / Zep**: choose these when you need temporal reasoning over changing facts and entity relationships, for example "what was true when". This costs a graph DB plus LLM calls on every ingest (Graphiti), or a managed or Enterprise contract (Zep).

## Sources
- [MM1] https://github.com/scramb/memory-manager/blob/main/docs/PLAN.md
- [MM2] https://github.com/scramb/memory-manager/commits/main (OAuth merged via WP-11 #71; v0.1.0 is the first tagged release)
- [C1] https://claude.com/docs/connectors/building/authentication
- [M1] https://pypi.org/project/mem0ai/
- [M2] https://github.com/mem0ai/mem0/blob/main/README.md
- [M3] https://docs.mem0.ai/migration/oss-v2-to-v3
- [M4] https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py (`infer` parameter of `add`)
- [M5] https://github.com/mem0ai/mem0/blob/main/server/README.md
- [M6] https://github.com/mem0ai/mem0/blob/main/server/docker-compose.yaml
- [M7] https://docs.mem0.ai/platform/mem0-mcp
- [M8] https://github.com/mem0ai/mem0/issues/6370
- [M9] https://github.com/mem0ai/mem0/pull/6530
- [M10] https://github.com/mem0ai/openmemory
- [B1] https://pypi.org/project/basic-memory/ · https://github.com/basicmachines-co/basic-memory/releases/tag/v0.23.2
- [B2] https://github.com/basicmachines-co/basic-memory/blob/main/README.md
- [B3] https://github.com/basicmachines-co/basic-memory/blob/main/docs/semantic-search.md
- [B4] https://github.com/basicmachines-co/basic-memory/blob/main/src/basic_memory/cli/commands/mcp.py
- [B5] https://github.com/basicmachines-co/basic-memory/blob/main/src/basic_memory/mcp/server.py
- [B6] https://github.com/basicmachines-co/basic-memory/blob/main/src/basic_memory/mcp/tools/write_note.py
- [B7] https://github.com/basicmachines-co/basic-memory/blob/main/src/basic_memory/mcp/tools/delete_note.py
- [G1] https://pypi.org/project/graphiti-core/ · https://github.com/getzep/graphiti/releases/tag/v0.30.2
- [G2] https://github.com/getzep/graphiti/blob/main/README.md
- [G3] https://github.com/getzep/graphiti/blob/main/mcp_server/README.md
- [Z1] https://pypi.org/project/zep-cloud/
- [Z2] https://github.com/getzep/zep/blob/main/README.md
- [Z3] https://www.getzep.com/blog/announcing-a-new-direction-for-zeps-open-source-strategy/
- [Z4] https://help.getzep.com/context-mcp-server
- [Z5] https://www.getzep.com/pricing
