# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic vault generator for load testing (#107, WP-21).

`loadtest.generate` writes a deterministic, synthetic vault for the k6/
Postgres load test (#108, #109): valid notes (ADR-0005) spread over
personal, group and org namespaces, plus two sidecar files for the loader
and the latency scenarios:

- `<out>/vault/<namespace>/<type>/<slug>.md` - the notes themselves.
- `<out>/namespaces.json` - alias -> `{kind, members}` for every namespace,
  so a loader can recreate group membership without re-deriving it.
- `<out>/queries.jsonl` - `{id, query, expected, namespaces}` entries whose
  `expected` note ids are guaranteed to contain a unique marker term,
  giving the load test known-answer queries.

`loadtest.load` bulk-loads that vault into Postgres (ADR-0007 §2), creates
one static token per sampled synthetic principal, and writes a JSON side
file for k6 - the server's base URL, a sample of known note paths, and the
token list. `loadtest/k6/` (`lib.js`, `search.js`, `read.js`, `write.js`,
`smoke.js`) drives the actual `memory_search`/`memory_read`/`memory_write`/
`memory_edit` calls against a running server, with a p95 latency threshold
per scenario. `make loadtest-smoke` (`scripts/loadtest-smoke.sh`) wires all
three together: generate, load, reindex, serve, run k6.
"""
