// SPDX-License-Identifier: AGPL-3.0-only
//
// Shared k6 helpers for the loadtest scenarios (#108, #124): one `tools/call`
// JSON-RPC POST against the MCP endpoint, stateless - no prior `initialize`,
// the server's stateless Streamable HTTP transport serves a bare `tools/call`
// anyway (ADR-0009 §1, pinned server-side by `tests/test_stateless_transport.
// py`) - plus the context side file `loadtest/load.py` writes: the server's
// base URLs (one per replica, #269) and one synthetic principal per static
// token, each carrying its own `alias` (its generator namespace alias),
// `namespaces` (its full membership, for bookkeeping) and `read_paths` (a
// sample of its own `me/...`/`org/...` paths - `memory_read` must never be
// driven by a ULID, only a vault-relative path, and never a path this
// principal cannot actually read under RLS).
//
// `MCP_JSON_RESPONSE` defaults to true (`config.py`), so every response here
// is a single JSON object, never an SSE stream - `toolsCall` always calls
// `res.json()` directly.

import http from 'k6/http';
import { check } from 'k6';
import { SharedArray } from 'k6/data';

const CONTEXT_FILE = __ENV.MM_CONTEXT_FILE;
if (!CONTEXT_FILE) {
  throw new Error("MM_CONTEXT_FILE is required (loadtest.load's JSON side file)");
}

// Read once in the init context (`open()` only works there) and shared
// across every VU via `SharedArray`, rather than re-parsing per VU.
const context = JSON.parse(open(CONTEXT_FILE));

// One entry per server replica (#269, `scripts/loadtest-smoke.sh`'s own
// `LOADTEST_REPLICAS`) - `toolsCall` spreads requests over them itself
// (module docstring above: "no local load balancer is needed"), via
// `pickBaseUrl` below.
export const BASE_URLS = context.base_urls;

export const TOKENS = new SharedArray('loadtest-tokens', function () {
  return context.tokens;
});

// Round-robin over `BASE_URLS`, per VU (module-level state here is a fresh
// copy per VU, the same scoping `nextRequestId` below already relies on -
// k6 runs each VU in its own JS VM instance, never sharing a `let` across
// VUs) - so every VU's own requests still spread evenly across every
// replica, even though no single counter is shared process-wide.
//
// `unhealthyBaseUrls` (#269) is this VU's own memory of a base URL that
// already failed at the transport level (`response.status === 0`: refused
// or reset, exactly what a `kill -9`'d replica produces) - once marked, this
// VU never round-robins to it again for the rest of the run. Without this,
// a killed replica would keep taking its full round-robin share of every
// VU's requests for however long is left in the run, not just the handful
// already in flight to it at the moment of the kill - the one thing
// `scripts/loadtest-smoke.sh`'s own `LOADTEST_KILL_AFTER` scenario needs to
// still clear the `http_req_failed{phase:measure}` rate<0.01 threshold.
let nextBaseUrlIndex = 0;
const unhealthyBaseUrls = new Set();

function pickBaseUrl() {
  const candidates = BASE_URLS.filter((url) => !unhealthyBaseUrls.has(url));
  // Every base URL this VU has ever tried failed (should not happen outside
  // a misconfigured run that kills more replicas than it starts) - fall
  // back to the full list rather than pick from an empty one.
  const pool = candidates.length > 0 ? candidates : BASE_URLS;
  const url = pool[nextBaseUrlIndex % pool.length];
  nextBaseUrlIndex += 1;
  return url;
}

const REQUEST_HEADERS = {
  'Content-Type': 'application/json',
  Accept: 'application/json, text/event-stream',
  'MCP-Protocol-Version': '2025-11-25',
};

let nextRequestId = 1;

// Diagnostics for a red "tool call did not error" check (#108 round 2): logs
// the first few `result.isError` payloads verbatim so a flaky run tells us
// *why* the tool returned an error (e.g. `VersionConflict` text) instead of
// just the check's pass/fail count - capped so a systemic failure does not
// flood k6's console.
const MAX_LOGGED_ERROR_SAMPLES = 8;
let loggedErrorSamples = 0;

function logErrorSample(name, payload) {
  if (loggedErrorSamples >= MAX_LOGGED_ERROR_SAMPLES) {
    return;
  }
  loggedErrorSamples += 1;
  console.log(
    `[loadtest] isError sample ${loggedErrorSamples}/${MAX_LOGGED_ERROR_SAMPLES} ` +
      `for ${name}: ${JSON.stringify(payload.result)}`
  );
}

// Each VU keeps one synthetic principal for the run's whole lifetime - every
// VU exercises one principal end to end (its own token, namespaces and
// read_paths), rather than a fresh random pick per request.
export function vuPrincipal() {
  return TOKENS[(__VU - 1) % TOKENS.length];
}

// One `tools/call` JSON-RPC POST, checked for transport success (HTTP 200,
// a `result`, `result.isError !== true`). Returns `{ok, result}`: `result`
// is `response.result` (its own shape depends on the tool - `toolsCall`
// itself only validates the JSON-RPC envelope), `null` if any check failed.
export function toolsCall(token, name, toolArguments) {
  const url = pickBaseUrl();
  const body = JSON.stringify({
    jsonrpc: '2.0',
    id: nextRequestId++,
    method: 'tools/call',
    params: { name: name, arguments: toolArguments },
  });
  const response = http.post(url, body, {
    headers: Object.assign({ Authorization: `Bearer ${token}` }, REQUEST_HEADERS),
    // `tool` tags every metric this request produces (http_req_duration,
    // http_req_failed, checks, ...) with the MCP tool name - `name` here is
    // always one of memory_search/memory_read/memory_write/memory_edit, so
    // write and edit (both under the `write` scenario) can be told apart in
    // the export (#109, WP-21).
    tags: { tool: name },
  });

  if (response.status === 0) {
    // No HTTP response at all (k6: connection refused/reset/timeout) -
    // `url`'s replica is gone; see `unhealthyBaseUrls`'s own docstring above.
    unhealthyBaseUrls.add(url);
  }

  let payload = null;
  if (response.status === 200) {
    try {
      payload = response.json();
    } catch (_error) {
      payload = null;
    }
  }

  const ok = check(response, {
    'http status is 200': () => response.status === 200,
    'response carries a result': () => payload !== null && payload.result !== undefined,
    'tool call did not error': () =>
      payload !== null && payload.result !== undefined && payload.result.isError !== true,
  });

  if (payload !== null && payload.result !== undefined && payload.result.isError === true) {
    logErrorSample(name, payload);
  }

  return { ok: ok, result: ok ? payload.result : null };
}
