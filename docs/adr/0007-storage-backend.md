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

## Addendum 2026-10-07 — full-text search with very frequent terms (#117)

The load test (#108) showed the cost of OR semantics: a query containing a term that occurs in every chunk ranked all chunks, which took 1.3 s at 60k chunks and 8.8 s at 1M rows.

The owner decided on 2026-10-07 to keep the ranking unchanged and to bound only the candidate stage.

**Detecting frequent terms**
- Very frequent lexemes are recognised from the planner statistics: `pg_stats.most_common_elems` × `pg_class.reltuples`.
- Those lexemes are left out of the match condition, so their posting lists are never read.
- Ranking runs only over a hard-capped candidate set.

**Access from the app role**
- Under RLS, the non-owner app role cannot see `pg_stats` rows for `chunks`.
- A `SECURITY DEFINER` function owned by the schema owner therefore answers only one question: which of the given lexemes exceed the frequency threshold.
- This discloses that a term is very common across all namespaces. It discloses nothing about rare terms or about any content.

**Limitations**
- Detection depends on `ANALYZE` having run. Bulk loads and `reindex --full` must analyze `chunks`.
- Without statistics, only the cap bounds the work.

**Rejected:** AND-first with an OR fallback. It changes ranking semantics for every vault, and for natural-language questions the OR fallback still reads the saturated posting lists.

## Addendum 2026-10-09 — skipping the #117 retry once the vector side already has enough (#291)

WP-32's load test (#291) found the #117 retry above itself as the dominant latency cost for a vector-only hybrid search: a query engineered to have no lexical match at all starves the selective-filtered attempt the same way a frequent-word query does, pays the full retry, and gets nothing for it once a vector leg already covers the same query. Measured on the WP-32 load-test vault (50k chunks/10k notes): ~135 ms per retry, pushing `search_vector_only`'s p95 (361–617 ms) over F-01's 300 ms search budget (`docs/benchmarks/vector-only-search.md`).

The owner's standing approval for recommendations that do not change F-01's functional scope covers this change (same basis as the original #117 decision above).

**Decision:** `search.py`'s `hybrid_search` runs its vector legs *before* the full-text side and skips the #117 retry only once those legs already returned at least `limit` chunks (the final note count the call returns) - an outcome-based condition, not "an embedding exists". A vector side that is itself starved (RLS hiding every row of every visible kind, a `chunks.model`/dimension mismatch, or genuinely nothing near the query) still gets the retry, exactly like a plain full-text-only call always did.

**What changes:** no schema change, no change to the #117 detection/capping mechanism itself (`mm_frequent_lexemes`, `_CANDIDATE_CAP`) - only whether `fulltext_search`'s already-existing `retry_unfiltered` escape hatch fires for one specific caller (`_hybrid_search_impl`), and only once its own vector legs make the retry moot.

**Why not "skip whenever an embedding exists":** an earlier draft of this fix keyed the skip on `provider is not None`/`embedding is not None` alone. WP-32's own load test exposed why that is unsafe on its own: a mismatch between the load test's loader and the server's `EMBEDDING_MODEL` made every vector leg return zero rows (`vector_search`'s `c.model = $n` filter excluding every stored chunk), and the retry-skip still fired - a search that structurally finds nothing looked fast for the wrong reason, not because the fix worked. Gating on the vector side's actual outcome catches that failure mode instead of hiding it.

**Measured effect:** `docs/benchmarks/vector-only-search.md` records the before/after per-stage timings and a k6 correctness check (#291) added alongside the existing latency scenarios. A first version of that check sampled hit/miss from the load scenarios' own random (principal, query) draws and proved unreliable (0–14 samples per run, flaky at that size - a second verification round's own finding). The version actually shipped (`search_correctness_lexical`/`search_correctness_vector_only`, `search.js`) instead runs a fixed, visibility-chosen set of 50 lexical and 50 vector-only query/principal pairs as a dedicated, non-timed scenario, gated on both hit rate (`rate>=0.9`) and sample count (`count>=50`) - the latter is what makes a run with too few (or zero) samples fail loud rather than pass by default. Three consecutive `make loadtest-smoke` runs each measured 50/50 (100 %) on both checks; reproducing the `chunks.model` mismatch by hand dropped `check:vector` to 2/50 (4 %), crossing its threshold and failing the build as intended, while `check:lexical` (no embedding dependency) stayed 50/50 - reverted immediately after, no tracked file left in that state.

**Eval:** `make eval`'s recall@5/MRR on the golden set is unaffected - the golden set runs against the Git backend (no ADR-0016 per-kind legs, no load-test loader), and the retry-skip condition never changes which candidates `fulltext_search`/`vector_search` themselves find, only whether a already-redundant second full-text statement runs.

**Rejected:** keeping the unconditional `embedding is not None` skip (hides exactly the regression this addendum exists to fix); reverting to always running the retry regardless of the vector side's outcome (restores the latency cost #291 set out to remove, for the common case where the vector side already has a good answer).

Two points of §3 were incomplete. The owner decided on 2026-10-08:

**What erasing a user removes**
- The personal namespace with every note, revision, chunk and job, and the user's identity rows, are hard-deleted.
- Notes the user wrote in shared namespaces stay, because they belong to the group, project or org. The author fields of that user's revisions (`author_oid` and the display author) are set to `erased`.
- Audit rows keep only metadata. For erased notes, the path is redacted as well, because a slug can carry personal data.
- Rejected: keeping the `oid` as a pseudonymous author (it stays linkable to a person) and erasing shared notes too (teams would lose knowledge they own).

**Replay after a restore**
- A restore or PITR rolls back `erasure_log` together with the data, so the table alone cannot replay what it lost.
- Every `erasure_log` row (IDs, actor, reason, time, no content) is therefore also emitted through the audit/SIEM export.
- After a restore, the operator passes the exported rows as a JSONL file in `ERASURE_LOG_REPLAY_FILE`. The server replays them before `/readyz` turns true. The restore runbook documents the step.
- Rejected: writing the log to the backup object store (a cloud SDK dependency per provider) and a second database excluded from restores (one more database to operate).
