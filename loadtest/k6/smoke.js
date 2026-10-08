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

export { search } from './search.js';
export { read } from './read.js';
export { write } from './write.js';

const WARMUP_DURATION = __ENV.MM_WARMUP_DURATION || '10s';
const MEASURE_DURATION = __ENV.MM_MEASURE_DURATION || '60s';

const SEARCH_RATE = Number(__ENV.MM_SEARCH_RATE || 10); // calls/s
const READ_RATE = Number(__ENV.MM_READ_RATE || 7); // calls/s
const WRITE_RATE = Number(__ENV.MM_WRITE_RATE || 3); // iterations per 2s (= 1.5/s, 3 calls/s)

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
    search: {
      executor: 'constant-arrival-rate',
      exec: 'search',
      rate: SEARCH_RATE,
      timeUnit: '1s',
      duration: MEASURE_DURATION,
      startTime: WARMUP_DURATION,
      preAllocatedVUs: 20,
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
      preAllocatedVUs: 15,
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
      preAllocatedVUs: 10,
      maxVUs: 30,
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
  },
};
