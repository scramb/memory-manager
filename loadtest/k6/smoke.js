// SPDX-License-Identifier: AGPL-3.0-only
//
// The loadtest-smoke composition (#108): search, read and write run as three
// concurrent scenarios against one server replica, modelled on F-01's
// capacity assumption (docs/features/F-01-enterprise-scale.md), not driven
// as fast as possible:
//
//   2,000 active users, 10 % concurrent at peak        -> 200 users
//   ~1 tool call per active user every ~10 s            -> ~20 calls/s cluster-wide
//   20 calls/s over 3 API replicas                      -> ~6.7 calls/s per replica
//   this smoke runs ONE replica at 3x its own share      -> ~20 calls/s total (worst case:
//     the whole cluster's call rate landing on a single replica, not divided across three)
//
// Call mix (F-01's own split of tool calls): search 50 %, read 35 %, write+edit
// 15 %. At ~20 calls/s that is search 10/s, read 7/s, write+edit 3/s (write.js
// makes two calls - memory_write then memory_edit - per iteration, so the
// write scenario's own iteration rate is 1.5/s, i.e. 3 iterations every 2s).
// The search 10/s is itself split further (#269): 8/s plain `search`
// (`search.js`'s own `search`, drawing from every query in `queries.jsonl`)
// plus 2/s `search_vector_only` (`searchVectorOnly`, #266's dedicated,
// lexical-marker-free share) - carved *out of* the existing 10/s search
// budget, not added on top of it, so a default (`LOADTEST_REPLICAS=1`, no
// kill) run still drives the one replica at the same total ~20 calls/s
// F-01's own capacity assumption above models, not ~22.
//
// Each scenario runs twice: a `*_warmup` instance (10s, `tags: {phase:
// 'warmup'}`) that fills connection pools and caches but is excluded from
// every threshold below - the `scenario`/`phase` tag values it carries never
// match a threshold's metric sub-tag - followed by the measured instance
// (`MM_MEASURE_DURATION`, default 60s, `tags: {phase: 'measure'}`, same
// scenario names `search`/`read`/`write` the thresholds already reference).
// `scripts/loadtest-smoke.sh` (`make loadtest-smoke`) is what actually runs
// this, against a freshly loaded 10k-note vault and one `memory-manager
// serve --http` process.

export { search, searchVectorOnly } from './search.js';
export { read } from './read.js';
export { write } from './write.js';

const WARMUP_DURATION = __ENV.MM_WARMUP_DURATION || '10s';
const MEASURE_DURATION = __ENV.MM_MEASURE_DURATION || '60s';

// `SEARCH_RATE` (#269: down from 10, see the module docstring above) is
// only the *plain* search share - `SEARCH_VECTOR_ONLY_RATE` is the other
// 2/s of the original 10/s search budget, broken out into its own scenario
// (`searchVectorOnly`) rather than left a random ~20% slice of this one, so
// its own latency/failure rate is visible on its own tag - the two together
// still sum to the original 10/s.
const SEARCH_RATE = Number(__ENV.MM_SEARCH_RATE || 8); // calls/s
const READ_RATE = Number(__ENV.MM_READ_RATE || 7); // calls/s
const WRITE_RATE = Number(__ENV.MM_WRITE_RATE || 3); // iterations per 2s (= 1.5/s, 3 calls/s)
const SEARCH_VECTOR_ONLY_RATE = Number(__ENV.MM_SEARCH_VECTOR_ONLY_RATE || 2); // calls/s

