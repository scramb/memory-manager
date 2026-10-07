// SPDX-License-Identifier: AGPL-3.0-only
//
// memory_read scenario (#108): one `memory_read` call per iteration, by a
// known vault path sampled by `loadtest.load` (`context.read_paths`) - never
// a ULID, exactly as the task contract requires.

import { READ_PATHS, toolsCall, vuToken } from './lib.js';

export function read() {
  const path = READ_PATHS[Math.floor(Math.random() * READ_PATHS.length)];
  toolsCall(vuToken(), 'memory_read', { items: [path] });
}
