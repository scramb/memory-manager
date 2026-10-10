# SPDX-License-Identifier: AGPL-3.0-only
"""Client compatibility profiles: data, not code paths (ADR-0010).

`profiles.py` holds the known `Profile`s (`default`, `claude-ai`, `claude-code`) as an
immutable registry: a delivery mode for the usage rules plus the client's documented
limits. `select.py` picks a profile for an incoming request (#131, ADR-0010): an
override (`?profile=`, `MM-Client-Profile`, or `serve --stdio --profile`) wins outright,
otherwise the connecting client's `clientInfo.name` is mapped if known, otherwise
`profiles.DEFAULT_PROFILE`. All three delivery modes - `full`, `descriptions` (#132) and
`short` (#306) - are implemented by `mcp/server.py`'s `_ProfileMiddleware`, which is the
one place that acts on `delivery_mode`; no profile registered here uses `descriptions` or
`short` yet. Enforcing that the tool contract fits every profile's limits is the schema
linter, `lint.py` (#133).

`profiles.py` and `select.py` deliberately do not import from `memory_manager.mcp` or
`memory_manager.app`: `mcp/server.py` imports `select.py` to run profile selection as
request middleware, and a reverse import here would make that a cycle. `lint.py` is the
one documented exception - it builds the real server in-process to list its tools, so it
imports `memory_manager.app.Services` and `memory_manager.mcp.server.build_server`
directly; nothing in `mcp` or the rest of `compat` imports `lint.py` back, so this stays a
one-way edge, not a cycle.
"""

from __future__ import annotations

__all__: list[str] = []
