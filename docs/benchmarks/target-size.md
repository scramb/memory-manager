# Target-size load test (1M notes / 5M chunks) — targets not met, cause measured

Measured: 2026-10-09/10 · Feeds: [#271](https://github.com/scramb/memory-manager/issues/271), [F-01's Done when](../features/F-01-enterprise-scale.md) · Work package: WP-32 · Relates to: #296 (commit `78d9c8d`), [ADR-0007](../adr/0007-storage-backend.md), [ADR-0008](../adr/0008-namespace-permissions.md), [ADR-0009](../adr/0009-stateless-replicas.md), [ADR-0016](../adr/0016-vector-index.md)

**Status: done — targets not met.** Owner decision 2026-10-10: this report
documents the outcome honestly rather than stopping on a missed target. The
generate+load phase completes reliably at the full 1,000,000-note /
~5,000,000-chunk target size (the loader OOM from the first attempt is
fixed, WP-32/#271's own earlier round). Both `postgres` and `valkey` shared-
state runs reached the k6 phase and produced real numbers, each run once
plus one retry. **#296** (`search.py`: the `user`-kind vector leg now
filters `c.namespace` on `mm_readable_ns()` when the caller gives no
explicit namespace filter) measurably improved several numbers — but
**search p95, read p95 and write p95 are still missed, by one to two orders
of magnitude**, with a specific, measured cause (below). **Not a sizing
commitment** — see "Owner-accepted deviation" below.

## Owner-accepted deviation (PLAN O24, record-don't-ask)

This run uses a local kind cluster on the implementer's own node via
`scripts/loadtest-cluster.sh`, without `KUBE_CONTEXT` — not an operator
cluster. The numbers in this file are measured on a single-node kind
cluster, not an operator cluster; this report is not a sizing commitment.
Recorded the same way in
[`docs/features/F-01-enterprise-scale.md`](../features/F-01-enterprise-scale.md)
("Done when") and [`docs/PLAN.md`](../PLAN.md) (O24 note).

## Hardware and software profile

- 8 vCPU, 47 GiB RAM, one kind node (`kindest/node:v1.33.4`) on rootful
  podman on a single Linux host; virtual disk, ~125 GiB free at the start of
  each run. Not isolated bare metal, not a multi-node cluster.
- **CNPG instances: 1, not 3** (deviation from the issue's "3 replicas"
  wording for the *API* tier — the CNPG `Cluster` is a separate knob,
  `database.cnpg.instances`, pre-approved to fall back to 1 if 3 do not fit
  this node's own disk budget; `halfvec(1024)` heap+TOAST alone scales to
  ~13.4 GiB at 5M chunks, `docs/research/vector-index.md` §1). The
  forced-kill component in `LOADTEST_KILL_AFTER` is an **API** Pod, never
  the CNPG instance.
- CNPG resources raised via `scripts/loadtest-cluster.sh`'s own
  `LOADTEST_HELM_SET` (existing chart values, no chart schema change):
  `database.cnpg.storage.size=40Gi`, `database.cnpg.resources.requests.cpu=2`,
  `database.cnpg.resources.requests.memory=3Gi`,
  `database.cnpg.resources.limits.memory=6Gi` (chart default: 100m CPU /
  256Mi request, 512Mi limit). **`shared_buffers` stayed at CNPG's own
  default (128 MB)** — the chart renders no `postgresql.parameters`
  override; see "The measured cause" for why this plausibly compounds the
  result below.
- `loadtest.load`'s own bulk-load/index-build session settings
  (`loadtest/k8s/generate-load-job.yaml`): `--maintenance-work-mem 2GB
  --max-parallel-maintenance-workers 4`; `--chunk-workers` at its own
  default (one process per core, 8).
- **The generate+load Job carries a memory request/limit**
  (`requests.cpu 2`, `requests.memory 4Gi`, `limits.memory 18Gi`, sized from
  this task's own earlier measurement: `memory.current` plateaus at
  ~18.0 GiB and stays flat, `anon` a comfortable ~8.6 GiB of that) — a
  regression here now OOMs the Pod, not the host.
- `database.cnpg.imageName` default:
  `ghcr.io/cloudnative-pg/postgresql:16-standard-trixie` → PostgreSQL 16.15,
  pgvector 0.8.6, CNPG operator 1.26.1.
- 3 `api` replicas, 2 `worker` replicas (chart defaults, no HPA/KEDA during
  the measured run), `embedding.provider=openai` pointed at the in-cluster
  embedding stub. `track_functions = 'all'` via `alter database
  memory_manager set track_functions = 'all'` plus a rollout restart of
  `api`/`worker` (CNPG's own instance manager owns `postgresql.auto.conf`,
  so a direct `alter system` fails `Permission denied`).
- **Image**: built from `wp/32-load-target` at commit `78d9c8d` (#296
  committed) for every run in this file's own "After #296" section, below.

## Method

Two generations of runs, same loaded dataset throughout (`loadtest.generate
--notes 1000000 --users 200 --groups 20 --seed 1`, loaded via `loadtest.load
--with-chunks --use-existing-database`, `LOADTEST_SKIP_GENERATE_LOAD=1`
reusing it across every run after the first):

- **Before #296** (WP-32/#271's own earlier round, same session): one
  `postgres` run, reached the k6 phase, numbers kept below for comparison.
  No `valkey` run exists for this generation — the dataset was lost to an
  operational mistake before one happened (recorded at the time; not
  repeated, since #296 changes the comparison anyway).
- **After #296** (this round): `postgres` run, then `postgres` retry, then
  `valkey` run (`LOADTEST_SKIP_GENERATE_LOAD=1`, same dataset), then
  `valkey` retry — one retry per shared-state value, both used since the
  first attempt of each showed numbers worth confirming were not a one-off
  (they were: each pair is consistent, see Results).

```sh
# Run 1 (postgres, bootstraps its own kind cluster, loads the vault):
LOADTEST_NOTES=1000000 LOADTEST_SHARED_STATE=postgres LOADTEST_KILL_AFTER=30 \
  LOADTEST_PVC_SIZE=25Gi STEP_TIMEOUT_SECONDS=10800 \
  LOADTEST_HELM_SET="database.cnpg.instances=1 database.cnpg.storage.size=40Gi database.cnpg.resources.requests.cpu=2 database.cnpg.resources.requests.memory=3Gi database.cnpg.resources.limits.memory=6Gi" \
  LOADTEST_RESULTS_DIR=./results/postgres KEEP_CLUSTER=1 scripts/loadtest-cluster.sh

# Runs 2-4 (postgres retry, valkey, valkey retry): existing-cluster path,
# KEEP_RELEASE=1 on every run but the last (docs/benchmarks/cluster-loadtest.md's
# own "Reusing a loaded dataset" section has the full sequence).
LOADTEST_NOTES=1000000 LOADTEST_SHARED_STATE=<postgres|valkey> LOADTEST_KILL_AFTER=30 \
  LOADTEST_SKIP_GENERATE_LOAD=1 LOADTEST_REGISTRY_INSECURE=1 \
  KUBE_CONTEXT=kind-mm-loadtest LOADTEST_REGISTRY=localhost:5002 \
  LOADTEST_HELM_SET="database.cnpg.instances=1 database.cnpg.storage.size=40Gi database.cnpg.resources.requests.cpu=2 database.cnpg.resources.requests.memory=3Gi database.cnpg.resources.limits.memory=6Gi" \
  LOADTEST_RESULTS_DIR=./results/<run> [KEEP_RELEASE=1] scripts/loadtest-cluster.sh
```

## Results

### Dataset facts (one load, reused by every run below)

- **1,000,000 / 1,000,000 notes.** Generate+load total: 118-120 minutes
  (generate ~18-20 min, matching `docs/benchmarks/baseline.md`'s 100k-note
  generate time ×10; the rest is chunk-building/`COPY`/index build).
- **5,008,017 chunks** — `chunks_user` 4,887,846, `chunks_group` 79,748,
  `chunks_project` 36,871, `chunks_org` 3,552. Load throughput ~1,170-1,180
  rows/s.
- **HNSW index build: ~419 s (7 min) for all three multi-owner
  indexes** — `chunks_group_embedding_hnsw_idx` ~265 s / 217,776,128 bytes
  (208 MiB), `chunks_project_embedding_hnsw_idx` ~117 s / 100,696,064 bytes
  (96 MiB), `chunks_org_embedding_hnsw_idx` ~37 s / 9,707,520 bytes
  (9.3 MiB). `chunks_user` (personal, ADR-0016) has no HNSW index — B-tree
  on `namespace` plus exact sort, by design.
- **Database size: 33 GB.**
- Loader Pod memory: plateaus at ~18.0 GiB `memory.current` (`anon`
  ~8.6 GiB), `0` restarts, `Completed` every time this dataset was built.

### Before #296 (`postgres`, one run, kept for comparison)

| Tool/scenario | p50 (med) | p95 | p99 | k6 threshold |
|---|---|---|---|---|
| `search` | 50,971 ms | 60,003 ms | 60,008 ms | p95<300 — crossed, ~200× |
| `search_vector_only` | 55,857 ms | 60,012 ms | 60,015 ms | p95<300 — crossed, ~200× |
| `read` | 12,527 ms | 49,446 ms | 58,525 ms | p95<100 — crossed, ~494× |
| `write` | 28,962 ms | 60,008 ms | 60,018 ms | p95<200 — crossed, ~300× |

`http_req_failed{phase:measure}`: 17.3 %. `cosine_distance()`: **11,145,446
calls** for 180 HTTP requests. Cause identified then: a vector query with no
explicit namespace filter against the 4,887,846-row `chunks_user` partition
(no HNSW index there) fell back to an exact distance-and-sort pass over the
*whole* partition before RLS narrowed the output — `search.py`'s `vector_search`
had no selective predicate to push down. #296 fixes exactly this.

### After #296

#### Per-tool latency (k6 `http_req_duration`, ms) — both runs per shared state

| Tool/scenario | Run | p50 (med) | p95 | p99 | k6 threshold |
|---|---|---|---|---|---|
| `search` | postgres | 23,077 | 51,370 | 54,411 | p95<300 — crossed, ~171× |
| `search` | postgres retry | 23,153 | 43,043 | 49,423 | p95<300 — crossed, ~143× |
| `search` | valkey | 29,599 | 56,424 | 57,280 | p95<300 — crossed, ~188× |
| `search` | valkey retry | 24,349 | 52,068 | 58,099 | p95<300 — crossed, ~174× |
| `search_vector_only` | postgres | 25,614 | 41,063 | 46,157 | p95<300 — crossed, ~137× |
| `search_vector_only` | postgres retry | 34,589 | 45,391 | 47,338 | p95<300 — crossed, ~151× |
| `search_vector_only` | valkey | 41,317 | 55,984 | 56,655 | p95<300 — crossed, ~187× |
| `search_vector_only` | valkey retry | 30,365 | 56,298 | 58,397 | p95<300 — crossed, ~188× |
| `read` | postgres | 1,055 | 23,619 | 24,945 | p95<100 — crossed, ~236× |
| `read` | postgres retry | 4,182 | 16,124 | 17,318 | p95<100 — crossed, ~161× |
| `read` | valkey | 13,931 | 36,165 | 52,110 | p95<100 — crossed, ~362× |
| `read` | valkey retry | 2,884 | 36,796 | 38,149 | p95<100 — crossed, ~368× |
| `write` | postgres | 530 | 35,515 | 41,223 | p95<200 — crossed, ~178× |
| `write` | postgres retry | 5,025 | 19,080 | 19,539 | p95<200 — crossed, ~95× |
| `write` | valkey | 20,177 | 49,188 | 51,871 | p95<200 — crossed, ~246× |
| `write` | valkey retry | 13,004 | 54,862 | 58,546 | p95<200 — crossed, ~274× |

`http_req_failed{phase:measure}`: postgres 5.8 %, postgres retry 6.3 %,
valkey 1.9 %, valkey retry 6.5 % — all above the <1 % budget except the one
valkey run, itself inconsistent with its own retry (1.9 % vs 6.5 %), both
small-sample (~100-160 requests/run). `checks{phase:measure}`: 94.3-98.3 %
across all four, against >99 %.

**#296's own measured effect** (comparing the single before-#296 run to the
after-#296 runs' own medians): `read` p50 roughly 12,500 ms → ~1,000-14,000 ms
(mixed - see Limitations), `write` p50 ~29,000 ms → ~500-20,000 ms,
`checks{phase:measure}` 84.9 % → 94.3-98.3 %, `http_req_failed{phase:measure}`
17.3 % → 1.9-6.5 %. Real, substantial improvement - not enough to meet any
of the three p95 targets.

#### Recall@5 (vector-only correctness, ADR-0016 `ef_search`/`iterative_scan` settings)

| Run | Lexical hit rate | Vector hit rate (recall@5) | Samples (lex/vec) |
|---|---|---|---|
| postgres | 75.0 % (9/12) | 92.3 % (12/13) | 12 / 13 |
| postgres retry | 75.0 % (9/12) | 72.7 % (8/11) | 12 / 11 |
| valkey | 75.0 % (9/12) | 76.9 % (10/13) | 12 / 13 |
| valkey retry | 75.0 % (9/12) | 84.6 % (11/13) | 12 / 13 |
| **pooled per shared state** | **postgres: 75.0 % (18/24)** | **postgres: 83.3 % (20/24)** | 24 / 24 |
| | **valkey: 75.0 % (18/24)** | **valkey: 80.8 % (21/26)** | 24 / 26 |

**No run reaches the `count>=50` minimum sample size** — the correctness
scenario is itself latency-starved (each sequential call can take tens of
seconds, so only 11-13 of the intended 50 complete within its own 5-minute
budget). Even pooled across both runs per shared state, the sample stays
well under 50. The vector (recall@5) hit rate is consistently in the
72.7-92.3 % range across four independent samples — plausibly close to or
at the `>=0.9` bar, but this report cannot say so with the statistical
confidence `count>=50` was designed to give.

#### RLS/namespace-resolution and vector function self-time (`pg_stat_user_functions`, `track_functions=all`, reset via `pg_stat_reset()` before each measured phase)

| Function | postgres | postgres retry | valkey | valkey retry |
|---|---|---|---|---|
| `cosine_distance()` calls | 11,145,446 | 12,441,101 | 11,097,717 | 10,922,340 |
| `cosine_distance()` mean self-time/call | 0.131 ms | 0.125 ms | 0.133 ms | 0.123 ms |
| `mm_readable_ns()` mean self-time/call | 4.24 ms | 4.69 ms | 4.10 ms | 3.96 ms |
| `mm_frequent_lexemes()` mean self-time/call | 67.7 ms | 70.0 ms | 69.4 ms | 65.9 ms |

`cosine_distance()` call counts are **within noise of the before-#296
number** (11.1-12.4M vs. 11.1M before) — #296 did not reduce the *total*
volume of distance computations; it changed *which* queries pay for it (no
more catastrophic, unbounded full-partition scans for the specific
no-filter `chunks_user` case), while the three HNSW-indexed partitions'
own bounded-but-real per-leg scan cost (below) remains the dominant,
unchanged cost driver.

### Behaviour around the kill

Every run's forced `api` Pod delete (`--grace-period=0`, 40 s into the k6
run: warmup 10 s + `LOADTEST_KILL_AFTER=30`, the middle of the 60 s measured
phase) is visible in each run's own `timings.json` (`forced_pod_deletion`).
As before #296, the *other two*, never-killed `api` Pods also show
readiness-probe failures shortly after each kill — this run's own
already-severe per-request latency (above) means losing a third of API
capacity compounds an existing problem rather than being cleanly
separable from it; none of these four runs isolate "replica-loss-only"
degradation from the baseline latency problem.

### The measured cause

#296 removed the *unbounded* cost (a no-filter query against `chunks_user`
scanning the whole, now-4.89M-row partition). What remains, per the
`pg_stat_user_functions` figures above, is the **sum of four bounded, but
individually substantial, per-request vector-search legs** (`search.py`'s
`_vector_search_legs`, one query per `_VECTOR_KINDS` entry):

