// SPDX-License-Identifier: AGPL-3.0-only
//
// Shared k6 helpers for the loadtest scenarios (#108, #124): one `tools/call`
// JSON-RPC POST against the MCP endpoint, stateless - no prior `initialize`,
// the server's stateless Streamable HTTP transport serves a bare `tools/call`
// anyway (ADR-0009 §1, pinned server-side by `tests/test_stateless_transport.
// py`) - plus the context side file `loadtest/load.py` writes: the server's
// base URL and one synthetic principal per static token, each carrying its
// own `alias` (its generator namespace alias), `namespaces` (its full
// membership, for bookkeeping) and `read_paths` (a sample of its own
// `me/...`/`org/...` paths - `memory_read` must never be driven by a ULID,
// only a vault-relative path, and never a path this principal cannot
// actually read under RLS).
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

export const BASE_URL = context.base_url;

export const TOKENS = new SharedArray('loadtest-tokens', function () {
  return context.tokens;
});

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
  const body = JSON.stringify({
    jsonrpc: '2.0',
    id: nextRequestId++,
    method: 'tools/call',
    params: { name: name, arguments: toolArguments },
  });
  const response = http.post(BASE_URL, body, {
    headers: Object.assign({ Authorization: `Bearer ${token}` }, REQUEST_HEADERS),
    // `tool` tags every metric this request produces (http_req_duration,
    // http_req_failed, checks, ...) with the MCP tool name - `name` here is
    // always one of memory_search/memory_read/memory_write/memory_edit, so
    // write and edit (both under the `write` scenario) can be told apart in
    // the export (#109, WP-21).
    tags: { tool: name },
  });

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
