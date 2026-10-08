# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for OTel tracing end-to-end (#262, WP-31): the HTTP edge's server
span, the MCP tool span, and Postgres statement child spans in one trace,
continuing an incoming W3C `traceparent`.

Skips the whole module when the `otel` extra is not installed (the
`try`/`except ImportError` below, `pytestmark`) - `uv run --extra otel
pytest tests/test_tracing.py` (this task's Definition of Done) is where
every test here actually runs; plain `make check` (no extra) must stay
green too, the same no-op contract `tracing.py`'s own module docstring
describes, just exercised at module-collection granularity instead of
per-test.

Builds its own `TracerProvider` with a `SimpleSpanProcessor` (synchronous
export, no batching delay) feeding an `InMemorySpanExporter`, and installs
it directly on `memory_manager.observability.tracing`'s module globals -
the same seam `tests/test_observability.py`'s own
`test_tracing_is_a_no_op_without_the_otel_endpoint_configured` resets to
force the no-op branch; the `otel_exporter` fixture here is its mirror
image, forcing a real tracer instead. No `OTEL_EXPORTER_OTLP_ENDPOINT` is
ever set and no OTLP network call is ever made - `_ensure_configured`'s own
real path is bypassed entirely by setting `_configured=True` up front.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import httpx2
import pytest
from mcp import Client as McpClient
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from memory_manager.app import open_services
from memory_manager.auth.tokens import ALL_NAMESPACES, create_token
from memory_manager.config import ServerConfig
from memory_manager.http import HEALTH_PATH, create_app
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE
from memory_manager.observability import tracing
from memory_manager.observability.logging import JsonFormatter

try:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import SpanKind
except ImportError:
    _OTEL_INSTALLED = False
else:
    _OTEL_INSTALLED = True

# Skips every test below, cleanly, the moment the `otel` extra is missing
# (module docstring) - rather than letting the `try` above fail collection
# of the whole module outright.
pytestmark = pytest.mark.skipif(not _OTEL_INSTALLED, reason="the 'otel' extra is not installed")

_NOTE_PATH = "personal/fact/a.md"
_PUBLIC_URL = "https://mm.example.test"
_NOTE_MARKER = "wp31-tracing-note-body-marker-1a2b3c"

# A syntactically valid W3C `traceparent` (version `00`, sampled) with a
# fixed trace/span id, taken straight from the W3C Trace Context spec's own
# example - never a real production trace.
_INCOMING_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
_INCOMING_SPAN_ID = "00f067aa0ba902b7"
_INCOMING_TRACEPARENT = f"00-{_INCOMING_TRACE_ID}-{_INCOMING_SPAN_ID}-01"


def _note_content(body: str = f"{_NOTE_MARKER}\n") -> str:
    return f"---\ntitle: A\ndescription: B\ntype: fact\n---\n{body}"


def _environ(bare_remote: Path, tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"VAULT_REMOTE": str(bare_remote), "VAULT_DIR": str(tmp_path / "vault"), **extra}


@asynccontextmanager
async def _running_app(environ: dict[str, str], config: ServerConfig) -> AsyncIterator[Starlette]:
    app = create_app(lambda: open_services(environ), config)
    async with app.router.lifespan_context(app):
        yield app


@asynccontextmanager
async def _mcp_client(
    app: Starlette, config: ServerConfig, *, token: str | None = None
) -> AsyncGenerator[McpClient, None]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    http_client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver", headers=headers
    )
    transport = streamable_http_client(
        f"http://testserver{config.mcp_path}", http_client=http_client
    )
    async with McpClient(transport, mode="legacy") as client:
        yield client