- `user` (now filtered to `mm_readable_ns()`): an exact distance-and-sort
  pass over the caller's *own* personal namespace - averaging
  4,887,846 ÷ 200 personal namespaces ≈ **24,400 rows** per namespace.
- `group`/`project` (HNSW, `iterative_scan=relaxed_order`,
  `max_scan_tuples=20000`): up to 20,000 candidate distance evaluations
  each - close to or exceeding each partition's own actual size
  (79,748 / 36,871 rows), so this is close to a near-full-partition scan in
  practice at this partition size, not a small approximate probe.
  `mm_frequent_lexemes()`'s own ~66-70 ms mean self-time (not changed by
  #296) runs on the lexical side of the same hybrid-search call, adding to
  the same per-request budget.
- `org` (HNSW, `ef_search=400`, unfiltered): up to 20,000 candidates against
  a 3,552-row partition - effectively exhaustive every time.

Summed, a single hybrid-search call's own vector legs plausibly cost on the
order of **~65,000-70,000 `cosine_distance` evaluations** even in the
*filtered*, best case (24,400 + 20,000 + 20,000 + 3,552) - at the measured
~0.12-0.13 ms mean self-time per call, that is **~8-9 seconds of raw
`cosine_distance` compute alone**, before RLS, planning, network or any
other per-request cost. Under this hardware profile's `database.cnpg`
allocation (2 vCPU request, no CPU limit, but a single CNPG instance
competing with itself across concurrent requests) and `shared_buffers` left
at CNPG's own 128 MB default against a 33 GB database (so a meaningful
share of each partition's own pages very likely are not cached), that
per-request cost queues under the k6 run's own concurrency (`search` 5/s,
`search_vector_only` 2/s, `read` 3/s, `write` 0.5/s plus the correctness
scenario) into the tens-of-seconds p95s measured above. This is a direct,
scale-dependent consequence of ADR-0016's own `max_scan_tuples=20000`
starting parameter (`docs/research/vector-index.md` §3, measured against a
smaller `group`/`project` partition than this run's own 79,748/36,871 rows)
meeting this run's own CPU/memory allocation - not a bug introduced by this
task, and not something this task's own "no code tuning" boundary permits
fixing here.

## F-01 "Done when" status

**Not met.** See
[`docs/features/F-01-enterprise-scale.md`](../features/F-01-enterprise-scale.md)
for the updated line. search/read/write p95 are all missed by one to two
orders of magnitude on this node's own hardware profile, with the cause
above measured, not guessed. F-01 closes with this gap documented (owner
decision 2026-10-10); reaching the targets is tracked in #297 (ADR-0016
`max_scan_tuples`/`shared_buffers`/CNPG sizing).

## Limitations

- **Small per-run sample sizes** (~100-240 HTTP requests per run) - the
  before/after #296 comparison and the four after-#296 runs both show real
  variance run-to-run (e.g. `write` p95 19.1-54.9 s across the four after-
  #296 runs) on top of the consistent, order-of-magnitude miss; no run here
  reaches a sample size that would resolve that variance precisely.
- **Recall@5 sample-starved** (11-13 of the intended 50 per run, pooled
  24-26) - see "Recall@5", above.
- **Kill behaviour not isolated from the baseline latency problem** - see
  "Behaviour around the kill".
- **`shared_buffers` and CNPG CPU/memory sizing untuned** - plausibly
  compounds "The measured cause" above; not changed here (chart exposes no
  `postgresql.parameters` override; CNPG CPU/memory sizing is this task's
  own `LOADTEST_HELM_SET`, already raised once, not raised further without
  a measured reason to pick a new number).
- **No run beyond one retry per shared-state value** - the owner's 2026-10-10
  decision was to document the outcome rather than keep re-running; a wider
  sweep of `LOADTEST_HELM_SET` CNPG sizing or ADR-0016 parameters belongs to
  #297, not this one.
