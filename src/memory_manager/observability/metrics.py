# SPDX-License-Identifier: AGPL-3.0-only
"""Prometheus metrics for memory-manager (#43, WP-12).

Every metric is a module-level singleton registered on `prometheus_client`'s
default registry, the usual pattern for that library - `metrics_endpoint`
(mounted at `/metrics` by `http.py`) renders exactly that registry with
`generate_latest()`, no registry plumbing needed anywhere else.

What each metric answers:
- `mm_tool_calls_total`/`mm_tool_duration_seconds` (`tool`, `outcome`):
  how often, how long, and whether an MCP tool call succeeded - from
  `track_tool_call`, the decorator `observability.instrument_tool` applies
  to every tool in `mcp/server.py`.
- `mm_queue_writes_total` (`op`, `outcome`)/`mm_queue_depth`: the write
  queue's own throughput and backlog - `queue.py` calls `record_queue_write`
  after every write job and sets `QUEUE_DEPTH` after every `put`/`get` on
  its internal `asyncio.Queue`.
- `mm_git_operations_total`/`mm_git_duration_seconds` (`op`): every `git`
  subprocess `vault/git.py`'s `Git.run` invokes, labeled by its subcommand
  (`_git_op_name`) - via `track_git_operation`.
- `mm_search_duration_seconds` (`mode`): how long `search.hybrid_search`
  took, labeled `hybrid` (a vector provider was given) or `fulltext` (none
  was) - via `track_search`.
- `mm_index_notes`: the Postgres index's current note count. Defined here
  so the metric name exists; nothing in this work package's blast radius
  sets it (the indexer is out of scope, #45).
- `mm_build_info` (`version`, `commit`): set once at import, same values
  `/healthz` already reports (ADR-0002 §13).

Never records a note's content or a token - every label here is a tool
name, an `Op` literal, a git subcommand or a search mode: operational
shape, not user data (CLAUDE.md: note content is data, never logged).
"""

from __future__ import annotations

import functools
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from typing import Any, TypeVar

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, Info, generate_latest
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from memory_manager import __commit__, __version__

__all__ = [
    "GIT_DURATION_SECONDS",
    "GIT_OPERATIONS_TOTAL",
    "INDEX_NOTES",
    "METRICS_ENABLED_ENV",
    "QUEUE_DEPTH",
    "QUEUE_WRITES_TOTAL",
    "SEARCH_DURATION_SECONDS",
    "TOOL_CALLS_TOTAL",
    "TOOL_DURATION_SECONDS",
    "metrics_enabled",
    "metrics_endpoint",
    "record_queue_write",
    "track_git_operation",
    "track_search",
    "track_tool_call",
]

METRICS_ENABLED_ENV = "METRICS_ENABLED"
_FALSY_BOOL_ENV = frozenset({"0", "false", "no", "off", ""})

TOOL_CALLS_TOTAL = Counter("mm_tool_calls_total", "MCP tool calls.", ["tool", "outcome"])
TOOL_DURATION_SECONDS = Histogram(
    "mm_tool_duration_seconds", "MCP tool call duration in seconds.", ["tool"]
)
QUEUE_WRITES_TOTAL = Counter(
    "mm_queue_writes_total", "Write queue jobs processed.", ["op", "outcome"]
)
QUEUE_DEPTH = Gauge("mm_queue_depth", "Jobs currently queued in the write queue.")
GIT_OPERATIONS_TOTAL = Counter(
    "mm_git_operations_total", "git subprocess invocations.", ["op", "outcome"]
)
GIT_DURATION_SECONDS = Histogram(
    "mm_git_duration_seconds", "git subprocess duration in seconds.", ["op"]
)
SEARCH_DURATION_SECONDS = Histogram(
    "mm_search_duration_seconds", "hybrid_search duration in seconds.", ["mode"]
)
INDEX_NOTES = Gauge("mm_index_notes", "Notes currently held in the Postgres index.")

_BUILD_INFO = Info("mm_build", "The version and commit this process was built from.")
_BUILD_INFO.info({"version": __version__, "commit": __commit__})

