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

Nothing here loads a database or runs k6 (#108) - this module only
produces the on-disk vault and its sidecars.
"""
