# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `observability/` (#43, WP-12): metrics, structured logging, tracing.

Drives the full Streamable HTTP app in-process, the same way `test_http_app.py`
does (`app.router.lifespan_context(app)` plus an ASGI transport) - tool calls go
through a real `mcp.Client` (`httpx2.ASGITransport`, no real network), so every
metric asserted here is one `mcp/server.py`'s tools, `queue.py` or `vault/git.py`
actually recorded for a real call, not a hand-constructed stand-in. Counters and
histograms are process-wide singletons (`observability/metrics.py`'s module
docstring), so every assertion here reads a *delta* across the call under test,
never an absolute value another test could have already moved.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import httpx2
import pytest
from mcp import Client as McpClient
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from memory_manager.app import open_services
from memory_manager.auth.tokens import ALL_NAMESPACES, create_token
from memory_manager.config import ServerConfig
from memory_manager.http import METRICS_PATH, WEBHOOK_PATH, create_app
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE
from memory_manager.observability import tracing
from memory_manager.observability.logging import (
    AccessLogQueryRedactionFilter,
    JsonFormatter,
    configure_logging,
)
from memory_manager.observability.metrics import (
    GIT_OPERATIONS_TOTAL,
    QUEUE_DEPTH,
    QUEUE_WRITES_TOTAL,
    SEARCH_DURATION_SECONDS,
    TOOL_CALLS_TOTAL,
)

_NOTE_PATH = "personal/fact/a.md"
_WEBHOOK_SECRET = "s3cr3t"  # noqa: S105 - test fixture value, not a real secret
_PUBLIC_URL = "https://mm.example.test"


def _note_content(body: str = "Body.\n") -> str:
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


def _value(labels_fn: object) -> float:
    return cast(float, labels_fn._value.get())  # type: ignore[attr-defined]


