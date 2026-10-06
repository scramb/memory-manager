# SPDX-License-Identifier: AGPL-3.0-only
"""Optional OpenTelemetry tracing around MCP tool calls (#43, WP-12).

Off by default, on two conditions both having to hold: `OTEL_EXPORTER_OTLP_ENDPOINT`
is set in the environment, and the `otel` extra (`opentelemetry-sdk`,
`opentelemetry-exporter-otlp`) is installed - neither is true by default
(CLAUDE.md: few dependencies, every external service optional and
pluggable). `trace_tool_call(tool)` is the one decorator `observability.
instrument_tool` composes with `metrics.track_tool_call`; it lazily
configures the SDK on its very first call (not at import time, and not
via a separate startup hook `cli.py` would otherwise have to carry) and is
a plain no-op - the wrapped function runs exactly as it would unwrapped -
whenever the endpoint is unset or the SDK import fails.
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

__all__ = ["trace_tool_call"]

_logger = logging.getLogger(__name__)

_OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
_SERVICE_NAME = "memory-manager"

_ToolFunc = TypeVar("_ToolFunc", bound=Callable[..., Awaitable[Any]])

_configured = False
_tracer: Tracer | None = None


def _ensure_configured() -> None:
    """Set up an OTLP tracer provider on first use; a no-op every time after.

    Reads `OTEL_EXPORTER_OTLP_ENDPOINT` fresh each process (it never
    changes mid-process), so there is nothing to invalidate once set - the
    `_configured` guard just makes this idempotent, not a cache a test
    needs to reset.
    """
    global _configured, _tracer
    if _configured:
        return
    _configured = True

    endpoint = os.environ.get(_OTLP_ENDPOINT_ENV)
    if not endpoint:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        _logger.warning(
            "%s is set but the 'otel' extra is not installed - tracing stays off "
            "(install memory-manager[otel] to enable it)",
            _OTLP_ENDPOINT_ENV,
        )
        return

    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: _SERVICE_NAME}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(_SERVICE_NAME)


def trace_tool_call(tool: str) -> Callable[[_ToolFunc], _ToolFunc]:
    """Decorate an async MCP tool function with one OTel span named `tool`.

    A no-op decorator (the wrapped function runs, nothing else happens)
    until `_ensure_configured` finds both the endpoint and the SDK - see
    the module docstring. Uses `functools.wraps` for the same reason
    `metrics.track_tool_call` does: the SDK's schema generation and
    `Context` injection must still see the original signature.
    """

    def decorator(func: _ToolFunc) -> _ToolFunc:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            _ensure_configured()
            if _tracer is None:
                return await func(*args, **kwargs)
            with _tracer.start_as_current_span(tool):
                return await func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
