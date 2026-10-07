# ADR-0007 — Storage: a `StorageBackend` interface with Git (default) and Postgres (enterprise) as source of truth

Status: Accepted · Date: 2026-10-07
Relates to: vault, queue, index, search, F-xx Enterprise Scale; [ADR-0003](./0003-git-access.md), [ADR-0005](./0005-note-format.md), [ADR-0008](./0008-namespace-permissions.md), [ADR-0009](./0009-stateless-replicas.md)

## Context

Today the Git vault is the source of truth, written by a single process through a serialized queue. Postgres is a derived index that `reindex --full` can rebuild. CLAUDE.md states two rules for this: **"Git is the source of truth"** and **"Never hard-delete notes"**. Enterprise mode needs:

- ~1M notes and ~5M chunks for 2,000 users.
- Write p95 < 200 ms, read p95 < 100 ms with ≥ 3 replicas.
- **Provable erasure** (GDPR Art. 17) of a note or a whole namespace, including every revision, chunk and embedding.

Facts from [`docs/research/enterprise.md`](../research/enterprise.md) §2–3:

- The Git write path is single-writer by construction: per-replica clone, write queue, poll, webhook, indexer hooks. Several writers race at `git push`.
- Git history is designed to be permanent. Erasure means rewriting history in every clone and remote, which can't be proven across mirrors.
- `FOR UPDATE SKIP LOCKED` with an outbox row in the write transaction measured about 3,800 jobs/s. That makes asynchronous embeddings cheap without a new service.
- 5M × 1024-dim vectors: about 41 GB HNSW with `vector` (one tuple per 8 KB page) versus about 14 GB with `halfvec`. Filtered HNSW needs `hnsw.iterative_scan`; hash partitions are not pruned by RLS predicates.
- A CNPG backup retention of 30 d sets the deletion horizon at about 37 days, plus bucket versioning. An ID-only erasure log can be replayed after a restore.

## Options

### A — Git stays the only source of truth, scaled (one repo per namespace, sharded writers)
Pro: keeps the core rule; humans can edit with Git or Obsidian · Con: needs thousands of repos or a sharded single-writer fleet; erasure would rewrite history in every repo; latency targets depend on `git push` round trips; nothing in the stack is built for this.

### B — Postgres is the source of truth in enterprise mode; Git stays the default; both behind one `StorageBackend` interface
- `storage.backend: git | postgres`.
- The Postgres backend stores notes in the same canonical format as ADR-0005: path, frontmatter fields, body, and `version` = SHA-256 of the canonical bytes.
- It adds an append-only `note_revisions` table (revision number, author `oid`, client, timestamp, content).
- Writes are serialized per note by `UPDATE … WHERE current_revision = $n` inside a transaction. The external `if_version` token **stays the content hash**, so the tool contract is unchanged; the revision number is internal.
- Optional Markdown export per namespace to Git or blob storage, for backup and portability. It is never read back.

Pro: horizontal writes; real erasure; latency within reach; Git users unaffected · Con: two backends to maintain; in enterprise mode humans edit through tools or export/import, not Git; changes two CLAUDE.md rules for that mode.

### C — Postgres for everything, Git backend removed
Pro: one code path · Con: breaks single-user and team operation, which must stay unchanged; loses the human-editable vault that sets the project apart.

## Decision

**B**, accepted by the owner on 2026-10-07, including the two CLAUDE.md rule changes under Consequences.

1. **Interface.** `StorageBackend` exposes `read`, `list`, `write(if_version)`, `edit`, `supersede`, `archive`, `promote`, `erase`, `export` and `changes_since(cursor)`. `queue.py` and `vault/repo.py` become the Git implementation; the MCP tools call only the interface.
   - Validation, secret scan, path safety, size cap and audit stay **above** the interface, so both backends enforce them identically.
2. **Postgres schema (enterprise).**
   - `notes`: current row per note, with `namespace_id`, path, canonical fields, body, `version` and `current_revision`.
   - `note_revisions`: append-only, written in the same transaction.
   - `chunks`: text, `tsvector`, `halfvec(1024)` embedding, model and dimension, partitioned by namespace kind.
   - `jobs`: outbox for embeddings, export and retention.
   - RLS on all content tables per [ADR-0008](./0008-namespace-permissions.md).
3. **Archive vs. erasure.**
   - **Archive** stays the normal "delete" for Claude: `memory_archive` moves the note to `_archive/` and nothing is lost. The CLAUDE.md rule keeps its meaning for every tool call.
   - **Erasure** is a separate, non-MCP operation: `/account` self-service, the admin area on `/account` ([ADR-0008](./0008-namespace-permissions.md)), and the retention job. In one transaction it hard-deletes the note or namespace, all revisions, chunks and jobs.
   - It writes an `erasure_log` row (IDs, actor, reason, time, no content). Audit rows of the erased objects keep only metadata.
   - After a backup restore or PITR, `erasure_log` is replayed before the API becomes ready.
   - Documented horizon: backup retention (default 30 d) + 7 d.
4. **Embeddings asynchronous.**
   - A write commits the note, the revision and its full-text chunks, then returns.
   - The `worker` pulls embedding jobs with `SKIP LOCKED`, using `LISTEN/NOTIFY` as a wake-up hint with polling as fallback.
   - Search fuses whatever vectors exist and falls back to full text per chunk, as the current fallback does.
5. **Index strategy** (to be confirmed by the load test and its own ADR when WP-level design starts):
   - `halfvec` HNSW (`m=16`, `ef_construction=64`).
   - `chunks` partitioned by namespace kind: personal uses a B-tree on namespace plus exact sort, group/project uses HNSW with `iterative_scan=relaxed_order`, org uses HNSW unfiltered.
   - Results are fused with RRF.
   - halfvec recall is checked against the golden set before it is adopted.
6. **Migration.** `memory-manager migrate git-to-postgres <namespace-map>` imports a Git vault losslessly: current notes byte-identical (same `version`), and Git history as revisions with the commit author and time. Archive notes stay archived. A dry run reports the mapping.
7. **Git backend unchanged:** single replica, Git as source of truth, derived index, `reindex --full`.

Checked against the guardrails:
- Few dependencies: none new (asyncpg, plain SQL, no queue library).
- OSS first: yes.
- Container: Postgres was already required; no new service.
- Technology pool: within ADR-0001.

## Consequences

- **CLAUDE.md must change** (owner decision): "Git is the source of truth" becomes "Git is the source of truth for the Git backend; with the Postgres backend, Postgres is, and `export` gives a portable copy". "Never hard-delete notes" becomes "No MCP tool hard-deletes; erasure is an audited, non-MCP operation".
- PLAN scope changes: "Hard deletion of notes" moves from "not in scope" to enterprise scope. `reindex --full` stays meaningful as "rebuild chunks from `notes`".
- The secret scan and validation code must work without a working tree; they already operate on bytes.
- In enterprise mode, human editing goes through the tools or `export` → edit → `import`. There is no live Git round trip.
- A future ADR covers the vector index and partitioning in detail once the load test exists.

## Reversibility

Expensive. Once enterprise data lives in Postgres, going back to Git means a full export and losing revision metadata. The interface itself is cheap, and Git deployments are untouched.