export const options = {
  scenarios: {
    search_warmup: {
      executor: 'constant-arrival-rate',
      exec: 'search',
      rate: SEARCH_RATE,
      timeUnit: '1s',
      duration: WARMUP_DURATION,
      preAllocatedVUs: 10,
      maxVUs: 30,
      tags: { phase: 'warmup' },
    },
    read_warmup: {
      executor: 'constant-arrival-rate',
      exec: 'read',
      rate: READ_RATE,
      timeUnit: '1s',
      duration: WARMUP_DURATION,
      preAllocatedVUs: 10,
      maxVUs: 20,
      tags: { phase: 'warmup' },
    },
    write_warmup: {
      executor: 'constant-arrival-rate',
      exec: 'write',
      rate: WRITE_RATE,
      timeUnit: '2s',
      duration: WARMUP_DURATION,
      preAllocatedVUs: 5,
      maxVUs: 15,
      tags: { phase: 'warmup' },
    },
    search_vector_only_warmup: {
      executor: 'constant-arrival-rate',
      exec: 'searchVectorOnly',
      rate: SEARCH_VECTOR_ONLY_RATE,
      timeUnit: '1s',
      duration: WARMUP_DURATION,
      preAllocatedVUs: 5,
      maxVUs: 15,
      tags: { phase: 'warmup' },
    },
    search: {
      executor: 'constant-arrival-rate',
      exec: 'search',
      rate: SEARCH_RATE,
      timeUnit: '1s',
      duration: MEASURE_DURATION,
      startTime: WARMUP_DURATION,
      // `preAllocatedVUs` (#269, down from 20): `constant-arrival-rate`
      // rotates iterations across its *whole* preallocated pool over a run
      // this long, not just the handful actually needed to sustain
      // SEARCH_RATE at this latency (measured: steady-state concurrency
      // never exceeds ~5 across every scenario combined) - so this number
      // is also the worst-case count of VUs that each independently
      // discover a killed replica once (`lib.js`'s own per-VU circuit
      // breaker, #269) before `LOADTEST_KILL_AFTER` is set. Kept small
      // enough that the sum across every measure scenario below stays
      // comfortably under `http_req_failed{phase:measure}`'s 1% budget;
      // `maxVUs` is untouched, so a real latency spike can still scale
      // this scenario up exactly as before.
      preAllocatedVUs: 3,
      maxVUs: 60,
      tags: { phase: 'measure' },
    },
    read: {
      executor: 'constant-arrival-rate',
      exec: 'read',
      rate: READ_RATE,
      timeUnit: '1s',
      duration: MEASURE_DURATION,
      startTime: WARMUP_DURATION,
      preAllocatedVUs: 2, // #269 - see the `search` scenario's own comment above
      maxVUs: 40,
      tags: { phase: 'measure' },
    },
    write: {
      executor: 'constant-arrival-rate',
      exec: 'write',
      rate: WRITE_RATE,
      timeUnit: '2s',
      duration: MEASURE_DURATION,
      startTime: WARMUP_DURATION,
      preAllocatedVUs: 2, // #269 - see the `search` scenario's own comment above
      maxVUs: 30,
      tags: { phase: 'measure' },
    },
    search_vector_only: {
      executor: 'constant-arrival-rate',
      exec: 'searchVectorOnly',
      rate: SEARCH_VECTOR_ONLY_RATE,
      timeUnit: '1s',
      duration: MEASURE_DURATION,
      startTime: WARMUP_DURATION,
      preAllocatedVUs: 3, // #269 - see the `search` scenario's own comment above
      maxVUs: 20,
      tags: { phase: 'measure' },
    },
  },
  thresholds: {
    'http_req_duration{scenario:search}': ['p(95)<300'],
    'http_req_duration{scenario:read}': ['p(95)<100'],
    'http_req_duration{scenario:write}': ['p(95)<200'],
    'checks{phase:measure}': ['rate>0.99'],
    'http_req_failed{phase:measure}': ['rate<0.01'],
    // `write` (#108/#124) covers both memory_write and memory_edit - an
    // empty condition list never fails (and never aborts), but still makes
    // k6 instantiate the submetric so `lib.js`'s `tool` tag shows up in
    // `--summary-export` (#109, WP-21: the baseline reports write and edit
    // latency separately).
    'http_req_duration{tool:memory_write}': [],
    'http_req_duration{tool:memory_edit}': [],
    // `search_vector_only` (#269) is reported, not gated: the embedding
    // stub's own round trip (`EMBEDDING_PROVIDER=openai` against
    // `loadtest.embedding_stub`, #268) adds latency the plain `search`
    // scenario's own p(95)<300 was never calibrated against, and
    // `LOADTEST_EMBEDDINGS=none` runs this scenario against plain
    // full-text with no matching chunk at all - neither should fail the
    // build over a threshold nobody has set on purpose yet.
    'http_req_duration{scenario:search_vector_only}': [],
  },
};
