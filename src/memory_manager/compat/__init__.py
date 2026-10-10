# SPDX-License-Identifier: AGPL-3.0-only
"""Client compatibility profiles: data, not code paths (ADR-0010).

`profiles.py` holds the known `Profile`s (`default`, `claude-ai`, `claude-code`) as an
immutable registry: a delivery mode for the usage rules plus the client's documented
limits. Picking a profile for an incoming request - override, `clientInfo`, or the
`default` fallback - is #131, out of scope here. Enforcing that the tool contract fits
every profile's limits is the schema linter, #133.

Deliberately does not import from `memory_manager.mcp` or `memory_manager.app`: profile
selection (#131) will need to import this module from `mcp/server.py`, and a reverse
import here would make that a cycle.
"""

from __future__ import annotations

__all__: list[str] = []
