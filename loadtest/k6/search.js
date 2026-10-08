// SPDX-License-Identifier: AGPL-3.0-only
//
// memory_search scenario (#108, #124): one `memory_search` call per
// iteration, the query drawn from `loadtest.generate`'s `queries.jsonl` -
// each entry has a single marker-bearing note as its known-correct answer
// (see `loadtest/generate.py`'s docstring), but this scenario only asserts
// the call succeeded; recall/MRR against the marker is the retrieval eval's
// job (`memory-manager eval`), not this latency scenario's.
//
// A static token carries no `groups` claim, so the only namespaces it can
// ever read under RLS are its own personal namespace (`me`) and `org`
// (`namespaces.Resolution.readable()`). When the drawn query's own marker
// note lives in one of those two, the call scopes itself to exactly that
// pair - what a real client addressing its own readable set would pass;
// every other query (a marker note in a group this token has no claim for)
// falls back to no `namespaces` filter at all, letting the server narrow to
// whatever the matrix already allows rather than asserting a namespace this
// token structurally cannot see.

import { SharedArray } from 'k6/data';
import { toolsCall, vuPrincipal } from './lib.js';

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

const READABLE_ALIASES = ['me', 'org'];

export function search() {
  const principal = vuPrincipal();
  const entry = queries[Math.floor(Math.random() * queries.length)];
  const inOwnOrOrg = entry.namespaces.some(function (namespace) {
    return namespace === principal.alias || namespace === 'org';
  });
  const args = { query: entry.query, limit: 5 };
  if (inOwnOrOrg) {
    args.namespaces = READABLE_ALIASES;
  }
  toolsCall(principal.token, 'memory_search', args);
}
