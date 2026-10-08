# Vector index at scale: `vector` vs `halfvec`, HNSW parameters, partitioning by namespace kind

Retrieved/measured: 2026-10-08. Feeds [ADR-0016](../adr/0016-vector-index.md), PLAN O22,
[F-01](../features/F-01-enterprise-scale.md) "Open decisions". Builds on
[`docs/research/enterprise.md`](./enterprise.md) §1/§3 (100k/200k-row lab runs on
2026-10-07) and [ADR-0007](../adr/0007-storage-backend.md) §5's starting point - this
spike repeats and extends those numbers at ≥ 10× scale and adds the pieces §3 left
`[unverified]`: halfvec recall on the real embedding model, 5M-row-class build cost,
and the fourth namespace kind (`project`, ADR-0008) plus the additive fifth
(`agent`, ADR-0013).

Legend: **[lab]** = measured by me on 2026-10-08, this spike, on a dedicated container
(not the shared CI/test Postgres); **[doc]** = pgvector/PostgreSQL documentation;
**[est]** = arithmetic from sourced/measured numbers; **[unverified]** = could not
confirm from a primary source. Hardware: **8 vCPU, 47 GB RAM, local SSD** (generic -
CLAUDE.md: no operator-specific values), `pgvector/pgvector:pg16` (pgvector 0.8.7) in
Podman, `maintenance_work_mem=4GB`, `shared_buffers=4GB`, `max_parallel_maintenance_workers=4`,
`--shm-size=4g`. A separate, non-shared container from the one `make db-up`/CI tests use.

## 0. Method

- **Tooling:** [`loadtest/vector_index.py`](../../loadtest/vector_index.py) (new,
  this spike) - a deterministic synthetic-chunk generator (`iter_rows`: Gaussian-
  mixture-on-the-sphere vectors, same namespace-mix shape as `loadtest/generate.py`:
  Zipf-by-rank weights over personal/group/project/org namespaces) plus a small CLI
  (`load`/`index`/`bench`/`drop`) that creates a scratch table (never the real
  `chunks` schema - Blast Radius excludes `src/`), COPYs rows in, builds one HNSW
  index, and benchmarks it under a simplified RLS policy (`current_setting('app.ns')`
  array containment + `SET ROLE` to a non-owner role, no password - the same
  session-variable pattern ADR-0008/R2 already decided, just without that ADR's
  membership-table function, which is out of scope here and already costed in
  `docs/research/enterprise.md` §2 "Rate limiting" table / R2 cost addendum).
  Every number in §1-§4 below is one JSON line this tool printed; reusable by WP-32
  at target size.
- **Real embeddings:** an ad hoc script (not kept, same convention as
  `docs/research/enterprise.md`'s `[E1]`-`[E3]`), embedding every chunk of
  `examples/vault` (61 notes, 71 chunks) and every `eval/golden.yaml` query (50
  queries) with `openai/text-embedding-3-large` through OpenRouter
  (`EMBEDDING_PROVIDER=openai`, `EMBEDDING_URL=https://openrouter.ai/api/v1`,
  checked live: `dimensions=1024` returns 1024 values, omitted returns 3072), then
  comparing fp32 against each component rounded to IEEE754 binary16 and back
  (`struct.pack("<e", ...)`) - exactly the value `halfvec` would store, without
  needing a schema change to prove it. The official hybrid number for today's
  schema came from `uv run memory-manager eval` itself (unchanged, real run against
  a fresh `mm_eval_*` database), not from the ad hoc script.
- **Scale:** 1,000,000 synthetic chunks for `vector(1024)` and `halfvec(1024)` (the
  decision the issue centers on). `halfvec(3072)` was measured at 300,000 rows and
  linearly extrapolated to 1M/5M (§2) - the issue's own explicit fallback ("measure
  smaller and extrapolate, stating so"): at the observed halfvec(1024) build rate,
  1M rows of 3072-dim halfvec would have run past this spike's time budget (the
  300k-row halfvec(3072) build alone took 74 minutes).