@pytest.fixture
def otel_exporter(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """A real OTel tracer, backed by an in-memory exporter, installed on
    `tracing`'s module globals for the duration of one test (module docstring).
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("memory-manager-test")
    monkeypatch.setattr(tracing, "_configured", True)
    monkeypatch.setattr(tracing, "_tracer", tracer)
    yield exporter
    exporter.clear()


def _trace_id_hex(span: object) -> str:
    return format(span.context.trace_id, "032x")  # type: ignore[attr-defined]


# --- db_span --------------------------------------------------------------


def test_db_span_is_a_no_op_without_a_tracer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_configured", True)
    monkeypatch.setattr(tracing, "_tracer", None)

    with tracing.db_span("noop"):
        pass


def test_db_span_records_operation_without_sql_or_parameters(
    otel_exporter: InMemorySpanExporter,
) -> None:
    with tracing.db_span("fulltext_search"):
        pass

    spans = otel_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "fulltext_search"
    assert spans[0].attributes is not None
    assert spans[0].attributes["db.system"] == "postgresql"
    assert spans[0].attributes["db.operation"] == "fulltext_search"


# --- TracingMiddleware -----------------------------------------------------


async def test_tracing_middleware_is_a_no_op_without_a_tracer(
    monkeypatch: pytest.MonkeyPatch, bare_remote: Path, tmp_path: Path
) -> None:
    monkeypatch.setattr(tracing, "_configured", True)
    monkeypatch.setattr(tracing, "_tracer", None)

    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path), config) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.get(HEALTH_PATH)

    assert response.status_code == 200


async def test_server_span_continues_an_incoming_traceparent(
    otel_exporter: InMemorySpanExporter, bare_remote: Path, tmp_path: Path
) -> None:
    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path), config) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.get(HEALTH_PATH, headers={"traceparent": _INCOMING_TRACEPARENT})

    assert response.status_code == 200
    server_spans = [s for s in otel_exporter.get_finished_spans() if s.name == HEALTH_PATH]
    assert len(server_spans) == 1
    span = server_spans[0]
    assert _trace_id_hex(span) == _INCOMING_TRACE_ID
    assert span.parent is not None
    assert format(span.parent.span_id, "016x") == _INCOMING_SPAN_ID
    assert span.attributes is not None
    assert span.attributes["http.response.status_code"] == 200


async def test_server_span_starts_a_fresh_trace_without_an_incoming_traceparent(
    otel_exporter: InMemorySpanExporter, bare_remote: Path, tmp_path: Path
) -> None:
    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path), config) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.get(HEALTH_PATH)

    assert response.status_code == 200
    server_spans = [s for s in otel_exporter.get_finished_spans() if s.name == HEALTH_PATH]
    assert len(server_spans) == 1
    assert server_spans[0].parent is None


# --- One trace: request -> tool -> DB statements ---------------------------


async def test_one_trace_spans_the_request_the_tool_call_and_its_db_statements(
    otel_exporter: InMemorySpanExporter,
    bare_remote: Path,
    tmp_path: Path,
    test_database_url: str,
) -> None:
    config = ServerConfig(public_url=_PUBLIC_URL)
    environ = _environ(bare_remote, tmp_path, DATABASE_URL=test_database_url)

    async with _running_app(environ, config) as app:
        import asyncpg

        pool = await asyncpg.create_pool(test_database_url)
        try:
            plaintext, _info = await create_token(
                pool,
                "tracing-test",
                scopes=[READ_SCOPE, WRITE_SCOPE],
                namespaces=[ALL_NAMESPACES],
            )
        finally:
            await pool.close()

        async with _mcp_client(app, config, token=plaintext) as client:
            write_result = await client.call_tool(
                "memory_write",
                {"path": _NOTE_PATH, "content": _note_content(), "if_version": "new"},
            )
            assert write_result.is_error is False

            otel_exporter.clear()
            search_result = await client.call_tool("memory_search", {"query": _NOTE_MARKER})

    assert search_result.is_error is False
    assert search_result.structured_content is not None
    assert search_result.structured_content["mode"] == "fulltext"

    spans = otel_exporter.get_finished_spans()

    tool_spans = [s for s in spans if s.name == "memory_search"]
    assert len(tool_spans) == 1
    tool_span = tool_spans[0]
    tool_trace_id = _trace_id_hex(tool_span)

    db_spans = [s for s in spans if s.name == "fulltext_search"]
    assert db_spans
    assert all(_trace_id_hex(s) == tool_trace_id for s in db_spans)

    server_spans = [
        s for s in spans if s.kind == SpanKind.SERVER and _trace_id_hex(s) == tool_trace_id
    ]
    assert len(server_spans) == 1
    assert server_spans[0].name == config.mcp_path

    # No note content, and no bearer token, in any span's name or attributes.
    serialized = " ".join(
        [s.name for s in spans]
        + [str(value) for s in spans if s.attributes for value in s.attributes.values()]
    )
    assert _NOTE_MARKER not in serialized
    assert plaintext not in serialized


# --- Log/trace correlation (observability/logging.py) ----------------------


def test_json_log_line_carries_trace_and_span_id_while_a_span_is_active(
    otel_exporter: InMemorySpanExporter,
) -> None:
    import logging

    tracer = tracing._tracer
    assert tracer is not None

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("tests.test_tracing.correlation")
    logger.addHandler(_Capture())
    logger.setLevel(logging.INFO)
    try:
        # `JsonFormatter.format` reads the *currently active* span
        # (`_trace_context_fields`'s own docstring) - called here, inside
        # the `with` block, the same way a real handler's `emit()` runs
        # synchronously during the `logger.info` call that triggered it,
        # not once the span that was active at log time has already ended.
        with tracer.start_as_current_span("log-correlation-span") as span:
            logger.info("hello")
            assert len(records) == 1
            payload = JsonFormatter().format(records[0])
            expected_trace_id = format(span.get_span_context().trace_id, "032x")
            expected_span_id = format(span.get_span_context().span_id, "016x")
    finally:
        logger.handlers.clear()

    import json

    parsed = json.loads(payload)
    assert parsed["trace_id"] == expected_trace_id
    assert parsed["span_id"] == expected_span_id


def test_json_log_line_has_no_trace_id_outside_a_span(
    otel_exporter: InMemorySpanExporter,
) -> None:
    import json
    import logging

    record = logging.LogRecord(
        name="tests.test_tracing.correlation",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="hello",
        args=(),
        exc_info=None,
    )
    parsed = json.loads(JsonFormatter().format(record))

    assert "trace_id" not in parsed
    assert "span_id" not in parsed
