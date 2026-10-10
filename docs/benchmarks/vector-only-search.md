# Vector-only search latency — where the time went, the fix, and the rework (#291)

Measured: 2026-10-09 · Feeds: [#291](https://github.com/scramb/memory-manager/issues/291), [ADR-0007 addendum 2026-10-09](../adr/0007-storage-backend.md), [ADR-0016](../adr/0016-vector-index.md) · Work package: WP-32

#269's 3-replica load smoke added a `search_vector_only` scenario (queries with no lexical marker, #266) that measured p95 361–617 ms against the 10k-note/50k-chunk load-test vault, while lexical/hybrid `search` stayed at 154–177 ms — over F-01's 300 ms search budget, and not gated in `loadtest/k6/smoke.js` pending this task.

A first pass at this task fixed the dominant latency cost (the full-text side's own unfiltered retry, below) but shipped two defects a verification round caught: (1) `loadtest/load.py` stamped a hardcoded `chunks.model` that never matched the server's own `EMBEDDING_MODEL`, so every vector leg in the actual `make loadtest-smoke` run returned zero rows — the measured "fast" p95 was a search finding nothing, not a validated fix; (2) the retry-skip condition fired whenever an embedding existed at all, regardless of whether the vector side actually found anything, which would have silently hidden exactly that regression; (3) neither `search.js` nor `lib.js` ever checked that a search's own known-correct note came back in its results, so the latency scenarios alone could not have caught (1) or (2).

A second verification round then found the first attempt at fix (3) itself unreliable: a hit-rate metric fed from the *load* scenarios' own random (principal, query) draws had a sample count that varied run to run (0–14 observations per 60 s run) and was flaky purely from that - one of three otherwise-identical runs failed the `search` gate on a single unlucky miss (7/8 = 87.5 %), and `search_vector_only` drew zero eligible samples in every one of those three runs, so its own gate never actually tested anything. Round 2's fix (below, "Deterministic correctness scenarios") replaces that random sampling with a fixed, visibility-chosen set of query/principal pairs and a companion sample-count threshold, run as its own non-timed k6 scenario. This note covers the original latency finding, all four fixes, and the calibration evidence for each round.

## Hardware and software profile

Same shared dev VM as `baseline.md`: 8 vCPU, 47 GiB RAM, virtual disk. PostgreSQL 16.15, pgvector 0.8.7, `mm-pg` container (podman). The per-stage profiling in "Where the time went" ran directly against `asyncpg`/`memory_manager.search`, not through `serve --http` or k6; the hit-rate and end-to-end latency numbers further down are from the actual `make loadtest-smoke` DoD command (k6, 3 replicas, real request path).

## Where the time went

### Method

1. Generated and loaded the exact vault `scripts/loadtest-smoke.sh` builds for `LOADTEST_EMBEDDINGS=stub`: `loadtest.generate --notes 10000 --users 200 --groups 20 --seed 1`, then `loadtest.load --with-chunks --seed 1` into a dedicated `mm_loadtest_profile` database (49,944 chunks: `user` 48,836 / `group` 673 / `project` 374 / `org` 61), `analyze chunks, notes`. (This ad hoc script passed the correct `model` string by hand - the hardcoded-mismatch bug below was only in `loadtest/load.py`'s own default, not in this manual repro.)
2. Took one `queries.jsonl` entry from the vector-only share (`"Which note best matches the topic of copperleaf echofen glimmer yonder?"`, no lexical marker by construction, #266) and its planted vector (`loadtest.vectors.near_vector`).
3. Ran `memory_manager.search`'s own functions directly (`fulltext_search`, `_vector_search_legs`, `_search_connection`) under `db.rls.request_identity` for a synthetic principal with real registry-derived readable namespaces (`mm_readable_ns()` — this loadtest's principals are genuine members of several groups/projects at the database level, not the "no groups claim" token-only view `search.js`'s own comment describes; the namespace filter a request actually carries, `None` or `["me", "org"]`, still matches `search.js`'s own `searchEntry` logic), timing each stage with `time.perf_counter()`, 4–5 repetitions per case after a warm-up call.
4. Used `EXPLAIN (ANALYZE, BUFFERS, VERBOSE)` on `fulltext_search`'s own built SQL (`_build_fulltext_query`) to see why the selective-filtered attempt came back empty and what the retry actually scanned.

### Findings

**The full-text side's own unfiltered retry (#117, ADR-0007 addendum) is the dominant cost for a vector-only query, not the four per-kind vector legs.** `EXPLAIN` on the vector-only query text above showed the selective-filtered `candidates` CTE finding zero rows (the query's own generic vocabulary happens to match no chunk this principal can read *and* satisfies the selective lexeme filter at once — the same shape the retry's #117 rescue was built for, just with nothing to rescue), which triggers `fulltext_search`'s own unfiltered retry: a second full statement execution that scanned ~32k buffers and took ~135 ms by itself, then still returned only generic-word noise (never the right answer — a vector-only query was engineered to have none). The four vector legs, by contrast, cost 45–95 ms *together* for this vault size, including three `SET LOCAL` statements and one query each, per the per-stage table below. RLS function cost (`mm_readable_ns()`, `baseline.md`'s own ~1.5 ms/call) is a small fraction of either side and not the lever here.

#### Per-stage timing, one connection, no concurrency (ms, `oid-user-00017`, 4–5 reps)

| Stage | `namespaces` filter | Before (retry always, pre-#291 code) | After (retry skipped once the vector side has enough) |
|---|---|---|---|
| `fulltext_search` | none | 123–180 | 37–38 |
| `fulltext_search` | `["me", "org"]` | 74–99 | 21–24 |
| `_vector_search_legs` (4 kinds) | none | 62–76 | unchanged |
| `_vector_search_legs` (2 visible kinds) | `["me", "org"]` | 32–49 | unchanged |
| fulltext + vector, same connection (`rrf_fuse` included) | none | 186–256 | 95–114 |
| fulltext + vector, same connection (`rrf_fuse` included) | `["me", "org"]` | 105–140 | 53–69 |

The fix roughly halves the combined fulltext+vector cost for a query with no lexical match, by skipping a retry attempt that was never going to find the right answer anyway once the vector side already has enough to work with.

## The three fixes (#291 rework)

**1. `chunks.model` mismatch (loader vs. server).** `loadtest/load.py --with-chunks` stamped a hardcoded `_CHUNK_VECTOR_MODEL = "loadtest-synthetic"` onto every chunk row regardless of what the server it was loaded for actually queries with; `scripts/loadtest-smoke.sh` independently set the server's own `EMBEDDING_MODEL=loadtest-stub`. `vector_search`/`_vector_search_legs` filter every leg on `c.model = $n`, so every leg found zero rows in every `make loadtest-smoke` run with `LOADTEST_EMBEDDINGS=stub` - the "fast" `search_vector_only` p95 the first pass of this task reported was a search that structurally found nothing, not a validated fix. Fixed by threading one `--embedding-model`/`embedding_model` parameter from `loadtest.load`'s CLI (default: `$EMBEDDING_MODEL`, falling back to the old hardcoded value) down to `_chunk_copy_lines`, and by `loadtest-smoke.sh` defining the model name exactly once (`EMBEDDING_MODEL_VALUE`) and passing the same value to both the loader and every server/worker's own `EMBEDDING_MODEL`.

**2. The retry-skip condition itself.** The first pass skipped `fulltext_search`'s own unfiltered retry whenever `embedding is not None` - true whenever a `provider` is configured and embedding the query succeeded, regardless of whether the resulting vector legs found anything at all. Combined with fix 1's bug, this hid the regression completely: vector legs found zero rows, the retry that could have at least tried to rescue something was skipped anyway, and the "finds nothing" search was also fast. `_hybrid_search_impl` now runs its vector legs *before* the full-text side and skips the retry only once `vector_hit_count >= limit` (the final note count the call returns) - an outcome-based condition. Recorded as a dated addendum in ADR-0007, next to the original #117 decision it modifies.

**3. No correctness check at all (round 1 attempt).** Neither `search.js` nor `lib.js` ever asserted that a search actually found its own query's known-correct note (`queries.jsonl`'s `expected` field) - every scenario only checked the call succeeded and was fast. A `search_hit_rate` custom k6 `Rate` metric recorded, for every `search`/`searchVectorOnly` load-scenario call where the *randomly* drawn query's own namespace happened to be visible to the *randomly* drawn principal, whether `entry.expected` showed up in the top-N results. This caught the round-1 regression demo (below) but turned out flaky in normal operation - see fix 4.

**4. Deterministic correctness scenarios (round 2).** A random (principal, query) draw's own visibility under RLS is a coin flip neither side controls - most notes are personal (#291's own measurement: ~97 % of the WP-32 load-test vault's chunks), each in one specific namespace, so whether a given draw even has a known ground truth varies run to run. Calibration evidence (below) showed this made fix 3's metric unreliable: 1 of 3 otherwise-identical runs failed the `search` gate on one unlucky miss, and `search_vector_only` drew zero eligible samples in all three. Fix: `search.js` now *selects* (not draws) up to `CORRECTNESS_SAMPLE_SIZE = 50` lexical and 50 vector-only query/principal pairs whose own namespace a real token can actually read - by construction, from `loadtest.generate`'s own deterministic generation order, not by chance - and `smoke.js` runs them as two dedicated, non-timed `shared-iterations` scenarios (`vus: 1`, tagged `phase: 'correctness'`, entirely separate from the load scenarios' own latency gates). Getting to 50 *visible* pairs of each kind needed `loadtest.load --tokens 200` (every personal namespace, not the previous default of 50 of 200) - `scripts/loadtest-smoke.sh` now passes that explicitly, with the exact shortfall measured below. Each scenario's own `search_correctness_hit_rate{check:lexical|vector}` (a `Rate`) is gated together with `search_correctness_samples{check:lexical|vector}` (a `Counter`, `count>=50`) - the companion count is what makes a run with too few (or zero) samples fail loud, since a k6 `Rate` threshold alone passes trivially with zero samples (confirmed directly: `rate>=0.8` on an empty `Rate` metric reports `rate=0.00%` and is *not* crossed).

## Hit-rate calibration

### Round 1 (random draws from the load scenarios - superseded)

Only sampled for `inOwnOrOrg` draws (the query's own namespace is the calling principal's own alias or `org`); every other draw has an undefined ground truth under RLS. Given `loadtest-smoke.sh`'s own call rates and the ~1–3 active VUs a 60 s run actually needs, this was a **small, run-to-run-variable sample**: 0 to 14 observations per scenario across the calibration runs below.

| Run | State | `search` hits/attempts | `search_vector_only` hits/attempts |
|---|---|---|---|
| reworked fix, run 1 | fixed | 1/1 | 2/2 |
| reworked fix, run 2 | fixed | 0/0 | 3/4 |
| regression repro | `chunks.model` hardcoded to a value the server never queries with | 9/11 (**0.82, below the 0.9 gate - build failed as intended**) | 0/0 |
| calibration 1 | fixed | 1/1 | 0/0 |
| calibration 2 | fixed | 2/2 | 0/0 |
| calibration 3 | fixed | 1/1 | 1/1 |
| calibration 4 | fixed | 2/2 | 0/0 |

Aggregated across every *fixed*-state run above: `search` **5/5 ≈ 100 %**; `search_vector_only` **6/7 ≈ 86 %** - but with `search_vector_only` drawing *zero* samples in 4 of 6 fixed-state runs, and `search` itself failing once at 7/8 in a run not shown above (round 2's own trigger for this rework). This is the unreliability fix 4 replaces, not a result to calibrate a production threshold against.

### Round 2 (deterministic, visibility-chosen pairs)

Three consecutive `make loadtest-smoke` runs (`LOADTEST_REPLICAS=3 LOADTEST_KILL_AFTER=30 LOADTEST_SHARED_STATE=postgres LOADTEST_EMBEDDINGS=stub`), host checked quiet (`uptime` 1-minute load 2.5–2.9) before each:

| Run | `check:lexical` hits/count | `check:vector` hits/count | Exit |
|---|---|---|---|
| 1 | 50/50 (100 %) | 50/50 (100 %) | 0 |
| 2 | 50/50 (100 %) | 50/50 (100 %) | 0 |
| 3 | 50/50 (100 %) | 50/50 (100 %) | 0 |

Every run drew exactly 50 samples for each check - no longer 0–14, because the pairs are selected, not drawn. `smoke.js`'s thresholds are `rate>=0.9` plus `count>=50` for both `check:lexical` and `check:vector`: 0.9 rather than 1.0 despite three straight 100 % runs, because ADR-0016's own open caveat ("`org`'s recall numbers... should be re-checked against real content") means occasional approximate-HNSW misses are expected at this sample size, not necessarily a bug - a single miss in 50 (98 %) still clears 0.9 comfortably, while a real regression (below) misses far more than that.

**Regression reproduced and reverted.** `scripts/loadtest-smoke.sh`'s `--embedding-model` was temporarily hardcoded to a value the server never queries with (the exact round-1 bug, reintroduced by hand - no tracked file left in that state afterwards) and `make loadtest-smoke` run again: `check:lexical` stayed 50/50 (100 % - lexical search does not depend on embeddings at all, correctly unaffected), `check:vector` dropped to **2/50 (4 %)**, crossed its own `rate>=0.9` threshold, and the build failed (k6 exit 99). Reverting the one line restored all three green runs above.

## `make eval`

Unaffected: `recall@5=0.84`, `mrr=0.7123`, identical before and after this rework. The golden set runs against the Git backend (flat `chunks`, no ADR-0016 per-kind legs, no load-test loader), and the retry-skip condition changes only whether an already-redundant second full-text statement runs - never which candidates `fulltext_search`/`vector_search` themselves find.

## Limitations

- The per-stage table's profiling is single-connection, no concurrency, no HTTP/MCP layer, no replica contention - the DoD's own `make loadtest-smoke` run (k6, 3 replicas, real request path) is the actual gate; that table explains *why* the latency fix works, the k6 run confirms *how much*.
- One query text, one principal for the per-stage table. The retry's cost scales with how many buffers the unfiltered scan touches, which grows with vault/table size (ADR-0007 addendum's own 1.3 s at 60k chunks / 8.8 s at 1M chunks for the *un-capped* version of this problem) — the saving should grow, not shrink, at the 1M/5M-chunk target size this profiling run does not reach.
- Round 2's fixed N=50 is a deliberately modest floor (the task's own "N >= 50"), not a tight confidence interval - 50 samples at 100 % still leaves room for a real ~5-10 % recall gap to hide under the `rate>=0.9` threshold. A future task wanting a tighter bound on `search_vector_only`'s real recall should raise `CORRECTNESS_SAMPLE_SIZE` (`search.js`) together with `loadtest.load --tokens` (more tokens -> more eligible pairs) rather than rely on a bigger `MM_MEASURE_DURATION` - the correctness scenarios do not scale with measure duration at all (`shared-iterations`, fixed iteration count).
- `loadtest.load --tokens 200` (every personal namespace) was the minimum found empirically to clear 50 visible vector-only pairs on this vault/seed (default 50 tokens: only 28 of the vault's 80 vector-only marker queries were visible to any of them; 200 tokens: 77). A vault generated with different `--notes`/`--users`/`--seed` values could need a different `--tokens` to still clear 50 - `search.js`'s own `CORRECTNESS_LEXICAL_PAIRS.length === 0` guard catches a *total* failure to find any eligible pair, but `search_correctness_samples{check:...}`'s `count>=50` threshold is what actually catches "found some, but fewer than 50" for any vault/token combination.
