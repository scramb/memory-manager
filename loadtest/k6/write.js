// SPDX-License-Identifier: AGPL-3.0-only
//
// memory_write + memory_edit scenario (#108): each iteration creates a new
// note in a dedicated namespace (`loadtest-writes`, never one of the
// generator's synthetic namespaces) and edits it once, so the scenario
// measures both write-tool call shapes end to end. The path is keyed by
// `RUN_ID`-`__VU`-`__ITER`, so two runs - or two iterations of the same VU -
// never collide on `if_version: 'new'`.
//
// Round 2 (#108): `RUN_ID` is `Date.now()` - a bare 13-digit number - and
// used to go straight into the note's `title` frontmatter value. The
// `credit-card` secret-scan rule (`vault/secrets_rules.toml`) matches any
// 13-19 digit run and Luhn-validates it; about one in ten random 13-digit
// values passes that check, so roughly one in ten writes got rejected as
// `SecretRejected` under load - a real server behaviour, not a k6 flake
// (confirmed by `lib.js`'s error-sample logging). `RUN_ID` stays in the
// *path* only (paths are never secret-scanned) - the written content below
// never repeats it, so it can never trip the rule.

import { toolsCall, vuToken } from './lib.js';

const RUN_ID = __ENV.MM_RUN_ID || `${Date.now()}`;

const BODY = 'lumen vantrix obelisk cobalt ember.\n';
const OLD_STR = 'lumen vantrix obelisk cobalt ember.';
const NEW_STR = 'lumen vantrix obelisk cobalt ember edited.';

export function write() {
  const slug = `${RUN_ID}-${__VU}-${__ITER}`;
  const path = `loadtest-writes/fact/${slug}.md`;
  const content =
    '---\n' +
    'title: Load test write\n' +
    'description: Synthetic load-test note, safe to discard.\n' +
    'type: fact\n' +
    '---\n' +
    BODY;

  const token = vuToken();
  const created = toolsCall(token, 'memory_write', {
    path: path,
    content: content,
    if_version: 'new',
  });
  if (!created.ok || created.result.structuredContent === undefined) {
    return;
  }

  toolsCall(token, 'memory_edit', {
    path: path,
    old_str: OLD_STR,
    new_str: NEW_STR,
    if_version: created.result.structuredContent.version,
  });
}
