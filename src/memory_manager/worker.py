# SPDX-License-Identifier: AGPL-3.0-only
"""`memory-manager worker` - a separate process that runs periodic singleton
jobs exactly once across however many replicas of it run (#217, ADR-0009 §4:
"worker: embedding queue, Graph delta sync, retention, OAuth cleanup, with
its own HPA").

Builds on the one pattern already proven for this in `http.py` (#106):
`_cleanup_loop`/`_run_cleanup_iteration` sleep on a fixed interval, then try
a non-blocking Postgres advisory lock (`pg_try_advisory_xact_lock`) before
doing anything - whichever replica wins the race runs the job this round,
every other replica sees the lock held and skips it. This module turns that
one-off shape into a small, reusable registry (`Job`, `_job_loop`) instead of
a single hardcoded loop, and derives each job's own lock key from its name
(`_lock_key`) rather than one fixed constant per job - a second job
registered here later never needs a lock key of its own picked by hand.

Only one job is registered today (`_cleanup_job`, `build_jobs`): the OAuth
authorization server's stale state (`auth.store.cleanup`) and
`PostgresSharedState`'s own expired `rate_limits` rows
(`PostgresSharedState.sweep_expired_windows`) - exactly what `http.py`'s own
`_run_cleanup_iteration` already swept, moved here because this worker, not
every `api` replica, is now the one process that runs it for the `"postgres"`
backend (`http.py`'s own loop keeps running for the `"git"` backend
unchanged, ADR-0009 §6 - see that module's `lifespan` for the gating). Run
unconditionally (not gated on whether an OAuth authorization server or a
`PostgresSharedState` backend is actually configured, unlike `http.py`'s
narrower optimization): both halves are idempotent no-ops against an empty
table, and a worker process has no cheap way to know an `api` replica's own
`LOGIN_MODE`/`VALKEY_URL` without duplicating `http.py`'s own config
resolution for no benefit beyond skipping an occasional empty `DELETE`.

The embedding queue, Graph delta sync and retention jobs ADR-0009 §4 also
names for this deployment are future work (#218, #219, WP-24, WP-26) - not
registered here.

This process always connects to Postgres as the owner, never switching to
`DATABASE_APP_ROLE` (ADR-0008 addendum: "the worker is a system identity,
like `reindex`/`import`" - the same connection shape `cli.py`'s
`_open_migrated_pool` already gives `reindex`/`token create|list|revoke`),
since every job here touches operational tables (`oauth_pending`,
`oauth_auth_codes`, `oauth_tokens`, `oauth_clients`, `rate_limits`), never a
`FORCE ROW LEVEL SECURITY` content table that would need a request principal
to switch to instead.

Refuses to run at all against `STORAGE_BACKEND=git` (`cli.py`'s `worker`
subcommand) - that backend is still the single, un-replicated `api` process
`http.py`'s own loop already covers (ADR-0009 §6); a worker process would
have no job to run for it today and no principled way to run one safely
tomorrow (there is no shared Postgres to take an advisory lock against in
the first place unless `DATABASE_URL` happens to be set for the derived
index, which `"git"` does not require).

Exposes the same three paths `http.py` does (`/healthz`, `/readyz`,
`/metrics`) on its own port (`WorkerConfig.port`, default 8090) through
`create_worker_app`, served with `http.py`'s own `GracefulShutdownServer` -
the identical `SIGTERM` -> `app.state.draining = True` -> `/readyz` 503
mechanism, reused rather than reimplemented. `/readyz` additionally reports
whether every job's scheduling loop is still alive (`jobs_running`) - a
crashed loop (every iteration already guards its own job against raising,
so only a bug in the loop itself, not a failing job, could cause this) means
this worker is no longer doing anything useful, the same "not ready" signal
a database outage gives.

Graceful shutdown (ADR-0009 §1/§5, mirrored from `http.py`'s own): on
shutdown, `create_worker_app`'s `lifespan` stops every job loop from
scheduling another run and waits up to `WorkerConfig.shutdown_grace_seconds`
for whatever job is already running to finish on its own - only past that
deadline are the loops cancelled outright, so this process never hangs
forever on a job that never finishes. The process as a whole still ends
with `-SIGTERM`, not a plain `0`: `http.GracefulShutdownServer` is uvicorn's
own signal-handling machinery underneath, the same reused mechanism that
already makes the `api` process exit the identical way on `SIGTERM`
(`tests/test_shutdown.py`'s own `-signal.SIGTERM` assertion for it) - this
worker's graceful, bounded drain happens first, inside that same exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from memory_manager import __commit__, __version__
from memory_manager.auth import store
from memory_manager.auth.shared_state import PostgresSharedState
from memory_manager.config import ServerConfig, rate_limit_sweep_floor_seconds
from memory_manager.observability.metrics import metrics_endpoint

__all__ = ["Job", "build_jobs", "create_worker_app"]

_logger = logging.getLogger(__name__)

HEALTH_PATH = "/healthz"
READY_PATH = "/readyz"
METRICS_PATH = "/metrics"

_SOURCE_URL = "https://github.com/scramb/memory-manager"

#: Same interval `http.py`'s own `_CLEANUP_INTERVAL_SECONDS` used for this
#: sweep (#106) - not configurable, for the identical reason given there: the
#: advisory lock makes it a singleton regardless of how many worker replicas
#: run it, so a shorter interval would only cost extra skipped lock attempts,
#: never duplicate work.
_CLEANUP_INTERVAL_SECONDS = 60 * 60


@dataclass(frozen=True)
class Job:
    """One periodic singleton job: `run` is awaited at most every `interval_seconds`,
    and at most by one worker replica at a time (`_lock_key(name)`'s advisory lock).
    """

    name: str
    interval_seconds: float
    run: Callable[[asyncpg.Pool], Awaitable[None]]


def _lock_key(name: str) -> int:
    """The advisory lock key a job's every run takes, derived from its own `name` -
    not a fixed constant per job (`http.py`'s `_CLEANUP_LOCK_KEY` is one such
    constant, picked by hand for the one job that module runs): two jobs with
    different names can never collide on the same key, and a new job registered
    here later needs nothing picked for it beyond a name.
    """
    return zlib.crc32(f"memory_manager:worker:{name}".encode())


async def _run_singleton(pool: asyncpg.Pool, job: Job) -> None:
    """Run `job.run(pool)` iff this call wins `job`'s advisory lock this round -
    the non-blocking counterpart to `db/migrate.py`'s blocking one, same
    reasoning as `http.py`'s `_run_cleanup_iteration`: nothing waits on a
    singleton job, so a replica that loses the race skips this round outright.

    Takes the lock on its own connection, held only for as long as acquiring it
    takes (`http.py`'s `_run_cleanup_iteration` does the same) - `job.run` itself
    makes its own pool connections for whatever it actually does, same as
    `auth.store.cleanup`/`PostgresSharedState.sweep_expired_windows` always have.
    """
    async with pool.acquire() as conn, conn.transaction():
        acquired = await conn.fetchval("select pg_try_advisory_xact_lock($1)", _lock_key(job.name))
        if not acquired:
            _logger.debug(
                "worker job %r: advisory lock held elsewhere, skipping this round", job.name
            )
            return
        # `job.run` is held inside this same lock-holding transaction - still
        # on `conn`'s own connection only for the lock itself, with `job.run`
        # free to acquire whatever other pool connections it needs for its
        # own work (same shape as `http.py`'s `_run_cleanup_iteration`): the
        # lock is released only once this whole block ends, at the earliest.
        await job.run(pool)


async def _job_loop(pool: asyncpg.Pool, job: Job, stop: asyncio.Event) -> None:
    """Run `_run_singleton(pool, job)` every `job.interval_seconds`, until `stop` is
    set - mirrors `http.py`'s own `_cleanup_loop`: a failed run is logged and
    retried next interval, never allowed to crash this loop (or the process).

    `stop` is checked by racing it against the interval sleep, not polled -
    setting it while a run is already in flight has no effect on that run (it
    is let finish, `create_worker_app`'s `lifespan` docstring explains why);
    it only ever stops *another* run from being scheduled.
    """
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=job.interval_seconds)
            return
        except TimeoutError:
            pass
        try:
            await _run_singleton(pool, job)
        except Exception:
            _logger.exception("worker job %r failed", job.name)


async def _cleanup_job(pool: asyncpg.Pool, *, rate_limit_window_floor_seconds: float) -> None:
    """`auth.store.cleanup` plus `PostgresSharedState.sweep_expired_windows` - the
    exact sweep `http.py`'s own `_run_cleanup_iteration` used to run from every
    `api` replica (#106), now this worker's job for the `"postgres"` backend
    instead (module docstring: run unconditionally, not gated on whether either
    half currently has anything to do).
    """
    stats = await store.cleanup(pool)
    _logger.info(
        "oauth cleanup: pending=%d codes=%d tokens=%d clients=%d",
        stats.pending,
        stats.codes,
        stats.tokens,
        stats.clients,
    )
    swept = await PostgresSharedState(pool).sweep_expired_windows(
        older_than_seconds=rate_limit_window_floor_seconds
    )
    _logger.info("rate limit sweep: removed %d expired rate-limit window(s)", swept)


def build_jobs(config: ServerConfig) -> list[Job]:
    """This worker's job registry: today, just `_cleanup_job` (module docstring).

    `config` is read only for its `RATE_LIMIT_*`/`mcp_path`-independent fields
    (`config.rate_limit_sweep_floor_seconds`, shared with `http.py`'s own
    cleanup sweep rather than duplicated here) - `ServerConfig.from_env(os.
    environ)` builds one without requiring `PUBLIC_URL` or anything else
    HTTP-transport-specific to be set, since `resource_url()` (the one method
    that does require it) is never called here.
    """
    floor_seconds = rate_limit_sweep_floor_seconds(config)

    async def run(pool: asyncpg.Pool) -> None:
        await _cleanup_job(pool, rate_limit_window_floor_seconds=floor_seconds)

    return [Job(name="cleanup", interval_seconds=_CLEANUP_INTERVAL_SECONDS, run=run)]


def create_worker_app(
    pool: asyncpg.Pool, jobs: Sequence[Job], *, shutdown_grace_seconds: int
) -> Starlette:
    """Build the worker's own minimal Starlette app: `/healthz`, `/readyz`,
    `/metrics`, plus a `lifespan` that runs one `_job_loop` per `jobs` entry for
    as long as the app is up.

    `cli.py`'s `_serve_worker` serves the result with `http.py`'s own
    `GracefulShutdownServer` (`handle_exit` flips `app.state.draining = True`
    on `SIGTERM`/`SIGINT`, exactly as it does for the `api` process) - this
    function only has to set that flag's initial value and react to it in
    `_readyz`, the same split `http.py`'s `create_app` uses.

    On shutdown, `lifespan` sets `stop` (no job loop schedules another run
    after this) and waits up to `shutdown_grace_seconds` for every loop to
    return on its own - a loop whose job is still running when `stop` was set
    is not interrupted mid-run, only left to finish; past the deadline, any
    loop still not done is cancelled outright so this app's own shutdown (and
    therefore the process, `cli.py`'s `_serve_worker`) is never blocked
    forever on one job that never finishes.
    """

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        stop = asyncio.Event()
        tasks = [asyncio.create_task(_job_loop(pool, job, stop)) for job in jobs]
        app.state.tasks = tasks
        try:
            yield
        finally:
            stop.set()
            if tasks:
                _done, pending = await asyncio.wait(tasks, timeout=shutdown_grace_seconds)
                for task in pending:
                    task.cancel()
                for task in pending:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    routes = [
        Route(HEALTH_PATH, endpoint=_healthz, methods=["GET"]),
        Route(READY_PATH, endpoint=_readyz, methods=["GET"]),
        Route(METRICS_PATH, endpoint=metrics_endpoint, methods=["GET"]),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.pool = pool
    # Flipped to `True` by `http.GracefulShutdownServer.handle_exit` on
    # `SIGTERM`/`SIGINT` - `_readyz` checks this before anything else.
    app.state.draining = False
    return app


async def _healthz(_request: Request) -> Response:
    """ADR-0002 §13: a modified deployment's `/healthz` must point back at its source -
    the same body `http.py`'s own `_healthz` returns, this process is simply a
    different one of the same build."""
    return JSONResponse(
        {"status": "ok", "version": __version__, "commit": __commit__, "source": _SOURCE_URL}
    )


async def _readyz(request: Request) -> Response:
    """503 when draining (ADR-0009 §5), the database is unreachable, or a job loop's
    task has already finished on its own (module docstring: that can only mean
    the loop itself crashed, not that a job failed - every job run is already
    guarded inside `_job_loop`).
    """
    if request.app.state.draining:
        return JSONResponse({"ready": False, "draining": True}, status_code=503)

    pool: asyncpg.Pool = request.app.state.pool
    database_ready = True
    try:
        await pool.fetchval("select 1")
    except Exception:
        database_ready = False

    tasks: list[asyncio.Task[None]] = request.app.state.tasks
    jobs_running = bool(tasks) and all(not task.done() for task in tasks)

    ready = database_ready and jobs_running
    body = {
        "ready": ready,
        "database": database_ready,
        "jobs_running": jobs_running,
        "draining": False,
    }
    return JSONResponse(body, status_code=200 if ready else 503)
