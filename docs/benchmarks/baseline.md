# Latency baseline at 100k notes, one replica — R2 vs R1 (approximated)

Measured: 2026-10-08 · Feeds: [#109](https://github.com/scramb/memory-manager/issues/109), [ADR-0008](../adr/0008-namespace-permissions.md) · Work package: WP-21

Owner decision 2026-10-07 (#109): R1 is approximated at the database level — `mm_readable_ns()`/`mm_writable_ns()` are replaced by a lookup in a table precomputed from R2's own functions, run once for every registered principal; the RLS policies and the request path stay byte-for-byte identical between variants (`loadtest/r1_approximation.sql`). Embeddings are off for this run (moved to WP-32/#125): no HNSW index, no vector search cost in any of the numbers below.

## Hardware and software profile

- 8 vCPU (QEMU Virtual CPU version 2.5+, 1 thread/core), 47 GiB RAM, virtual disk (QEMU HARDDISK, `ROTA=1`) — a shared build/dev VM, not isolated bare metal; re-check on target hardware before treating absolute numbers as a capacity commitment.
- PostgreSQL 16.15, pgvector 0.8.7, Postgres defaults (`shared_buffers=128MB`, `work_mem=4MB`, `maintenance_work_mem=64MB`, `jit=on`), single `mm-pg` container, `--shm-size` 64 MB (podman default).
- One `memory-manager serve --http` replica, `STORAGE_BACKEND=postgres`, `EMBEDDING_PROVIDER=none`, rate limits raised out of the way (`scripts/loadtest-smoke.sh`).
- k6 `docker.io/grafana/k6:2.3.0`, run on the same host as `mm-pg` and the server (network cost is loopback, not representative of a real network hop between replica and client).
- Dataset: `loadtest.generate --notes 100000 --users 200 --groups 20 --seed 1` (deterministic; `#107`).

## Method

`scripts/loadtest-smoke.sh` under `LOADTEST_NOTES=100000 MM_MEASURE_DURATION=300s`, with `LOADTEST_RESULTS_DIR` set per run so k6's `--summary-export` (`--summary-trend-stats 'avg,med,p(95),p(99),max'`), `pg_stat_user_functions` (`track_functions=all`, set before the server starts) and the generate/load/reindex step timings are captured before the EXIT trap drops the database. Order: R2 → R1 → R2, a fresh `mm_loadtest` database for each run (drop, regenerate, reload, reindex, analyze — never reused across runs), same seed every time. The two R2 runs bracket the R1 run so their own spread shows the measurement noise at this scale, independent of which variant is "better".

```
LOADTEST_NOTES=100000 MM_MEASURE_DURATION=300s LOADTEST_RLS_VARIANT=r2 LOADTEST_RESULTS_DIR=<dir> make loadtest-smoke   # run 1 (R2)
LOADTEST_NOTES=100000 MM_MEASURE_DURATION=300s LOADTEST_RLS_VARIANT=r1 LOADTEST_RESULTS_DIR=<dir> make loadtest-smoke   # run 2 (R1, approximated)
LOADTEST_NOTES=100000 MM_MEASURE_DURATION=300s LOADTEST_RLS_VARIANT=r2 LOADTEST_RESULTS_DIR=<dir> make loadtest-smoke   # run 3 (R2)
```

**R1 approximation (`loadtest/r1_approximation.sql`):** for every row in `users`, the script sets the transaction-local identity (`app.oid`, `app.roles='Memory.User'` — the one role every synthetic principal carries, `loadtest.load`'s own `_TOKEN_ROLES`) and calls R2's own `mm_readable_ns()`/`mm_writable_ns()` once, freezing the result into a table `mm_r1_lookup (oid, readable_ns, writable_ns)`. It then `create or replace`s both functions as a lookup against that table, repeating `stable`, `security definer` and the pinned `search_path` explicitly, since `create or replace` resets any attribute the new definition does not repeat. `mm_ensure_personal_ns()` and `mm_principal_namespaces()` are untouched by either variant — they are not on the content-table read/write path #109 measures, and their own numbers below (nearly identical across all three runs) confirm that nothing else changed between variants.

Each run: generate → bulk-load under RLS with every principal registered → `reindex --full` → `analyze chunks, notes` → (R1 only) apply the approximation → `alter database … set track_functions='all'` → start `serve --http` → one idle sequential call per tool (isolated baseline) → 10 s warmup + 300 s measured k6 run (search 10/s, read 7/s, write+edit 3 iterations/2 s, per F-01's own call mix — `loadtest/k6/smoke.js`).

A red k6 threshold at 100k notes on one replica is a result, not an abort: `scripts/loadtest-smoke.sh` still writes `pg_stat_user_functions` and the summary export regardless of k6's own exit code (99 on a crossed threshold, 0 otherwise).

## Results

### Per-tool latency (k6 `http_req_duration`, ms)

| Tool | Variant | p50 (med) | p95 | k6 threshold |
|---|---|---|---|---|
| `memory_search` | R2 (run 1) | 80.9 | 419.6 | p95<300 — **crossed** |
| `memory_search` | R1 (approximated) | 56.7 | 145.2 | p95<300 — met |
| `memory_search` | R2 (run 3) | 65.7 | 160.0 | p95<300 — met |
| `memory_read` | R2 (run 1) | 12.1 | 18.6 | p95<100 — met |
| `memory_read` | R1 (approximated) | 10.2 | 15.5 | p95<100 — met |
| `memory_read` | R2 (run 3) | 11.3 | 15.8 | p95<100 — met |
| `memory_write`+`memory_edit` combined (`write` scenario) | R2 (run 1) | 47.0 | 71.1 | p95<200 — met |
| `memory_write`+`memory_edit` combined (`write` scenario) | R1 (approximated) | 21.3 | 31.2 | p95<200 — met |
| `memory_write`+`memory_edit` combined (`write` scenario) | R2 (run 3) | 43.0 | 57.5 | p95<200 — met |
| `memory_write` only | R2 (run 1) | 33.0 | 57.8 | (no separate threshold) |
| `memory_write` only | R1 (approximated) | 21.4 | 31.2 | (no separate threshold) |
| `memory_write` only | R2 (run 3) | 30.8 | 41.9 | (no separate threshold) |
| `memory_edit` only | R2 (run 1) | 53.0 | 80.4 | (no separate threshold) |
| `memory_edit` only | R1 (approximated) | 21.3 | 31.0 | (no separate threshold) |
| `memory_edit` only | R2 (run 3) | 49.5 | 62.2 | (no separate threshold) |

All three runs: 0 % `http_req_failed`, 100 % checks passed (6,200+ requests each) — the crossed threshold on R2 run 1's search p95 is purely a latency overrun, never an error.

**R2–R2 noise floor** (runs 1 and 3, same variant, fresh DB each, same seed): search p95 419.6 ms vs 160.0 ms, write p95 71.1 ms vs 57.5 ms. The spread is wide enough that run 1's crossed search threshold is, by itself, inconclusive about R2 vs the 300 ms target — see Limitations.

### RLS and namespace-resolution function cost (`pg_stat_user_functions`, `track_functions=all`)

Mean self-time per call, i.e. `self_time / calls`, in ms:

| Function | R2 (run 1) | R1 (approximated) | R2 (run 3) |
|---|---|---|---|
| `mm_readable_ns()` | 1.621 (40,532 calls) | **0.102** (40,573 calls) | 1.452 (40,504 calls) |
| `mm_writable_ns()` | 1.603 (5,584 calls) | **0.096** (5,572 calls) | 1.461 (5,572 calls) |
| `mm_ensure_personal_ns()` | 0.065 (6,194 calls) | 0.059 (6,199 calls) | 0.058 (6,191 calls) |
| `mm_principal_namespaces()` | 1.280 (6,194 calls) | 1.214 (6,199 calls) | 1.173 (6,191 calls) |

`mm_ensure_personal_ns()` and `mm_principal_namespaces()` are not touched by the R1 approximation (method, above) and their near-identical numbers across all three runs confirm that: R1 only changes `mm_readable_ns()`/`mm_writable_ns()`'s own derivation cost, roughly a 14–16× reduction in self-time at 100k notes / 200 users / 20 groups, nothing else on the request path.

### Step timings and isolated single-call baseline

| | R2 (run 1) | R1 (approximated) | R2 (run 3) |
|---|---|---|---|
| generate (100k notes) | 94.3 s | 93.7 s | 93.5 s |
| load (bulk insert under RLS) | 105.2 s | 104.4 s | 104.0 s |
| `reindex --full` | 1,238.2 s | 1,233.9 s | 1,239.9 s |
| isolated `memory_search` (idle server, one call) | 157.8 ms | 146.1 ms | 157.4 ms |
| isolated `memory_read` (idle server, one call) | 12.4 ms | 11.5 ms | 12.8 ms |
| isolated `memory_write` (idle server, one call) | 35.8 ms | 33.5 ms | 37.3 ms |

`reindex --full` at 100k notes (embeddings off) takes ~20 minutes on this hardware regardless of RLS variant, as expected — the approximation only changes the two read/write policy functions, not indexing.

## Limitations

- **Single run per variant, one replica, one host.** No repeated-trial statistics beyond the R2–R2 pair above; the search p95 spread between the two R2 runs (419.6 ms vs 160.0 ms) shows the noise at this scale is large enough that a single crossed threshold is not strong evidence on its own.
- **k6 on the same host as the server and `mm-pg`.** CPU contention between the load generator, the server process and Postgres inflates every absolute number here; a real deployment has k6 (or real clients) on a separate host.
- **Postgres defaults, not production tuning.** `shared_buffers=128MB`/`work_mem=4MB` are far below what the research doc (`docs/research/enterprise.md` §3.5, "`shared_buffers` ~25 % of memory") recommends for this hardware. Tuning was explicitly out of scope for this task.
- **Embeddings off, no HNSW.** This baseline covers lexical search, read and write/edit only; the vector-search cost (and R1 vs R2 under HNSW) is WP-32/#125's own follow-up.
- **No group-membership claim exercised.** Every synthetic principal's static token carries only `Memory.User` and its own personal namespace (`loadtest.load`'s own scope, #124) — the group/project branches of `mm_readable_ns()`/`mm_writable_ns()` are populated in the registry but not read by any request in this run; a workload that reads/writes group or project namespaces would spend more time in those CTEs under R2, and R1's precomputed lookup would absorb that difference entirely at measurement time.
- **100k notes, not the 1M/5M-chunk target size.** F-01's own gate (`docs/features/F-01-enterprise-scale.md`, "search p95 < 300 ms, read p95 < 100 ms, write p95 < 200 ms … on 3 API replicas") is a target-size, multi-replica measurement; this is a 10× smaller, single-replica spike feeding the R1-vs-R2 decision, not that gate itself.

## Recommendation (non-binding — the decision is the owner's)

At 100k notes on one replica, R1's precomputed lookup cuts `mm_readable_ns()`/`mm_writable_ns()` self-time by 14–16× and tracks consistently lower write/edit p95 (31 ms vs 57–71 ms) than either R2 run; its search p95 (145 ms) sits inside the R2–R2 noise band (160–420 ms) rather than clearly below it. Given the wide R2–R2 spread, this single 100k run does not by itself show R2 missing its 300 ms search target — the one crossed threshold (R2 run 1) is as consistent with noise as with a real overrun. Before deciding between R1 and R2 for production, the owner may want: repeated trials to separate signal from noise at this scale, the same comparison under HNSW/embeddings (WP-32/#125), and a run with the group/project branches actually exercised (limitations, above) — R2's "two independent computations" safety property (ADR-0008) is cheap here only because `mm_readable_ns()` is well under half of total request latency even in its slower, non-approximated form.
