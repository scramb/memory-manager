// SPDX-License-Identifier: AGPL-3.0-only
//
// memory_read scenario (#108, #124): one `memory_read` call per iteration, by
// a known vault path sampled by `loadtest.load` for this VU's own principal
// (`vuPrincipal().read_paths`) - never a ULID, and never a path outside what
// this principal can actually read under RLS.
//
// `memory_read` never returns a transport-level error for a path RLS hides -
// it reports `{item, error: {error: "NotFound", ...}}` for that one item
// instead (`mcp/server.py`'s `_read_items`), so `lib.js`'s own `toolsCall`
// check ("tool call did not error") would stay green even if every read
// silently failed. The `check` below is the canary against exactly that: a
// `read_paths` entry this principal cannot read is a loader bug (#124), not
// something this scenario should let through unnoticed.
import { check } from 'k6';
import { toolsCall, vuPrincipal } from './lib.js';

export function read() {
  const principal = vuPrincipal();
  const paths = principal.read_paths;
  const path = paths[Math.floor(Math.random() * paths.length)];
  const outcome = toolsCall(principal.token, 'memory_read', { items: [path] });
  if (!outcome.ok) {
    return;
  }
  const items = outcome.result.structuredContent.result;
  check(items, {
    'memory_read item carries no error (not silently hidden by RLS)': (value) =>
      Array.isArray(value) && value.length === 1 && value[0].error === undefined,
  });
}
