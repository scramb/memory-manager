# ADR-0016 — Vector index for `chunks`: `halfvec`, a B-tree on namespace, partitioning by namespace kind

Status: Proposed · Date: 2026-10-08
Relates to: index, search, storage, F-01 Enterprise Scale (M8, WP-23); [ADR-0007](./0007-storage-backend.md),
[ADR-0008](./0008-namespace-permissions.md), [ADR-0013](./0013-agent-identity.md)

## Context

[ADR-0007](./0007-storage-backend.md) §5 named an index strategy "to be confirmed
by the load test and its own ADR when WP-level design starts": `halfvec` HNSW
(`m=16`, `ef_construction=64`), `chunks` partitioned by namespace kind (personal
exact via B-tree, group/project HNSW with `iterative_scan=relaxed_order`, org
unfiltered HNSW), results fused with RRF. That starting point rested on a 100k/
200k-row lab run (`docs/research/enterprise.md` §1/§3, 2026-10-07) and left three
things `[unverified]`: halfvec's recall loss on the real embedding model, build
cost at something closer to the 5M-chunk target, and the `project`/`agent`
namespace kinds ADR-0008/ADR-0013 added since.

Issue #125 re-scoped this into the spike that opens WP-23, because the `chunks`
schema WP-23 builds depends on its outcome (#220, #221 are blocked on this ADR
being accepted). Fixed inputs from the owner (2026-10-08): the embedding model is
`openai/text-embedding-3-large` through OpenRouter, `EMBEDDING_DIMENSIONS` default
1024, pinned at first migration and immutable afterwards (PLAN O23); `chunks` is
shared with the Git backend (ADR-0007), so whatever this ADR decides must say what
the Git backend keeps.

