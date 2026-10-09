# SPDX-License-Identifier: AGPL-3.0-only
"""Optional OpenTelemetry tracing from the HTTP edge to MCP tool calls and
Postgres statements (#43 WP-12, #262 WP-31).

Off by default, on two conditions both having to hold: `OTEL_EXPORTER_OTLP_ENDPOINT`
is set in the environment, and the `otel` extra (`opentelemetry-sdk`,
`opentelemetry-exporter-otlp`) is installed - neither is true by default
(CLAUDE.md: few dependencies, every external service optional and
pluggable). `_active_tracer()` lazily configures the SDK on its very first
call (not at import time, and not via a separate startup hook `cli.py`
would otherwise have to carry) and is the one seam the three pieces below
share, each a plain no-op - the wrapped code runs exactly as it would
unwrapped - whenever the endpoint is unset or the SDK import fails:

- `trace_tool_call(tool)`: one span per MCP tool call, the decorator
  `observability.instrument_tool` composes with `metrics.track_tool_call`.
- `TracingMiddleware`: one SERVER span per HTTP request (`http.py`),
  continuing an incoming W3C `traceparent`/`tracestate`
  (`opentelemetry.propagate.extract`, the same extractor the installed MCP
  SDK's own `mcp.shared._otel.extract_trace_context` uses) rather than
  starting a fresh trace for every request.
- `db_span(op)`: one child span around a Postgres statement (or small,
  fixed group of them) - `db/rls.py`'s `request_identity` and `search.py`'s
  query functions are its only callers (#262). Carries `db.system`/
  `db.operation` only, never SQL text or bound parameters (CLAUDE.md: note
  content is data, never a command or a log/trace payload - the same rule
  extends to query parameters here).
- `current_traceparent()`/`job_span(kind, ...)`: the `jobs` outbox's own pair
  (#263 WP-31). `index/indexer.py`'s `Indexer.index_on_connection` calls
  `current_traceparent()` right before `jobs.enqueue`, so the row carries the
  enqueuing request's own `traceparent` (or `None` when no span is active -
  `enqueue_stale_embeddings`' own worker-startup catch-up, which never runs
  inside a request). `worker.py`'s `_dispatch_job` wraps every claimed job in
  `job_span`, a CONSUMER span continuing that stored `traceparent` when
  present, or starting a fresh trace when it is `None` - the messaging
  counterpart to `TracingMiddleware`'s incoming-`traceparent` continuation
  above, `opentelemetry.propagate.extract`/`inject` on a plain
  `{"traceparent": ...}` carrier rather than real HTTP headers.

Span nesting across all three follows the ambient OTel context
(`contextvars`, not a value threaded through every call): as long as a
tool call and the Postgres statements it makes run in the same `asyncio`
task as the request's own `TracingMiddleware` span, `start_as_current_span`
makes each one a child of whichever span is already current - verified by
reading the installed SDK (`opentelemetry-sdk` 1.45.1) directly rather than
assumed.
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, TypeVar

from starlette.datastructures import Headers

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["TracingMiddleware", "current_traceparent", "db_span", "job_span", "trace_tool_call"]

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


def _active_tracer() -> Tracer | None:
    """`_tracer` once `_ensure_configured` has run - `None` under the same
    conditions `trace_tool_call`'s own docstring describes. The one call every
    piece of this module makes before touching the SDK at all."""
    _ensure_configured()
    return _tracer


def trace_tool_call(tool: str) -> Callable[[_ToolFunc], _ToolFunc]:
    """Decorate an async MCP tool function with one OTel span named `tool`.

    A no-op decorator (the wrapped function runs, nothing else happens)
    until `_active_tracer()` finds both the endpoint and the SDK - see
    the module docstring. Uses `functools.wraps` for the same reason
    `metrics.track_tool_call` does: the SDK's schema generation and
    `Context` injection must still see the original signature.
    """

    def decorator(func: _ToolFunc) -> _ToolFunc:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = _active_tracer()
            if tracer is None:
                return await func(*args, **kwargs)
            with tracer.start_as_current_span(tool):
                return await func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


@contextmanager
def db_span(op: str) -> Iterator[None]:
    """Wrap one Postgres statement (or one small, fixed group of them) in an
    OTel span named `op`, a child of whatever span is already current (module
    docstring) - a no-op context manager under the same conditions
    `trace_tool_call` is (#262).

    `op` is a short, fixed operation name (e.g. `"fulltext_search"`,
    `"rls.set_identity"`) - never SQL text, never a table or column name
    taken from caller input. Attributes carry only `db.system` (always
    `"postgresql"`, the only backend this spans) and `db.operation` (`op`
    again, as its own attribute rather than only the span name, so a query
    across many traces does not need to parse span names) - no bound
    parameters, ever (module docstring).
    """
    tracer = _active_tracer()
    if tracer is None:
        yield
        return
    with tracer.start_as_current_span(
        op, attributes={"db.system": "postgresql", "db.operation": op}
    ):
        yield


def current_traceparent() -> str | None:
    """The currently active span's W3C `traceparent`, or `None` under the same
    conditions `trace_tool_call`'s docstring describes, or simply because no
    span is active right now (#263 WP-31).

    `opentelemetry.propagate.inject` into a fresh carrier leaves the
    `"traceparent"` key out entirely when there is no active span to encode
    (verified by reading the installed SDK, same as the module docstring's
    other claims) - so this needs no separate "is a span active" check of its
    own beyond `_active_tracer()`'s existing one. `index/indexer.py`'s
    `Indexer.index_on_connection` is this function's one caller, right before
    `jobs.enqueue`, so a job row carries the enqueuing request's own trace
    (or `None`, e.g. the worker's own startup catch-up, which runs with no
    span active at all).
    """
    tracer = _active_tracer()
    if tracer is None:
        return None

    from opentelemetry import propagate

    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent")


@contextmanager
def job_span(kind: str, *, job_id: str, attempt: int, traceparent: str | None) -> Iterator[None]:
    """Wrap one worker-claimed `jobs` row in a CONSUMER span (#263 WP-31) - a
    no-op context manager under the same conditions `trace_tool_call` is
    (module docstring). `worker.py`'s `_dispatch_job` is this function's one
    caller, around the whole claimed job (handler call plus the
    `jobs.complete`/`fail`/`fail_or_retry` that follows it).

    Continues `traceparent` (`jobs.ClaimedJob.traceparent`, the column
    `current_traceparent()` filled at enqueue time) via
    `opentelemetry.propagate.extract` on a plain `{"traceparent": ...}`
    carrier - the same extractor `TracingMiddleware` already uses on real
    HTTP headers - when it is not `None`; a job enqueued with no span active
    (`traceparent is None`) instead starts a fresh trace, the ambient-context
    default `start_as_current_span` already falls back to when no `context`
    is passed in.

    Span name and attributes follow the OTel messaging semantic
    conventions (`opentelemetry-semantic-conventions` 0.66b1, the version
    pinned transitively by `otel`'s own `opentelemetry-sdk>=1.45.1`, read
    directly rather than assumed - `SpanAttributes.MESSAGING_OPERATION` etc.
    resolve to the plain attribute names used here) for a "process" span:
    `"{kind} process"`, `messaging.system` (`"postgresql"` - the `jobs`
    outbox has no separate broker), `messaging.destination.name` (`kind`),
    `messaging.operation` (`"process"`). `job.kind`/`job.id`/`job.attempt`
    are this project's own addition, on top of those - IDs and a count only,
    never a job's own `payload` (CLAUDE.md: note content is data, never a
    log/trace payload; `jobs.py`'s own module docstring: payloads carry IDs
    only in the first place).
    """
    tracer = _active_tracer()
    if tracer is None:
        yield
        return

    from opentelemetry import propagate
    from opentelemetry.trace import SpanKind

    context = propagate.extract({"traceparent": traceparent}) if traceparent else None
    with tracer.start_as_current_span(
        f"{kind} process",
        context=context,
        kind=SpanKind.CONSUMER,
        attributes={
            "messaging.system": "postgresql",
            "messaging.destination.name": kind,
            "messaging.operation": "process",
            "job.kind": kind,
            "job.id": job_id,
            "job.attempt": attempt,
        },
    ):
        yield


class TracingMiddleware:
    """Pure ASGI middleware: one OTel SERVER span per HTTP request (#262).

    A no-op middleware (`self._app` runs, nothing else happens) under the
    same conditions `trace_tool_call` is - built unconditionally by
    `http.py`, since whether it does anything depends on the environment,
    not on how the app is wired.

    Continues an incoming W3C `traceparent`/`tracestate` via
    `opentelemetry.propagate.extract` on the request headers - Starlette's
    `Headers` is a case-insensitive `Mapping[str, str]`, exactly the carrier
    shape the default `TraceContextTextMapPropagator` getter reads
    (`carrier.get("traceparent")`, verified by reading the installed SDK;
    module docstring). A request with no such header, or an invalid one,
    starts a fresh trace instead - `extract` always returns a usable
    `Context`, never raises, for either case.

    The span name is the request's path: every route this server exposes
    (`http.py`'s route table) is a fixed path, never a `{param}` template,
    so there is no separate "route template" to compute - `scope["path"]`
    already is one. `http.method`/`http.route` are set going in;
    `http.response.status_code` once the response actually starts, and a
    5xx or an exception propagating out of `self._app` both mark the span
    as an error (an exception additionally records it) - neither attribute
    nor status ever carries a header value, a query string or a body.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        tracer = _active_tracer()
        if tracer is None:
            await self._app(scope, receive, send)
            return

        from opentelemetry import propagate
        from opentelemetry.trace import SpanKind, StatusCode

        path = scope["path"]
        context = propagate.extract(Headers(scope=scope))
        status_codes: list[int] = []

        async def send_with_status(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_codes.append(message["status"])
            await send(message)

        with tracer.start_as_current_span(
            path,
            context=context,
            kind=SpanKind.SERVER,
            attributes={"http.method": scope.get("method", ""), "http.route": path},
        ) as span:
            try:
                await self._app(scope, receive, send_with_status)
            except Exception as exc:
                span.set_attribute("error.type", type(exc).__qualname__)
                span.record_exception(exc)
                span.set_status(StatusCode.ERROR, str(exc))
                raise
            if status_codes:
                status = status_codes[0]
                span.set_attribute("http.response.status_code", status)
                if status >= 500:
                    span.set_status(StatusCode.ERROR)