_ToolFunc = TypeVar("_ToolFunc", bound=Callable[..., Awaitable[Any]])

# `Repo._auth_args()` is the only flag `vault/git.py` ever passes before a
# subcommand, and it always takes its config value as the next argument.
_GIT_VALUE_FLAGS = frozenset({"-c"})


def metrics_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether `/metrics` should serve anything, per `METRICS_ENABLED` (default: on)."""
    raw = (environ if environ is not None else os.environ).get(METRICS_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSY_BOOL_ENV


async def metrics_endpoint(_request: Request) -> Response:
    """The `/metrics` route `http.py` mounts: the Prometheus text exposition format.

    404 - the endpoint does not exist, rather than existing just to return
    nothing - when `METRICS_ENABLED` is falsy, the same contract `http.py`'s
    own vault webhook uses for a disabled feature. No auth of its own: an
    operator exposing this publicly is expected to restrict it at the
    network layer (Helm's `ServiceMonitor`, #45, runs inside the cluster).
    """
    if not metrics_enabled():
        return PlainTextResponse("not found", status_code=404)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def track_tool_call(tool: str) -> Callable[[_ToolFunc], _ToolFunc]:
    """Decorate an async MCP tool function with `mm_tool_calls_total`/`_duration_seconds`.

    `outcome` is `"error"` whenever the call raised, or returned something
    carrying `is_error=True` (every write tool's `CallToolResult` on a
    conflict) - `"ok"` otherwise. Relies on `functools.wraps` setting
    `__wrapped__`: both `inspect.signature(..., eval_str=True)` and
    `typing.get_type_hints` follow it to the wrapped function (verified
    against the installed SDK, `mcp.server.mcpserver.utilities.func_metadata`/
    `context_injection`), so the SDK's own schema generation and `Context`
    injection see the original signature, not `(*args, **kwargs)`.
    """

    def decorator(func: _ToolFunc) -> _ToolFunc:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            outcome = "error"
            try:
                result = await func(*args, **kwargs)
                outcome = "error" if getattr(result, "is_error", False) else "ok"
                return result
            finally:
                TOOL_CALLS_TOTAL.labels(tool=tool, outcome=outcome).inc()
                TOOL_DURATION_SECONDS.labels(tool=tool).observe(time.monotonic() - start)

        return wrapper  # type: ignore[return-value]

    return decorator


def record_queue_write(op: str, outcome: str) -> None:
    """`mm_queue_writes_total`'s one call site: after every write job the consumer runs."""
    QUEUE_WRITES_TOTAL.labels(op=op, outcome=outcome).inc()


def _git_op_name(args: Sequence[str]) -> str:
    """The first real subcommand in a `git` invocation, skipping leading flags.

    `-c <value>` is skipped as a pair; any other leading `-...` flag is
    skipped on its own. Returns `"unknown"` for an invocation that is
    nothing but flags (never actually produced by `vault/git.py`, but a
    label value, not an exception, is the right failure mode for a metric).
    """
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in _GIT_VALUE_FLAGS:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return "unknown"


@contextmanager
def track_git_operation(args: Sequence[str]) -> Iterator[None]:
    """Count and time one `git` subprocess invocation, labeled by `_git_op_name(args)`.

    `outcome` is `"error"` whenever the wrapped call raises (a `GitError`,
    `PushRejected`, or anything else) - `"ok"` otherwise, including a
    `check=False` call that came back with a non-zero exit code: that is an
    expected possible result for those callers, not a failed invocation.
    """
    op = _git_op_name(args)
    start = time.monotonic()
    outcome = "error"
    try:
        yield
        outcome = "ok"
    finally:
        GIT_OPERATIONS_TOTAL.labels(op=op, outcome=outcome).inc()
        GIT_DURATION_SECONDS.labels(op=op).observe(time.monotonic() - start)


@asynccontextmanager
async def track_search(mode: str) -> AsyncIterator[None]:
    """Time one `search.hybrid_search` call under `mm_search_duration_seconds{mode}`."""
    start = time.monotonic()
    try:
        yield
    finally:
        SEARCH_DURATION_SECONDS.labels(mode=mode).observe(time.monotonic() - start)