`docs/research/vector-index.md` (this spike, 2026-10-08) measured `vector(1024)`
and `halfvec(1024)` at 1,000,000 synthetic chunks, `halfvec(3072)` at 300,000 (the
issue's own fallback: measure smaller and extrapolate when 1M is too slow), and
checked recall on the real golden set and on synthetic vectors under RLS at
various `hnsw.ef_search`/`iterative_scan` settings and namespace-kind selectivity.
One finding reshapes ADR-0007's own starting point: **on a flat, unpartitioned
table, today's planner already routes every selective namespace filter around the
HNSW index to a sequential scan** (exact but slow: 71-103 ms at 1M rows), and
forcing the index with `iterative_scan=relaxed_order` is exact but *slower still*
(265-372 ms for a single personal namespace - over the project's own 300 ms search
budget). A plain **B-tree on `namespace`** turns the same selective query into an
exact, 4-15 ms bitmap-index lookup. ADR-0007 §5's "personal: B-tree on namespace,
exact sort" line said this already; this spike is the first measurement of it, and
it is the single biggest latency lever this ADR has to offer - bigger than the
`vector`-vs-`halfvec` choice itself.

## Options

### A — `vector`, one flat HNSW index, no B-tree, no partitioning (today's schema, unchanged)
Pro: zero schema change - what `index/indexer.py`'s `ensure_vector_index` already
does.
Con: 3× the index size of `halfvec` at 1024 dim (7.63 vs. 2.54 GiB at 1M rows,
`docs/research/vector-index.md` §1); every namespace-filtered query pays a full
sequential scan today (71-103 ms at 1M rows, growing with the table); no namespace
kind column to route `personal`/`agent` away from HNSW at all.

### B — `halfvec(1024)`, B-tree on `namespace`, list-partitioned `chunks` by namespace kind, tuned HNSW per kind, RRF-fused
- `chunks.embedding halfvec(1024)` (ADR-0007's own starting point; §1 confirms the
  3× size win and finds the ~3.4× build-time cost real, not a one-off).
- A B-tree on `chunks (namespace)` - the measured fix for selective filters
  (4-15 ms vs. 71-372 ms, `docs/research/vector-index.md` §1's main finding).
- `chunks` partitioned `list (namespace_kind)`: `personal`/`agent` (the B-tree
  above + exact sort, no HNSW - both are one-owner namespaces), `group`/`project`
  (HNSW, `hnsw.iterative_scan = relaxed_order`, `hnsw.ef_search` 100-200,
  `hnsw.max_scan_tuples` 20,000-50,000 - §3's recommended starting parameters),
  `org` (HNSW, unfiltered, generously tuned `ef_search`, to be validated against
  real content in WP-32 rather than this spike's worst-case synthetic numbers).
- A search issues one vector query per namespace kind the caller can read and
  fuses them with RRF - already the plan for full-text/vector fusion, so this adds
  no new fusion step, only more vector legs when more than one kind is visible.
- `agent` (ADR-0013) attaches as a fifth partition additively
  (`create table chunks_agent partition of chunks for values in ('agent')`), no
  change to the four already in place (`docs/research/vector-index.md` §5).

Pro: smallest index footprint; fastest and exact for the narrowest (and most
common, ADR-0008: default write target is `me`) selectivity; HNSW graphs for
`group`/`project`/`org` stay uncluttered by one-owner rows; every piece measured
in this spike, not assumed.
Con: ~3.4× longer index build (63 min vs. 18.5 min at 1M rows - plan for an
offline/maintenance-window build or rebuild, matching ADR-0007 §5's own "halfvec
build ran slow" flag and pgvector's own "create the index after loading initial
data" guidance); one-to-several vector legs per search instead of one when a
caller can read several kinds (mitigated: RRF fusion already happens for
full-text; most callers see `me` plus at most a handful of kinds).

### C — `halfvec(3072)` native dimensions instead of 1024
Pro: no re-truncation of the embedding model's own output; marginally higher raw
embedding fidelity.
Con: **no index-size advantage over option A's `vector(1024)`** - measured at
8,192 B/row, identical to `vector(1024)`, because a 6,152-byte `halfvec(3072)`
value hits the same one-tuple-per-8KB-page boundary `vector(1024)` does
(`docs/research/vector-index.md` §2); ~4× `halfvec(1024)`'s already-slow build
time at the same row count; the owner's O23 decision already pins
`EMBEDDING_DIMENSIONS` at 1024, immutable without a reindex, making this a
reindex-triggering change with no measured upside.

## Recommendation

**B**, for the owner to accept or reject. Checked against the guardrails:
- Few dependencies: none new - `halfvec` ships in the `vector` extension already
  required (ADR-0001, ADR-0007); a B-tree is a built-in PostgreSQL index type.
- OSS first: yes (PostgreSQL License, same as `vector`).
- Container: unchanged, same `pgvector/pgvector` image family already required
  (pgvector ≥ 0.8.4 per ADR-0007 §5; this spike ran 0.8.7).
- Technology pool: SQL/Python per ADR-0001; this ADR changes no application
  language boundary.

Option C is not recommended: `docs/research/vector-index.md` §2 found no size
advantage over keeping `vector(1024)` (option A) at all, let alone over option B,
so there is no measured reason to spend O23's reindex cost on it.

## Consequences

- **The `chunks` migration WP-23 writes (#220/#221) gets a fourth column it did
  not have before this ADR: `namespace_kind`** (or an equivalent denormalisation
  of `namespaces.kind` onto `chunks`/`notes`), needed as the partition key and the
  per-kind query router. ADR-0008 already added `namespace_kind` as a search
  result field; this ADR additionally makes it part of the storage layout.
- **`index/indexer.py`'s `ensure_vector_index`/`_apply_embeddings` and
  `search.py`'s `vector_search`/`hybrid_search`** need the `postgres`-backend
  equivalents of: creating the partitioned table and per-kind indexes (B-tree for
  `personal`/`agent`, HNSW with the parameters above for `group`/`project`/`org`),
  and issuing one vector leg per visible kind instead of one query. This is
  WP-23's implementation work (#220/#221), out of this ADR's own scope - this ADR
  fixes the *shape*, not the code.
- **Git backend unchanged.** This partitioning and index strategy is a
  `storage=postgres`-only concern; the Git backend's own derived index
  (`index/indexer.py`'s current one-index-per-`(model, dimension)` scheme, plain
  `vector` column) is unaffected and stays exactly as it is today - ADR-0007 §5/§7
  already drew this line, this ADR only fills in the Postgres-backend side of it.
- **Index build time is now a planned operational step, not an afterthought.**
  At 1M rows, a `halfvec(1024)` HNSW build took just over an hour on 8 vCPU/4 GB
  `maintenance_work_mem`; linear extrapolation to 5M rows is in the multi-hour
  range (`docs/research/vector-index.md` §1/§2). The migration runbook and any
  future `reindex --full` on the `postgres` backend need to budget for this
  (build after bulk load, raised `maintenance_work_mem`, matching shm sizing -
  already noted in ADR-0007 §5/`docs/research/enterprise.md` §3).
- **`org`'s own HNSW tuning is not fully settled by this spike.** §3's
  recommended parameters for `group`/`project` rest on numbers this spike trusts;
  `org`'s unfiltered recall numbers came from an accidentally near-uniform-random
  synthetic dataset (`docs/research/vector-index.md` §0's caveat) and should be
  re-checked against real content in WP-32 before being treated as final.

## Reversibility

Medium. `halfvec` vs. `vector` is a column-type-and-reindex change, cheap while
`chunks` is still empty (before WP-23 ships) and a full reindex once it is not -
the same cost either option would eventually pay if changed later, so deciding
now costs nothing extra. The B-tree on `namespace` is cheap to add or drop at any
time, independent of everything else in this ADR. Partitioning by kind is the
expensive part to reverse once data exists (`ALTER TABLE ... DETACH PARTITION`
plus a rebuild), which is exactly why it is being decided before WP-23 writes the
first row, not after.