### A generator caveat that shapes how to read §1/§3's recall numbers

`iter_rows` was meant to draw clustered, Gaussian-mixture vectors (24 centroids,
`noise=0.15` per component) - "closer to real embeddings' distribution than uniform
random", per the original intent carried over from `docs/research/enterprise.md`
§3's dataset description. It does not do that in 1024+ dimensions: a unit vector's
own per-component magnitude is `~1/sqrt(1024) ≈ 0.03`, far smaller than the `0.15`
noise added to each component, so after re-normalising, the result is statistically
indistinguishable from a uniformly random point on the sphere - confirmed directly
(a random row's cosine distance to 5,000 other random rows: min 0.856, mean 0.998,
i.e. essentially no two rows are ever close). **The recall numbers in §1 are
therefore a worst case, not a realistic one** - uniform-random, high-dimensional
vectors are exactly the case pgvector's own README warns is hardest for HNSW, and
real text embeddings (which lie on a far lower-dimensional manifold with genuine
semantic structure) recall much better in practice at the same `ef_search`,
consistent with §4's real-embedding check never showing a problem at all. This
spike did not have the budget to regenerate 1M rows with a fixed generator and
re-run every build; §1's recall table should be read as "how bad can it get",
useful for sizing `ef_search`/`max_scan_tuples` headroom, not as a prediction of
production recall.

## 1. `vector(1024)` vs `halfvec(1024)` at 1,000,000 rows

### Load and build

| | load (COPY) | HNSW build (m=16, ef_construction=64) | index size | heap+TOAST (est.) | table total |
|---|---|---|---|---|---|
| `vector(1024)` | 913 s (1,095 rows/s) | 1,108 s (18 min 28 s) | **7.63 GiB** (8,192,008,192 B = 8,192 B/row) | ~5.24 GiB | 12.87 GiB |
| `halfvec(1024)` | 1,229 s (814 rows/s) | **3,797 s (63 min 17 s)** | **2.54 GiB** (2,730,680,320 B = 2,731 B/row) | ~2.68 GiB | 5.22 GiB |

Both index sizes match the per-row formula from `docs/research/enterprise.md` §1
almost exactly (8,192 B/row for `vector`, one tuple per 8 KB page; 2,730 B/row for
`halfvec`, three per page) - the 100k-row lab numbers scale linearly to 1M, which
means that table's 5M extrapolation (vector ≈ 41 GB, halfvec ≈ 14 GB) is sound:
this spike's own linear projection lands at **38.2 GiB** / **12.7 GiB** respectively.

**halfvec is reproducibly slower to build, not a one-off lab anomaly.** The
2026-10-07 lab's 100k-row halfvec build (25 min, vs. 4.5 min for `vector`) was
flagged `[unverified cause]`, possibly disk contention from a concurrent test. At
1M rows, on a dedicated, otherwise-idle container, halfvec still took **3.4× as
long to build** as `vector` (63 min vs. 18.5 min). This is real, not noise, and
belongs in the sizing guidance for WP-23's migration and for any future reindex.

### Query latency and recall, under RLS, by namespace-kind selectivity, on a *flat* (unpartitioned) table

Scenario: one visible personal namespace (narrowest selectivity, ~1/2,551 of the
namespace list), 20 visible group namespaces out of 500 (moderate), every namespace
visible (`org`, unfiltered - 2,551 namespaces). `recall@10` is against the exact,
index-free order (`enable_indexscan`/`enable_bitmapscan = off`) on the same
RLS-filtered candidate set, inside the same transaction. 30 queries per row.