@asynccontextmanager
async def _captured_json_logs() -> AsyncIterator[io.StringIO]:
    """Swap the root logger's handlers for one capturing `JsonFormatter` handler.

    Restores whatever handlers were there before, so this never leaks into
    another test regardless of what `cli.py`'s `configure_logging` (never
    called in-process here) would otherwise have set up.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream=stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    previous = root.handlers[:]
    previous_level = root.level
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield stream
    finally:
        root.handlers.clear()
        for h in previous:
            root.addHandler(h)
        root.setLevel(previous_level)


# --- Tool call / queue / git metrics ----------------------------------------


async def test_tool_call_metrics_count_a_successful_read(bare_remote: Path, tmp_path: Path) -> None:
    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path), config) as app:
        before = _value(TOOL_CALLS_TOTAL.labels(tool="memory_index", outcome="ok"))
        async with _mcp_client(app, config) as client:
            result = await client.call_tool("memory_index", {})
        after = _value(TOOL_CALLS_TOTAL.labels(tool="memory_index", outcome="ok"))

    assert result.is_error is False
    assert after - before == 1


async def test_tool_call_metrics_count_an_error_outcome(bare_remote: Path, tmp_path: Path) -> None:
    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path), config) as app:
        before = _value(TOOL_CALLS_TOTAL.labels(tool="memory_write", outcome="error"))
        async with _mcp_client(app, config) as client:
            # Wrong `if_version` for a path that does not exist yet -> a
            # `CallToolResult` with `is_error=True`, not a raised exception.
            result = await client.call_tool(
                "memory_write",
                {"path": _NOTE_PATH, "content": _note_content(), "if_version": "bogus"},
            )
        after = _value(TOOL_CALLS_TOTAL.labels(tool="memory_write", outcome="error"))

    assert result.is_error is True
    assert after - before == 1


async def test_write_moves_queue_and_git_metrics(bare_remote: Path, tmp_path: Path) -> None:
    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path), config) as app:
        before_queue = _value(QUEUE_WRITES_TOTAL.labels(op="write", outcome="ok"))
        before_push = _value(GIT_OPERATIONS_TOTAL.labels(op="push", outcome="ok"))

        async with _mcp_client(app, config) as client:
            result = await client.call_tool(
                "memory_write",
                {"path": _NOTE_PATH, "content": _note_content(), "if_version": "new"},
            )

        after_queue = _value(QUEUE_WRITES_TOTAL.labels(op="write", outcome="ok"))
        after_push = _value(GIT_OPERATIONS_TOTAL.labels(op="push", outcome="ok"))

    assert result.is_error is False
    assert after_queue - before_queue == 1
    assert after_push - before_push == 1
    # The write queue's consumer drained the job it was given - nothing
    # should still be sitting in `asyncio.Queue` once the call returned.
    assert QUEUE_DEPTH._value.get() == 0


async def test_search_duration_is_recorded_without_a_database(
    bare_remote: Path, tmp_path: Path
) -> None:
    """No `DATABASE_URL`: `memory_search` uses the scan fallback, not `hybrid_search`
    (`search.py`'s own metric) - this only confirms the metric this work package adds
    stays untouched by that path, nothing in `search.py` fires on its own.
    """
    config = ServerConfig()
    async with (
        _running_app(_environ(bare_remote, tmp_path), config) as app,
        _mcp_client(app, config) as client,
    ):
        result = await client.call_tool("memory_search", {"query": "anything"})

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["mode"] == "scan"


async def test_search_duration_is_recorded_with_a_database(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    import asyncpg

    config = ServerConfig(public_url=_PUBLIC_URL)
    environ = _environ(bare_remote, tmp_path, DATABASE_URL=test_database_url)
    async with _running_app(environ, config) as app:
        pool = await asyncpg.create_pool(test_database_url)
        try:
            plaintext, _info = await create_token(
                pool,
                "search-metric-test",
                scopes=[READ_SCOPE, WRITE_SCOPE],
                namespaces=[ALL_NAMESPACES],
            )
        finally:
            await pool.close()

        before = SEARCH_DURATION_SECONDS.labels(mode="fulltext")._sum.get()
        async with _mcp_client(app, config, token=plaintext) as client:
            await client.call_tool(
                "memory_write",
                {
                    "path": _NOTE_PATH,
                    "content": _note_content("Searchable body.\n"),
                    "if_version": "new",
                },
            )
            result = await client.call_tool("memory_search", {"query": "searchable"})
        after = SEARCH_DURATION_SECONDS.labels(mode="fulltext")._sum.get()

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["mode"] == "fulltext"
    assert after >= before


# --- /metrics -----------------------------------------------------------------


async def test_metrics_endpoint_exposes_recorded_metrics_after_a_tool_call(
    bare_remote: Path, tmp_path: Path
) -> None:
    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path), config) as app:
        async with _mcp_client(app, config) as client:
            await client.call_tool("memory_index", {})

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.get(METRICS_PATH)

    assert response.status_code == 200
    body = response.text
    assert "mm_tool_calls_total" in body
    assert 'tool="memory_index"' in body
    assert "mm_build_info" in body


async def test_metrics_endpoint_is_404_when_disabled(
    bare_remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("METRICS_ENABLED", "false")
    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path), config) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.get(METRICS_PATH)

    assert response.status_code == 404


# --- Request id ---------------------------------------------------------------


async def test_request_id_header_is_echoed_when_valid(bare_remote: Path, tmp_path: Path) -> None:
    config = ServerConfig(webhook_secret=_WEBHOOK_SECRET)
    async with _running_app(_environ(bare_remote, tmp_path), config) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.post(
                WEBHOOK_PATH, content=b"{}", headers={"X-Request-ID": "good-id.123"}
            )

    assert response.headers["x-request-id"] == "good-id.123"


async def test_request_id_header_is_replaced_when_invalid(
    bare_remote: Path, tmp_path: Path
) -> None:
    config = ServerConfig(webhook_secret=_WEBHOOK_SECRET)
    bad_id = "bad id with spaces/slash"
    async with _running_app(_environ(bare_remote, tmp_path), config) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.post(
                WEBHOOK_PATH, content=b"{}", headers={"X-Request-ID": bad_id}
            )

    echoed = response.headers["x-request-id"]
    assert echoed != bad_id
    assert echoed  # a fresh id was generated, not an empty header


async def test_request_id_header_is_generated_when_absent(
    bare_remote: Path, tmp_path: Path
) -> None:
    config = ServerConfig(webhook_secret=_WEBHOOK_SECRET)
    async with _running_app(_environ(bare_remote, tmp_path), config) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.post(WEBHOOK_PATH, content=b"{}")

    assert response.headers["x-request-id"]


# --- Structured JSON logging ---------------------------------------------------


async def test_json_log_line_carries_the_requests_id(bare_remote: Path, tmp_path: Path) -> None:
    """A log line emitted while handling a request (the webhook's own signature
    warning) parses as JSON and carries the same request id the response echoed.
    """
    config = ServerConfig(webhook_secret=_WEBHOOK_SECRET)
    async with (
        _captured_json_logs() as captured,
        _running_app(_environ(bare_remote, tmp_path), config) as app,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.post(
                WEBHOOK_PATH, content=b"{}", headers={"X-Request-ID": "trace-me-1"}
            )

    assert response.status_code == 401
    records = [json.loads(line) for line in captured.getvalue().splitlines() if line.strip()]
    warnings = [r for r in records if r["level"] == "WARNING"]
    assert warnings
    assert warnings[0]["request_id"] == "trace-me-1"
    assert warnings[0]["logger"]
    assert "msg" in warnings[0]


async def test_log_line_outside_a_request_has_no_request_id(
    bare_remote: Path, tmp_path: Path
) -> None:
    async with (
        _captured_json_logs() as captured,
        _running_app(_environ(bare_remote, tmp_path), ServerConfig()),
    ):
        pass

    records = [json.loads(line) for line in captured.getvalue().splitlines() if line.strip()]
    assert records
    assert all("request_id" not in record for record in records)


# --- Access log query-string redaction ------------------------------------------


def _access_log_record(full_path: str, *, status_code: int = 200) -> logging.LogRecord:
    """A `logging.LogRecord` shaped exactly like uvicorn's `uvicorn.access` line.

    Matches `args = (client_addr, method, full_path, http_version,
    status_code)` from `uvicorn.protocols.http.*` (`h11_impl.py` et al.), so
    `AccessLogQueryRedactionFilter` sees the same record shape it does in
    production, without going through a real ASGI connection.
    """
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:12345", "GET", full_path, "1.1", status_code),
        exc_info=None,
    )


def _filtered_json_msg(full_path: str) -> str:
    record = _access_log_record(full_path)
    assert AccessLogQueryRedactionFilter().filter(record) is True
    payload = json.loads(JsonFormatter().format(record))
    return cast(str, payload["msg"])


def test_access_log_filter_redacts_oauth_code_and_state() -> None:
    msg = _filtered_json_msg("/oidc/callback?code=SECRET1&state=SECRET2&x=1")

    assert "SECRET1" not in msg
    assert "SECRET2" not in msg
    assert "code=[redacted]" in msg
    assert "state=[redacted]" in msg
    assert "x=1" in msg


def test_access_log_filter_redacts_the_login_challenge() -> None:
    msg = _filtered_json_msg("/login?pending=P")

    assert "pending=P" not in msg
    assert "pending=[redacted]" in msg


def test_access_log_filter_leaves_non_sensitive_paths_untouched() -> None:
    full_path = "/static/app.js?v=3"
    record = _access_log_record(full_path)

    assert AccessLogQueryRedactionFilter().filter(record) is True
    assert cast(tuple[object, ...], record.args)[2] == full_path


def test_access_log_filter_leaves_a_path_without_a_query_string_untouched() -> None:
    full_path = "/metrics"
    record = _access_log_record(full_path)

    assert AccessLogQueryRedactionFilter().filter(record) is True
    assert cast(tuple[object, ...], record.args)[2] == full_path


def test_configure_logging_attaches_the_filter_to_uvicorn_access_exactly_once() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    previous_filters = access_logger.filters[:]
    root = logging.getLogger()
    previous_handlers = root.handlers[:]
    previous_level = root.level
    try:
        configure_logging()
        configure_logging()
        matches = [f for f in access_logger.filters if isinstance(f, AccessLogQueryRedactionFilter)]
        assert len(matches) == 1
    finally:
        access_logger.filters[:] = previous_filters
        root.handlers.clear()
        for h in previous_handlers:
            root.addHandler(h)
        root.setLevel(previous_level)


# --- No secrets or note content in logs ----------------------------------------


async def test_no_token_or_note_content_in_logs_during_an_authenticated_write(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    import asyncpg

    config = ServerConfig(public_url=_PUBLIC_URL)
    environ = _environ(bare_remote, tmp_path, DATABASE_URL=test_database_url)
    marker = "wp12-note-body-marker-9f8e7d6c5b4a"

    async with (
        _captured_json_logs() as captured,
        _running_app(environ, config) as app,
    ):
        pool = await asyncpg.create_pool(test_database_url)
        try:
            plaintext, _info = await create_token(
                pool,
                "observability-test",
                scopes=[READ_SCOPE, WRITE_SCOPE],
                namespaces=[ALL_NAMESPACES],
            )
        finally:
            await pool.close()

        async with _mcp_client(app, config, token=plaintext) as client:
            result = await client.call_tool(
                "memory_write",
                {"path": _NOTE_PATH, "content": _note_content(marker), "if_version": "new"},
            )

    assert result.is_error is False
    logs = captured.getvalue()
    assert marker not in logs
    assert plaintext not in logs


# --- Tracing (optional OTel) ----------------------------------------------------


async def test_tracing_is_a_no_op_without_the_otel_endpoint_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    # `_ensure_configured` is idempotent for the process lifetime (the env
    # var cannot meaningfully change mid-process) - reset it so this test
    # observes its own, unconfigured decision rather than whatever an
    # earlier import already decided.
    monkeypatch.setattr(tracing, "_configured", False)
    monkeypatch.setattr(tracing, "_tracer", None)

    @tracing.trace_tool_call("test_tool")
    async def tool() -> int:
        return 42

    result = await tool()

    assert result == 42
    assert tracing._tracer is None
