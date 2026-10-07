// SPDX-License-Identifier: AGPL-3.0-only
//
// memory_search scenario (#108): one `memory_search` call per iteration, the
// query drawn from `loadtest.generate`'s `queries.jsonl` - each entry has a
// single marker-bearing note as its known-correct answer (see `loadtest/
// generate.py`'s docstring), but this scenario only asserts the call
// succeeded; recall/MRR against the marker is the retrieval eval's job
// (`memory-manager eval`), not this latency scenario's.

import { SharedArray } from 'k6/data';
import { toolsCall, vuToken } from './lib.js';

const QUERIES_FILE = __ENV.MM_QUERIES_FILE;
if (!QUERIES_FILE) {
  throw new Error("MM_QUERIES_FILE is required (loadtest.generate's queries.jsonl)");
}

const queries = new SharedArray('loadtest-queries', function () {
  return open(QUERIES_FILE)
    .split('\n')
    .filter(function (line) {
      return line.length > 0;
    })
    .map(JSON.parse);
});

export function search() {
  const entry = queries[Math.floor(Math.random() * queries.length)];
  toolsCall(vuToken(), 'memory_search', { query: entry.query, limit: 5 });
}