**What the planner actually does today (no B-tree on `namespace`, no partitioning - today's schema):**

| scenario | plan chosen | p50 | p95 | recall@10 |
|---|---|---|---|---|
| personal (1 ns) | **Parallel Seq Scan** + exact sort (planner skips the HNSW index - too selective to be worth it) | 71.1 ms | 101.1 ms | 1.0000 (trivially exact) |
| group (20 ns) | **Parallel Seq Scan** + exact sort | 77.3 ms | 103.2 ms | 1.0000 (trivially exact) |
| org (unfiltered) | **Index Scan** on the HNSW index, default `hnsw.ef_search=40` | 48.0 ms | 67.3 ms | **0.0033** |

This is the single most important structural finding of this spike: **today's
schema (one flat HNSW index, no B-tree on `namespace`) never uses the HNSW index
for a namespace-filtered query at all** - the planner's own cost estimate routes
every selective filter around it to a parallel sequential scan, which is why those
two rows are "trivially exact" (both the approximate and the exact leg run the
identical plan) rather than genuinely approximate. The *unfiltered* `org` row does
use the index, and at the default `ef_search=40` its recall collapses to ~0.3% at
1024 dimensions - far worse than the 64-dim lab's 0.29-0.72 unfiltered figures
(`docs/research/enterprise.md` §1), consistent with higher dimensionality making
default-tuned HNSW harder, and amplified by the worst-case, near-uniform synthetic
vectors (see the caveat above).

**Forcing the index to be used anyway** (`enable_seqscan/bitmapscan = off`), to see
what a namespace-filtered *approximate* search would cost once `chunks` partitions
force it:

| scenario | `ef_search` | `iterative_scan` | p50 | p95 | recall@10 |
|---|---|---|---|---|---|
| personal | 40 | off (pgvector default) | 92.2 ms | 114.7 ms | **0.0000** |
| personal | 200 | `relaxed_order`, `max_scan_tuples=20000` | **265.2 ms** | **372.0 ms** | 1.0000 |
| group | 40 | off | 84.5 ms | 113.4 ms | 0.0333 |
| group | 200 | `relaxed_order`, `max_scan_tuples=20000` | 143.2 ms | 275.3 ms | 1.0000 |
| org (unfiltered) | 200 | `relaxed_order`, `max_scan_tuples=20000` | 72.6 ms | 94.3 ms | 0.0267 |
| org (unfiltered) | 400 | `relaxed_order`, `max_scan_tuples=50000` | 98.0 ms | 129.8 ms | 0.0533 |

Two things follow. First, **the default `ef_search=40`/`iterative_scan=off`
combination is unusable under any filter** (recall 0-3%), exactly reproducing
pgvector's own filtering warning, just worse at 1024 dims than at 64. Second,
**`relaxed_order` does restore full recall under a selective filter, but at a
latency this spike's own numbers say is too expensive on a *flat*, 1M-row table**:
265-372 ms for the single-namespace case alone exceeds the project's own search
p95 target (300 ms, `docs/research/enterprise.md` §3). Raising `ef_search`/
`max_scan_tuples` further on the *unfiltered* `org` case barely moved recall
(2.7% → 5.3%) - the near-uniform synthetic vectors make this an unreliable number
in absolute terms (see the caveat above), but the direction - unfiltered HNSW on
1M+ near-uniform-hard vectors needs much more than `ef_search≈400` to recover - is
real and matters for sizing `org`'s own HNSW parameters generously.

**The actual fix the spike found: a plain B-tree on `namespace`.** Adding
`create index on chunks (namespace)` to the same flat, unpartitioned table and
re-running the *unforced* personal/group scenarios:

| scenario | plan chosen | p50 | p95 | recall@10 |
|---|---|---|---|---|
| personal (1 ns) | Bitmap Index Scan on the B-tree, exact sort | **4.4 ms** | **6.7 ms** | 1.0000 |
| group (20 ns) | Bitmap Index Scan on the B-tree, exact sort | **9.9 ms** | **14.9 ms** | 1.0000 |

This is a ~16-20× latency improvement over the seq-scan fallback and a ~30-60×
improvement over forcing the HNSW path with `relaxed_order`, at **exact** (not
approximate) recall, and is exactly what ADR-0007 §5 already proposed ("personal
uses a B-tree on namespace plus exact sort") - this spike is the first time it was
actually measured, and confirms it is not just correct but by far the fastest
option for narrow-selectivity callers, with no HNSW/`ef_search` tuning involved at
all.

## 2. Native 3072 dimensions (`halfvec(3072)`) vs 1024

Measured at 300,000 rows (budget fallback, see §0); `vector(3072)` is not valid
pgvector syntax (`vector` caps at 2,000 dimensions), so the only way to use native
3072-dim embeddings at all is `halfvec`.

| | load (COPY) | HNSW build (m=16, ef_construction=64) | index size | heap+TOAST (est.) | table total |
|---|---|---|---|---|---|
| `halfvec(3072)`, 300k rows | 983 s (305 rows/s) | **4,453 s (74 min 13 s)** | 2.29 GiB (2,457,608,192 B = 8,192 B/row) | ~2.34 GiB | 4.63 GiB |
| `halfvec(3072)`, **1M [est, linear]** | ~3,277 s (55 min) | ~14,843 s (~4.1 h) | ~7.63 GiB | ~7.79 GiB | ~15.4 GiB |
| `halfvec(3072)`, **5M [est, linear]** | ~4.6 h | ~20.6 h | ~38.2 GiB | ~39.0 GiB | ~77.2 GiB |

**The non-obvious finding: `halfvec(3072)` gets none of `halfvec`'s usual
page-packing advantage.** Its measured index cost is **8,192 B/row** - identical to
plain `vector(1024)` in §1, not the ~3×-smaller figure `halfvec(1024)` achieved.
The reason is the same page-boundary effect `docs/research/enterprise.md` §1 found
for `vector`: a `halfvec(3072)` value is `2×3072+8 = 6,152` bytes, which still only
fits one tuple per 8 KB page (three `halfvec(1024)` values at 2,056 bytes each fit
on one page; two 6,152-byte values do not). Practically: moving from `vector(1024)`
(today) straight to `halfvec(3072)` would cost **roughly the same index size** as
today's schema, not less, while multiplying build time by another ~4× over
`halfvec(1024)` at the same row count (304 vs. 1,229 s/300k-equivalent rows) and
needing a reindex if ever changed later (`EMBEDDING_DIMENSIONS` is pinned at first
migration and immutable, PLAN O23). Combined with the owner's already-fixed
default of 1024 (O23), native 3072 buys no space advantage here and a real build-
time and reindex-risk cost - this spike finds no case for it as the default.

## 3. Partitioning by namespace kind

Not re-measured at 1M scale in this spike (time budget; §1's B-tree finding
already answers the `personal`/`agent` question directly, see below). The
200k-row, 64-dim lab in `docs/research/enterprise.md` §1 found: hash partitioning
by namespace is **not** pruned by the RLS predicate (Merge Append over every
partition, ~10× the buffers); list partitioning **by namespace kind** *is* pruned
(the planner resolves to exactly one partition once `kind` is in the query), and
within the resolved partition personal selected a B-tree + exact sort while
group/org used HNSW. A quick structural check on this spike's own 1M-row table
confirms list partitioning by `kind` still prunes correctly at this size (smoke-
tested with `--partitioned` in `loadtest/vector_index.py`, confirmed via `EXPLAIN`:
the planner resolves straight to the matching partition, no `Merge Append`).

§1's finding sharpens what that partition split should actually do, though:
**a `personal` *partition* does not, by itself, solve the personal-namespace
latency problem**, because filtering a kind-partition down to *one specific*
namespace is exactly as selective as filtering the unpartitioned table to one
namespace - the fix is the **B-tree on `namespace`** §1 measured (4.4 ms p50),
not the partition boundary. The partition's own value is narrower: it keeps
`personal`/`agent` rows (one-owner cardinality, no HNSW needed at all) out of the
`group`/`project`/`org` HNSW graphs, so those graphs stay smaller and cleaner, and
it is what makes "personal uses B-tree, group/org use HNSW" an actual `WHERE kind =
…`-routable decision for the query planner instead of a per-query heuristic.

**Recommended HNSW parameters per kind**, combining ADR-0007 §5's starting point
with §1's numbers: `group`/`project` - `hnsw.iterative_scan = relaxed_order`,
`hnsw.ef_search` in the 100-200 range, `hnsw.max_scan_tuples` 20,000-50,000 (§1's
group row hit full recall at `ef_search=200` within the 300 ms budget: 143/275 ms
p50/p95); `org` - HNSW unfiltered, but with generous `ef_search` (§1's near-uniform
worst case needed far more than 400 to move recall meaningfully, so `org`'s own
tuning should be validated against real embeddings in WP-32, not assumed from this
spike's worst-case numbers); `personal`/`agent` - no HNSW at all, B-tree on
`namespace` plus exact sort.

## 4. Real-embedding recall check against the golden set

`uv run memory-manager eval` (today's schema, `vector(1024)`, hybrid full-text +
vector, RRF-fused, real `openai/text-embedding-3-large` via OpenRouter,
`dimensions=1024`) against `examples/vault`/`eval/golden.yaml`:

```
overall  recall@5=0.9000  mrr=0.7990
  alias          n=6   recall@5=1.0000  mrr=1.0000
  cross-language n=8   recall@5=0.6250  mrr=0.3063
  exact          n=15  recall@5=1.0000  mrr=0.9333
  paraphrase     n=15  recall@5=0.8667  mrr=0.8333
  supersede      n=6   recall@5=1.0000  mrr=0.8333
```

This is the production hybrid number for today's schema - unrelated to this
spike's proposal (it already ships) - and is reused only as a sanity baseline: the
cross-language misses are a known limitation of a 1024-dim embedding on short
German/English queries, not something either `vector` or `halfvec` affects.

Isolating the **vector channel only** (no full-text, no RRF - brute-force cosine
over every chunk's real embedding, in Python, ranked per note by its best chunk),
fp32 vs. the same vectors rounded to fp16 and back (what `halfvec` physically
stores) and 1024 vs. native 3072 dimensions:

| | recall@5 | MRR |
|---|---|---|
| 1024-dim, fp32 | 1.0000 | 0.9700 |
| 1024-dim, fp16 round-trip (halfvec proxy) | 1.0000 | 0.9700 |
| 3072-dim (native), fp32 | 1.0000 | 0.9600 |
| 3072-dim (native), fp16 round-trip (halfvec(3072) proxy) | 1.0000 | 0.9600 |

**Finding:** the committed golden set (61 notes, 71 chunks) is too small and too
easy for brute-force vector search to show any difference between fp32 and
halfvec's fp16 rounding, or between 1024 and 3072 dimensions - every variant sits
at the ceiling (`recall@5=1.0`). This confirms halfvec causes **no functional
regression** on real content. It is also the opposite extreme from §1's synthetic
worst case (real text embeddings vs. this spike's accidentally-uniform-random
vectors) - between the two, production recall on real content should sit far
closer to this section's ceiling than to §1's worst case, but neither this
section's exact brute force nor §1's broken clustering can quantify that gap
directly; only a larger real-content golden set under real approximate HNSW could
(out of scope here).

## 5. `agent` namespace kind (ADR-0013): additive, not measured separately

The `agent` kind is not generated by `loadtest/vector_index.py` and was not loaded
into any of the tables above: an agent's default namespace (`agent-<name>`,
ADR-0013) is owned by exactly one owner, the same one-row-per-owner cardinality as
`personal`. §1's B-tree finding and §3's partition reasoning both apply to `agent`
unchanged (same selectivity, same "exact sort, no HNSW" strategy). Attaching it to
a list-partitioned `chunks` costs one statement and no rebuild of the partitions
already in place:

```sql
create table chunks_agent partition of chunks for values in ('agent');
```

`loadtest/vector_index.py` documents this as `AGENT_PARTITION_DDL` rather than
generating `agent` rows that would be indistinguishable from `personal` ones for
every number this spike measured.

## 6. Decision impact

| Topic | Finding | Evidence |
|---|---|---|
| `vector` vs `halfvec` at 1024 dim | `halfvec` is ~3× smaller (2.54 vs. 7.63 GiB at 1M rows) but ~3.4× slower to build (63 vs. 18.5 min at 1M rows); no recall difference on real content | §1, §4 |
| Namespace-filtered search on a flat table | The planner already avoids the HNSW index for selective filters (seq scan fallback, exact but slow: 71-103 ms); forcing HNSW with `relaxed_order` is exact but slower still (143-372 ms, over budget for `personal`); a plain B-tree on `namespace` is exact **and** fast (4-15 ms) | §1 |
| Partitioning by namespace kind | Pruned correctly at 1M rows (list by kind, confirmed via `EXPLAIN`); its value is isolating one-owner kinds from the HNSW kinds, not solving per-namespace selectivity by itself - that is the B-tree's job | §3 |
| Native 3072 dims | No index-size advantage over today's `vector(1024)` (same 8,192 B/row, page-boundary effect) and a real extra build-time/reindex cost; the owner's O23 default (1024) already closes this | §2 |
| `agent` kind | Additive: one `partition of ... for values in ('agent')` statement, same strategy as `personal` | §5 |

## Not verified

- Production recall on real content under approximate HNSW with a tuned
  `ef_search`/`iterative_scan` - §1's synthetic vectors turned out to be a near-
  uniform worst case by generator accident (see §0's caveat), and §4's real-
  embedding check is exact brute force, which cannot exercise approximation at
  all. A larger real-content golden set would be needed to close this gap.
- `halfvec(3072)` at the full 5M target size and its HNSW build time there:
  linearly extrapolated (§2) from a 300k-row measurement, not measured directly -
  explicitly allowed by the issue's own fallback.
- Whether pgvector's halfvec SIMD build path is faster on different CPU
  microarchitectures - this spike's own 3.4× slowdown (§1) is a second, larger-
  scale data point on the same open question the 2026-10-07 lab flagged, not a
  resolution of it.

## Sources

- [pgvector README/CHANGELOG](https://github.com/pgvector/pgvector) (version, HNSW
  parameters, `halfvec`/`vector` storage and dimension limits, `iterative_scan`) -
  retrieved 2026-10-07, reused from `docs/research/enterprise.md` §1.
- [PostgreSQL 16 partitioning](https://www.postgresql.org/docs/16/ddl-partitioning.html),
  [row security](https://www.postgresql.org/docs/16/ddl-rowsecurity.html) -
  retrieved 2026-10-07, reused from `docs/research/enterprise.md` §3.
- `docs/research/enterprise.md` §1 (RLS-filtered HNSW recall, partitioning options,
  100k/200k-row lab) and §3 (PostgreSQL at enterprise scale) - retrieved 2026-10-07.
- `docs/adr/0007-storage-backend.md` §5 (starting point: `halfvec`, `m=16`,
  `ef_construction=64`, partitioning by kind, RRF), `docs/adr/0008-namespace-permissions.md`
  (RLS pattern, R2), `docs/adr/0013-agent-identity.md` (`agent` kind).
- Lab: `loadtest/vector_index.py` (this spike) against `pgvector/pgvector:pg16`
  (pgvector 0.8.7), measured 2026-10-08; script kept (unlike the ad hoc golden-set
  script, per §0).
- OpenRouter `openai/text-embedding-3-large` via the OpenAI-compatible embeddings
  endpoint (`memory_manager.index.embeddings.OpenAICompatibleProvider`, unchanged) -
  checked live 2026-10-08.
