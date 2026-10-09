// SPDX-License-Identifier: AGPL-3.0-only
//
// memory_search scenario (#108, #124): one `memory_search` call per
// iteration, the query drawn from `loadtest.generate`'s `queries.jsonl` -
// each entry has a single marker-bearing note as its known-correct answer
// (see `loadtest/generate.py`'s docstring). `search`/`searchVectorOnly`
// below are latency-only, same as before #291: the query/principal pairing
// is random, so whether the drawn query's own note is even visible to the
// drawn principal under RLS varies run to run - never a stable ground
// truth a hit-rate threshold could gate on without being flaky at the
// small sample sizes one load-test run produces (#291 round 2 found this
// the hard way: a random-draw hit-rate metric on these two scenarios
// crossed its own threshold in 1 of 3 otherwise-identical runs purely from
// sampling noise, and had zero samples at all for `searchVectorOnly` in
// every one of those three).
//
// Correctness (a search actually finding its own query's known-correct
// note, not just answering fast) is instead `searchCorrectnessLexical`/
// `searchCorrectnessVectorOnly` further down: a *deterministic* set of
// query/principal pairs chosen by visibility, not drawn at random, so
// every sample has a known ground truth and the sample count is fixed
// rather than a function of how many random draws happened to line up.
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
import { Counter, Rate } from 'k6/metrics';
import { scenario } from 'k6/execution';
import { TOKENS, toolsCall, vuPrincipal } from './lib.js';

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
const LEXICAL_QUERIES = queries.filter(function (entry) {
  return !isVectorOnlyQuery(entry);
});

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

// -- Correctness (#291 round 2): deterministic, visibility-chosen samples --
//
// `search`/`searchVectorOnly` above draw a random (principal, query) pair
// each iteration; whether the drawn query's own note is visible to the
// drawn principal is then a coin flip neither side controls (`entry.
// namespaces` is the note's own home namespace, almost always one
// *specific* personal namespace, #266 - see `loadtest.generate`'s own
// docstring on its Zipf-weighted namespace picker). A hit-rate metric fed
// only from "visible" random draws has a sample count that varies run to
// run - including, at the concurrency/call-rate this smoke test runs at,
// zero (#291 round 2's own finding: `searchVectorOnly` never once drew a
// visible pair in three identical runs).
//
// `buildCorrectnessPairs` instead *selects* up to `CORRECTNESS_SAMPLE_SIZE`
// entries whose own namespace this vault's tokens can actually read - by
// construction, not by chance - pairing each with the one principal that
// can read it (`PRINCIPAL_BY_ALIAS`, or any token at all for an `org`
// entry, since `org` is everyone's). `queries.jsonl`'s own entries are
// already in `loadtest.generate`'s deterministic generation order, so
// taking the first `CORRECTNESS_SAMPLE_SIZE` matches (rather than, say,
// every one of them, or a random subset) is itself a fixed, reproducible
// choice, not a per-run draw.
const CORRECTNESS_SAMPLE_SIZE = 50;

const PRINCIPAL_BY_ALIAS = {};
TOKENS.forEach(function (principal) {
  PRINCIPAL_BY_ALIAS[principal.alias] = principal;
});
// Any token can read `org` (`namespaces.Resolution.readable()`) - the first
// one in `TOKENS` is as good as any other for that case.
const ANY_PRINCIPAL = TOKENS[0];

function principalForEntry(entry) {
  const namespace = entry.namespaces[0];
  if (namespace === 'org') {
    return ANY_PRINCIPAL;
  }
  return PRINCIPAL_BY_ALIAS[namespace] || null;
}

function buildCorrectnessPairs(entries) {
  const pairs = [];
  for (let i = 0; i < entries.length && pairs.length < CORRECTNESS_SAMPLE_SIZE; i++) {
    const entry = entries[i];
    const principal = principalForEntry(entry);
    if (principal !== null) {
      pairs.push({ principal: principal, entry: entry });
    }
  }
  return pairs;
}

// Exported so `smoke.js` can size its own `shared-iterations` scenarios
// off the actual pair count - `loadtest.load --tokens` (`scripts/
// loadtest-smoke.sh`'s own `--tokens 200`, every personal namespace) is
// what makes both of these reach `CORRECTNESS_SAMPLE_SIZE` on the vault
// this script builds; a vault/token setup that falls short still runs
// (with fewer than `CORRECTNESS_SAMPLE_SIZE` iterations) rather than
// erroring here - `smoke.js`'s own `search_correctness_samples{check:...}`
// `count>=50` threshold is what actually enforces the minimum, the same
// way a `Rate` metric with zero samples needs a companion count check to
// fail loud rather than pass by default (#291 round 2's own finding).
export const CORRECTNESS_LEXICAL_PAIRS = buildCorrectnessPairs(LEXICAL_QUERIES);
export const CORRECTNESS_VECTOR_ONLY_PAIRS = buildCorrectnessPairs(VECTOR_ONLY_QUERIES);

if (CORRECTNESS_LEXICAL_PAIRS.length === 0 || CORRECTNESS_VECTOR_ONLY_PAIRS.length === 0) {
  throw new Error(
    'loadtest/k6/search.js: no visible (principal, query) pairs for one or both correctness ' +
      'scenarios - check loadtest.load --tokens covers enough personal namespaces for this ' +
      "vault's queries.jsonl"
  );
}

// One `Rate` (hit/miss) and one `Counter` (samples actually taken) per
// correctness check, both tagged `check:lexical`/`check:vector` - the
// `Counter` is what lets `smoke.js` gate on "at least N samples", which a
// `Rate` threshold alone cannot do (k6 passes a `rate>=X` threshold with
// zero samples rather than failing it, #291 round 2's own finding: the
// vector-only hit-rate gate from round 1 never actually ran in three
// consecutive load-test runs and still reported green).
const searchCorrectnessHitRate = new Rate('search_correctness_hit_rate');
const searchCorrectnessSamples = new Counter('search_correctness_samples');

function extractResultIds(outcome) {
  if (!outcome.ok || outcome.result === null) {
    return [];
  }
  const structured = outcome.result.structuredContent;
  if (!structured || !Array.isArray(structured.results)) {
    return [];
  }
  return structured.results.map(function (item) {
    return item.id;
  });
}

function runCorrectnessCheck(pair, checkTag) {
  const args = { query: pair.entry.query, limit: 5, namespaces: READABLE_ALIASES };
  const outcome = toolsCall(pair.principal.token, 'memory_search', args);
  const resultIds = extractResultIds(outcome);
  const hit = pair.entry.expected.some(function (id) {
    return resultIds.indexOf(id) !== -1;
  });
  const tags = { check: checkTag };
  searchCorrectnessHitRate.add(hit, tags);
  searchCorrectnessSamples.add(1, tags);
}

// `exec.scenario.iterationInTest` (`k6/execution`): a globally unique,
// zero-based iteration number for this scenario's run, stable regardless
// of how many VUs end up serving it - `smoke.js` runs both of these at
// `vus: 1` (plain sequential correctness checks, not a load pattern), so
// this is simply 0..N-1 in order, indexing `CORRECTNESS_*_PAIRS` directly.
export function searchCorrectnessLexical() {
  const pairs = CORRECTNESS_LEXICAL_PAIRS;
  const pair = pairs[scenario.iterationInTest % pairs.length];
  runCorrectnessCheck(pair, 'lexical');
}

export function searchCorrectnessVectorOnly() {
  const pairs = CORRECTNESS_VECTOR_ONLY_PAIRS;
  const pair = pairs[scenario.iterationInTest % pairs.length];
  runCorrectnessCheck(pair, 'vector');
}
