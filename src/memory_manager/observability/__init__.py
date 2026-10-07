# SPDX-License-Identifier: AGPL-3.0-only
"""Metrics, structured logging and optional tracing for memory-manager (#43, WP-12).

`instrument_tool` is the one decorator `mcp/server.py` applies to every
MCP tool: it composes `metrics.track_tool_call` (call count, duration) with
`tracing.trace_tool_call` (an OTel span, a no-op unless configured) into a
single hook per tool, rather than two separate decorators at every
registration site. `metrics`/`logging`/`tracing` are otherwise independent
and can be imported directly (`queue.py`, `vault/git.py`, `search.py`,
`http.py`, `cli.py` do).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from memory_manager.observability.metrics import track_tool_call
from memory_manager.observability.tracing import trace_tool_call

__all__ = ["instrument_tool"]

_ToolFunc = TypeVar("_ToolFunc", bound=Callable[..., Awaitable[Any]])


def instrument_tool(tool: str) -> Callable[[_ToolFunc], _ToolFunc]:
    """Wrap an MCP tool function with both call metrics and an optional trace span."""

    def decorator(func: _ToolFunc) -> _ToolFunc:
        return track_tool_call(tool)(trace_tool_call(tool)(func))

    return decorator
