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

// `loadtest.generate._build_vector_only_query_text` (#266) writes exactly
// this phrasing for the 1-in-5 share of marker queries that drop the
// lexical marker on purpose, so only a vector search against the entry's
// own `vector_key` can resolve it - `queries.jsonl` carries no separate
// boolean field for this, so this prefix is the one thing that tells the
// two query shapes apart here. Checked once, at init, rather than per
// iteration.
const VECTOR_ONLY_QUERY_PREFIX = 'Which note best matches the topic of';

function isVectorOnlyQuery(entry) {
  return entry.query.indexOf(VECTOR_ONLY_QUERY_PREFIX) === 0;
}

const VECTOR_ONLY_QUERIES = queries.filter(isVectorOnlyQuery);
if (VECTOR_ONLY_QUERIES.length === 0) {
  throw new Error(
    'loadtest/k6/search.js: queries.jsonl carries no vector-only entries (#269) - ' +
      'regenerate the vault with loadtest.generate at a large enough --notes for its ' +
      '1-in-5-of-marker-queries vector-only share to produce at least one'
  );
}

const READABLE_ALIASES = ['me', 'org'];

function searchEntry(principal, entry) {
  const inOwnOrOrg = entry.namespaces.some(function (namespace) {
    return namespace === principal.alias || namespace === 'org';
  });
  const args = { query: entry.query, limit: 5 };
  if (inOwnOrOrg) {
    args.namespaces = READABLE_ALIASES;
  }
  toolsCall(principal.token, 'memory_search', args);
}

export function search() {
  const principal = vuPrincipal();
  const entry = queries[Math.floor(Math.random() * queries.length)];
  searchEntry(principal, entry);
}

// A dedicated scenario share (#269, `smoke.js`'s own `search_vector_only`)
// drawing only from `VECTOR_ONLY_QUERIES` - exercises hybrid search's pure
// vector-ranking branch specifically, rather than leaving it a random,
// unmeasured ~20% slice of the plain `search` scenario above.
export function searchVectorOnly() {
  const principal = vuPrincipal();
  const entry = VECTOR_ONLY_QUERIES[Math.floor(Math.random() * VECTOR_ONLY_QUERIES.length)];
  searchEntry(principal, entry);
}
