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
- `mm_quota_hits_total` (`scope`, `outcome`): every `quotas.QuotaChecker`
  write-quota check, labeled by scope (`user`/`namespace`/`token`) and
  `outcome` (`"allowed"`/`"rejected"`) - via `record_quota_hit`.
- `mm_rate_limit_hits_total` (`limiter`): every request actually rejected by
  a rate limiter or a quota check - never a fail-open backend error, which
  every call site already treats as allowed before this is ever reached
  (#260). Fixed `limiter` values: `"mcp"`/`"write"`/`"oauth"`/`"webhook"`
  (`http.py`'s `_send_rate_limited` call sites, one per `RateLimiter`),
  `"login"` (the password brute-force window, `auth/login_password.py`),
  `"quota_user"`/`"quota_namespace"`/`"quota_token"` (WP-27's three
  `quotas.QuotaChecker` scopes) and `"quota_storage_notes"`/
  `"quota_storage_bytes"` (`quotas.StorageQuotaChecker`'s two resources,
  #243) - via `record_rate_limit_hit`.
- `mm_build_info` (`version`, `commit`): set once at import, same values
  `/healthz` already reports (ADR-0002 §13).
- `mm_jobs_pending`/`mm_jobs_oldest_pending_age_seconds` (`kind`): the `jobs`
  outbox's own backlog, by job kind - `worker.py`'s periodic metrics-refresh
  loop (#261) sets both for every kind it knows how to handle (bounded
  cardinality: today just `"embed_note"`), via `set_jobs_pending`. Every
  known kind is refreshed every round, including back down to `0` once its
  backlog is drained - never left stuck at whatever it last was.
- `mm_embedding_lag_seconds`: age in seconds of the oldest still-pending (or
  retrying) `"embed_note"` job in the `jobs` outbox - the same worker loop,
  via `set_embedding_lag_seconds`, but only while an embedding provider is
  actually configured (`worker._embedding_provider_configured`): without
  one, no `"embed_note"` job is ever claimed, so this metric is absent from
  `/metrics` altogether rather than reporting an ever-growing, misleading
  number (`_EmbeddingLagCollector`, the one metric here that is not a plain
  `Gauge` for exactly this reason).

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

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from memory_manager import __commit__, __version__

__all__ = [
    "GIT_DURATION_SECONDS",
    "GIT_OPERATIONS_TOTAL",
    "INDEX_NOTES",
    "JOBS_OLDEST_PENDING_AGE_SECONDS",
    "JOBS_PENDING",
    "METRICS_ENABLED_ENV",
    "QUEUE_DEPTH",
    "QUEUE_WRITES_TOTAL",
    "QUOTA_HITS_TOTAL",
    "RATE_LIMIT_HITS_TOTAL",
    "SEARCH_DURATION_SECONDS",
    "TOOL_CALLS_TOTAL",
    "TOOL_DURATION_SECONDS",
    "metrics_enabled",
    "metrics_endpoint",
    "record_queue_write",
    "record_quota_hit",
    "record_rate_limit_hit",
    "set_embedding_lag_seconds",
    "set_jobs_pending",
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
QUOTA_HITS_TOTAL = Counter("mm_quota_hits_total", "Write quota checks.", ["scope", "outcome"])
RATE_LIMIT_HITS_TOTAL = Counter(
    "mm_rate_limit_hits_total", "Requests rejected by a rate limiter or quota.", ["limiter"]
)
JOBS_PENDING = Gauge("mm_jobs_pending", "Jobs currently pending in the outbox, by kind.", ["kind"])
JOBS_OLDEST_PENDING_AGE_SECONDS = Gauge(
    "mm_jobs_oldest_pending_age_seconds",
    "Age in seconds of the oldest pending job, by kind.",
    ["kind"],
)

_BUILD_INFO = Info("mm_build", "The version and commit this process was built from.")
_BUILD_INFO.info({"version": __version__, "commit": __commit__})


class _EmbeddingLagCollector:
    """Backs `mm_embedding_lag_seconds` (#261): a plain `Gauge` reports `0` from
    the moment it is created, even before anything ever calls `.set()` - the
    wrong default here, since "0 lag" and "no embedding provider configured
    at all" must never look the same on `/metrics`. `collect()` yields no
    sample at all while `_seconds` is `None` (the initial state, and
    `set_embedding_lag_seconds(None)`'s own reset) - the one escape hatch a
    plain `prometheus_client` metric type does not give a caller, which is
    why this metric alone is a custom `Collector` rather than a `Gauge`.
    """

    def __init__(self) -> None:
        self._seconds: float | None = None

    def set(self, seconds: float | None) -> None:
        self._seconds = seconds

    def collect(self) -> Iterator[GaugeMetricFamily]:
        if self._seconds is None:
            return
        family = GaugeMetricFamily(
            "mm_embedding_lag_seconds",
            "Age in seconds of the oldest note revision without a current embedding.",
        )
        family.add_metric([], self._seconds)
        yield family


_EMBEDDING_LAG_COLLECTOR = _EmbeddingLagCollector()
REGISTRY.register(_EMBEDDING_LAG_COLLECTOR)

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


def record_quota_hit(*, scope: str, outcome: str) -> None:
    """`mm_quota_hits_total`'s one call site: `quotas.QuotaChecker`'s own write check."""
    QUOTA_HITS_TOTAL.labels(scope=scope, outcome=outcome).inc()


def record_rate_limit_hit(limiter: str) -> None:
    """`mm_rate_limit_hits_total`'s one metric: a request actually rejected by
    `limiter` - never a fail-open backend error (module docstring's list of
    fixed `limiter` values). Called from `http.py`'s `_send_rate_limited`,
    `auth/login_password.py`'s brute-force rejection, `quotas.QuotaChecker`'s
    `_enforce` rejection branch and `quotas.StorageQuotaChecker`'s `_reject` -
    each already past its own fail-open check by the time this runs, so a
    transient backend outage never shows up here."""
    RATE_LIMIT_HITS_TOTAL.labels(limiter=limiter).inc()


def set_jobs_pending(kind: str, pending: int, oldest_age_seconds: float) -> None:
    """`mm_jobs_pending`/`mm_jobs_oldest_pending_age_seconds`'s one call site:
    `worker.py`'s own periodic metrics-refresh loop (#261), once per job
    `kind` it knows how to handle - called every round for every such
    `kind`, including with `pending=0` once its backlog is drained, so
    neither gauge is ever left stuck at a stale, non-zero value.
    """
    JOBS_PENDING.labels(kind=kind).set(pending)
    JOBS_OLDEST_PENDING_AGE_SECONDS.labels(kind=kind).set(oldest_age_seconds)


def set_embedding_lag_seconds(seconds: float | None) -> None:
    """`mm_embedding_lag_seconds`'s one call site: `worker.py`'s own periodic
    metrics-refresh loop (#261). `None` - always passed while no embedding
    provider is configured (`worker._embedding_provider_configured`) - clears
    any previous value, so the metric is absent from `/metrics` altogether
    rather than stuck at whatever it last was (`_EmbeddingLagCollector.collect`).
    """
    _EMBEDDING_LAG_COLLECTOR.set(seconds)


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
