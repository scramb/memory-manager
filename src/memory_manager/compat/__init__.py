# SPDX-License-Identifier: AGPL-3.0-only
"""Client compatibility profiles: data, not code paths (ADR-0010).

`profiles.py` holds the known `Profile`s (`default`, `claude-ai`, `claude-code`) as an
immutable registry: a delivery mode for the usage rules plus the client's documented
limits. `select.py` picks a profile for an incoming request (#131, ADR-0010): an
override (`?profile=`, `MM-Client-Profile`, or `serve --stdio --profile`) wins outright,
otherwise the connecting client's `clientInfo.name` is mapped if known, otherwise
`profiles.DEFAULT_PROFILE`; today only the `full` delivery mode is actually deliverable
(`select.require_deliverable`) - `descriptions` is #132, `short` is #306. Enforcing that
the tool contract fits every profile's limits is the schema linter, #133.

Deliberately does not import from `memory_manager.mcp` or `memory_manager.app`:
`mcp/server.py` imports `select.py` to run profile selection as request middleware, and a
reverse import here would make that a cycle.
"""

from __future__ import annotations

__all__: list[str] = []
