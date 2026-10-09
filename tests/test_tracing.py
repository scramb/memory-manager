# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for OTel tracing end-to-end (#262, #263, WP-31): the HTTP edge's
server span, the MCP tool span, Postgres statement child spans, and a
worker-claimed job's own span, all in one trace, continuing an incoming
W3C `traceparent`.

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

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
import httpx
import httpx2
import pytest
import pytest_asyncio
from mcp import Client as McpClient
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from storage.contract import note_bytes

from memory_manager.app import open_services
from memory_manager.auth.tokens import ALL_NAMESPACES, create_token
from memory_manager.config import ServerConfig
from memory_manager.db.migrate import migrate
from memory_manager.http import HEALTH_PATH, create_app
from memory_manager.index.indexer import Indexer, VaultNotesSource
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE
from memory_manager.observability import tracing
from memory_manager.observability.logging import JsonFormatter
from memory_manager.storage.postgres import PostgresBackend
from memory_manager.vault.ulid import new_ulid
from memory_manager.worker import build_job_handlers, consume_jobs

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


# --- Worker: a job's span continues (or starts) the enqueuing trace --------
#
# Goes through the same chain `tests/worker/test_embeddings.py` already
# proves end to end (a `PostgresBackend` write -> `index_on_connection`'s own
# `jobs.enqueue` -> a real `worker.consume_jobs` loop claiming and running
# the job), not a bare `Indexer.embed_note_job()` call - `_dispatch_job`'s
# `job_span` call is the one thing under test here, and only `consume_jobs`
# exercises it.

_WORKER_POLL_SECONDS = 0.1
_WORKER_WAIT_TIMEOUT_SECONDS = 5.0


@dataclass
class _FakeProvider:
    """A deterministic embedding provider, no network - mirrors
    `tests/worker/test_embeddings.py`'s own `_FakeProvider`.
    """

    model: str = "fake-v1"
    dimension: int = 4
    calls: list[list[str]] = field(default_factory=list)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(text) + i) for i in range(self.dimension)] for text in texts]


@pytest_asyncio.fixture
async def pool(test_database_url: str) -> AsyncIterator[asyncpg.Pool]:
    migration_conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(migration_conn)
    finally:
        await migration_conn.close()

    created_pool = await asyncpg.create_pool(test_database_url, min_size=1, max_size=10)
    try:
        yield created_pool
    finally:
        await created_pool.close()


@dataclass
class _Worker:
    task: asyncio.Task[None]
    stop: asyncio.Event
    listen_conn: asyncpg.Connection

    async def stop_and_join(self, *, timeout: float = _WORKER_WAIT_TIMEOUT_SECONDS) -> None:
        self.stop.set()
        await asyncio.wait_for(self.task, timeout=timeout)
        await self.listen_conn.close()


async def _start_embedding_worker(
    test_database_url: str, pool: asyncpg.Pool, worker_indexer: Indexer
) -> _Worker:
    listen_conn = await asyncpg.connect(test_database_url)
    handlers = build_job_handlers(worker_indexer)
    stop = asyncio.Event()
    task = asyncio.create_task(
        consume_jobs(
            pool,
            listen_conn,
            handlers,
            kinds=tuple(handlers),
            stop=stop,
            poll_seconds=_WORKER_POLL_SECONDS,
        )
    )
    return _Worker(task=task, stop=stop, listen_conn=listen_conn)


async def _wait_until(
    condition: Callable[[], Awaitable[bool]], *, timeout: float = _WORKER_WAIT_TIMEOUT_SECONDS
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if await condition():
            return
        if loop.time() >= deadline:
            pytest.fail("condition was never satisfied in time")
        await asyncio.sleep(0.05)


async def _job_done(pool: asyncpg.Pool, note_id: str) -> bool:
    async with pool.acquire() as conn:
        state = await conn.fetchval(
            "select state from jobs where kind = 'embed_note' and payload->>'note_id' = $1 "
            "order by created_at desc limit 1",
            note_id,
        )
    return bool(state == "done")


async def test_worker_job_span_shares_the_enqueuing_requests_trace_id(
    otel_exporter: InMemorySpanExporter, test_database_url: str, pool: asyncpg.Pool
) -> None:
    """A write's own active span is current while `index_on_connection` enqueues
    the `"embed_note"` job (`current_traceparent()`) - the worker's own
    `job_span` around that claimed job shares that span's trace id.
    """
    tracer = tracing._tracer
    assert tracer is not None

    provider = _FakeProvider()
    api_indexer = Indexer(pool, VaultNotesSource(), provider)
    backend = PostgresBackend(pool, index_hook=api_indexer.index_on_connection)

    note_id = new_ulid()
    content = note_bytes(
        id=note_id, title="Traced Note", body="Some tracedword123 text to embed.\n"
    )
    with tracer.start_as_current_span("request") as request_span:
        request_trace_id = _trace_id_hex(request_span)
        await backend.write("personal/fact/traced.md", content, if_version="new", client="ci")

    worker_indexer = Indexer(pool, VaultNotesSource(), provider)
    worker = await _start_embedding_worker(test_database_url, pool, worker_indexer)
    try:
        await _wait_until(lambda: _job_done(pool, note_id))
    finally:
        await worker.stop_and_join()

    job_spans = [s for s in otel_exporter.get_finished_spans() if s.name == "embed_note process"]
    assert len(job_spans) == 1
    job_span_ = job_spans[0]
    assert job_span_.kind == SpanKind.CONSUMER
    assert _trace_id_hex(job_span_) == request_trace_id
    assert job_span_.attributes is not None
    assert job_span_.attributes["job.kind"] == "embed_note"
    assert job_span_.attributes["job.id"]


async def test_worker_job_without_an_enqueuing_span_starts_a_fresh_trace(
    otel_exporter: InMemorySpanExporter, test_database_url: str, pool: asyncpg.Pool
) -> None:
    """A write with no active span around it (`current_traceparent()` returns
    `None`) leaves `jobs.traceparent` `NULL` - the worker's own `job_span`
    then starts a fresh trace rather than continuing anything.
    """
    provider = _FakeProvider()
    api_indexer = Indexer(pool, VaultNotesSource(), provider)
    backend = PostgresBackend(pool, index_hook=api_indexer.index_on_connection)

    note_id = new_ulid()
    content = note_bytes(
        id=note_id, title="Untraced Note", body="Some untracedword456 text to embed.\n"
    )
    await backend.write("personal/fact/untraced.md", content, if_version="new", client="ci")

    async with pool.acquire() as conn:
        stored_traceparent = await conn.fetchval(
            "select traceparent from jobs where kind = 'embed_note' and payload->>'note_id' = $1",
            note_id,
        )
    assert stored_traceparent is None

    worker_indexer = Indexer(pool, VaultNotesSource(), provider)
    worker = await _start_embedding_worker(test_database_url, pool, worker_indexer)
    try:
        await _wait_until(lambda: _job_done(pool, note_id))
    finally:
        await worker.stop_and_join()

    job_spans = [s for s in otel_exporter.get_finished_spans() if s.name == "embed_note process"]
    assert len(job_spans) == 1
    assert job_spans[0].parent is None


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
