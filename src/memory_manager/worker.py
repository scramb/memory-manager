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

The `jobs` outbox consumer (#218, `consume_jobs` below) is a second,
unrelated loop shape this module runs, for a different problem: several
worker replicas claiming disjoint rows from `jobs` concurrently, rather than
one replica winning an exclusive lock. No job kind is registered against it
yet - the embedding queue is #219's own follow-up, Graph delta sync and
retention jobs ADR-0009 §4 also names are WP-24/WP-26's.

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
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from memory_manager import __commit__, __version__, jobs
from memory_manager.auth import store
from memory_manager.auth.shared_state import PostgresSharedState
from memory_manager.config import ServerConfig, rate_limit_sweep_floor_seconds
from memory_manager.observability.metrics import metrics_endpoint

__all__ = [
    "Job",
    "JobHandler",
    "build_job_handlers",
    "build_jobs",
    "consume_jobs",
    "create_worker_app",
]

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


# --- the `jobs` outbox consumer (#218) --------------------------------------
#
# A second, unrelated kind of loop this module runs: unlike `Job`/`_job_loop`
# above (a periodic singleton, won by exactly one replica's advisory lock,
# nothing ever waits on it), `consume_jobs` below claims rows several worker
# replicas may run *concurrently* - `jobs.claim`'s own `FOR UPDATE SKIP
# LOCKED` is what keeps two replicas from ever claiming the same row, not an
# advisory lock. No job kind is registered here yet (`handlers` is empty in
# production today) - the embedding queue is #219's own follow-up; this is
# only the consumer shape `jobs.claim`/`complete`/`fail`/`fail_or_retry` plug
# into, plus the unknown-kind-fails-outright case any future kind not yet
# known to a given worker build would otherwise hit.

#: `handlers`' value type: runs a claimed job's own `payload` against `pool`
#: (as the owner, same as every other call this module makes - `jobs.py`'s
#: own module docstring) and either returns (the job is `complete`d) or
#: raises (`fail_or_retry`, with the raised message as `last_error`).
JobHandler = Callable[[asyncpg.Pool, Mapping[str, object]], Awaitable[None]]

#: `consume_jobs`'s own default batch size - `create_worker_app`'s wiring
#: and this module's tests share the one constant rather than each picking
#: their own number.
_DEFAULT_JOBS_BATCH_SIZE = 10

#: `create_worker_app`'s own fallback for `jobs_poll_seconds` if a caller
#: does not read `WorkerConfig.jobs_poll_seconds` itself - the same default
#: that config carries (`config.py`'s own `_DEFAULT_JOBS_POLL_SECONDS`), not
#: imported from there to avoid a needless cross-module constant coupling
#: for one float both modules are free to default independently.
_DEFAULT_JOBS_POLL_SECONDS = 5.0


def build_job_handlers(_config: ServerConfig) -> dict[str, JobHandler]:
    """This worker's `jobs`-outbox handler registry: empty today.

    Mirrors `build_jobs` above for the other loop shape this module runs -
    `_config` is accepted (unused for now) for the same reason `build_jobs`
    takes `config`: a future handler (the embedding queue, #219) will need
    its own config fields, and every caller of this function already builds
    a `ServerConfig` to pass to `build_jobs` anyway. `create_worker_app`
    derives `consume_jobs`'s own `kinds` from this registry's keys, so
    registering a handler here is the only step #219 needs on this side -
    no separate kind list to keep in sync.
    """
    return {}


async def _dispatch_job(
    pool: asyncpg.Pool,
    handlers: Mapping[str, JobHandler],
    job: jobs.ClaimedJob,
    *,
    max_attempts: int,
) -> None:
    """Run `job` against `handlers`, or `jobs.fail` it outright if `handlers` has
    nothing registered for `job.kind` - unlike a handler's own failure (caught
    below, retried with backoff through `jobs.fail_or_retry`), an unknown kind
    can never succeed on a later attempt, so retrying it would only delay the
    same outcome.
    """
    handler = handlers.get(job.kind)
    if handler is None:
        _logger.error("job %s: no handler registered for kind %r, failing", job.id, job.kind)
        await jobs.fail(pool, job.id, error=f"unknown job kind {job.kind!r}")
        return
    try:
        await handler(pool, job.payload)
    except Exception as exc:
        _logger.exception("job %s (kind=%r) failed", job.id, job.kind)
        await jobs.fail_or_retry(pool, job.id, error=str(exc), max_attempts=max_attempts)
    else:
        await jobs.complete(pool, job.id)


async def consume_jobs(
    pool: asyncpg.Pool,
    listen_conn: asyncpg.Connection,
    handlers: Mapping[str, JobHandler],
    *,
    kinds: Sequence[str],
    stop: asyncio.Event,
    poll_seconds: float,
    batch_size: int = _DEFAULT_JOBS_BATCH_SIZE,
    max_attempts: int = jobs.DEFAULT_MAX_ATTEMPTS,
) -> None:
    """Claim and dispatch `kinds` jobs until `stop` is set.

    `listen_conn` must be a connection dedicated to this call for as long as
    it runs, never a pool connection (docs/research/enterprise.md §"Pool
    sizing": "LISTEN does not work through PgBouncer transaction mode ->
    dedicated direct connection per worker") - this function registers a
    listener on it (`jobs.NOTIFY_CHANNEL`) and removes it again on return,
    but never acquires or releases `listen_conn` itself; the caller owns its
    lifetime.

    Each round: claim up to `batch_size` jobs and dispatch every one of them
    in turn (a single worker runs its own batch sequentially, not
    concurrently - `jobs.claim`'s `SKIP LOCKED` is what lets *several worker
    replicas* overlap, not what this one replica does with its own claimed
    batch). If the batch was non-empty, loop straight back to `claim` again
    without waiting - more may already be waiting. Once a `claim` comes back
    empty, wait for either `stop`, a `NOTIFY` on `jobs.NOTIFY_CHANNEL`, or
    `poll_seconds`, whichever comes first - the `NOTIFY` is only ever a
    wake-up hint (ADR-0007 §4): a notification lost between this round's
    empty `claim` and the listener being (re)armed is still caught by the
    next `poll_seconds` timeout regardless.
    """
    wake = asyncio.Event()

    def _on_notify(
        _conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
        _pid: int,
        _channel: str,
        _payload: object,
    ) -> None:
        wake.set()

    await listen_conn.add_listener(jobs.NOTIFY_CHANNEL, _on_notify)
    try:
        while not stop.is_set():
            claimed = await jobs.claim(pool, kinds, limit=batch_size)
            for job in claimed:
                await _dispatch_job(pool, handlers, job, max_attempts=max_attempts)
            if claimed:
                continue
            wake.clear()
            stop_wait = asyncio.ensure_future(stop.wait())
            wake_wait = asyncio.ensure_future(wake.wait())
            try:
                await asyncio.wait(
                    {stop_wait, wake_wait},
                    timeout=poll_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in (stop_wait, wake_wait):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(stop_wait, wake_wait, return_exceptions=True)
    finally:
        await listen_conn.remove_listener(jobs.NOTIFY_CHANNEL, _on_notify)


def create_worker_app(
    pool: asyncpg.Pool,
    jobs: Sequence[Job],
    *,
    shutdown_grace_seconds: int,
    jobs_listen_conn: asyncpg.Connection | None = None,
    job_handlers: Mapping[str, JobHandler] | None = None,
    jobs_poll_seconds: float = _DEFAULT_JOBS_POLL_SECONDS,
    jobs_batch_size: int = _DEFAULT_JOBS_BATCH_SIZE,
) -> Starlette:
    """Build the worker's own minimal Starlette app: `/healthz`, `/readyz`,
    `/metrics`, plus a `lifespan` that runs one `_job_loop` per `jobs` entry,
    and - if `jobs_listen_conn` is given - one `consume_jobs` loop for the
    `jobs` outbox (#218), for as long as the app is up.

    `jobs_listen_conn` is `None` in every existing test of this function and
    for any caller that has no dedicated `LISTEN` connection to hand it
    (`consume_jobs`'s own docstring: never a pool connection) - the outbox
    loop is then simply not started, exactly as if this parameter did not
    exist. `cli.py`'s `_serve_worker` is the one production caller that
    passes one; `job_handlers` defaults to `build_job_handlers`'s own empty
    registry (#219 not landed yet) if omitted. `consume_jobs`'s own `kinds`
    is derived from `job_handlers`' keys - registering a handler is the only
    step adding a job kind needs on this side, no separate list to keep in
    sync.

    `cli.py`'s `_serve_worker` serves the result with `http.py`'s own
    `GracefulShutdownServer` (`handle_exit` flips `app.state.draining = True`
    on `SIGTERM`/`SIGINT`, exactly as it does for the `api` process) - this
    function only has to set that flag's initial value and react to it in
    `_readyz`, the same split `http.py`'s `create_app` uses.

    On shutdown, `lifespan` sets `stop` (no job loop schedules another run,
    and `consume_jobs` claims nothing further, after this) and waits up to
    `shutdown_grace_seconds` for every loop to return on its own - a loop
    whose job is still running when `stop` was set is not interrupted
    mid-run, only left to finish; past the deadline, any loop still not done
    is cancelled outright so this app's own shutdown (and therefore the
    process, `cli.py`'s `_serve_worker`) is never blocked forever on one job
    that never finishes. `jobs_listen_conn` itself is never closed here - it
    was never opened here either (module docstring of `consume_jobs`); the
    caller that opened it closes it, same as `pool`.
    """

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        stop = asyncio.Event()
        tasks = [asyncio.create_task(_job_loop(pool, job, stop)) for job in jobs]
        if jobs_listen_conn is not None:
            handlers = job_handlers if job_handlers is not None else {}
            tasks.append(
                asyncio.create_task(
                    consume_jobs(
                        pool,
                        jobs_listen_conn,
                        handlers,
                        kinds=tuple(handlers),
                        stop=stop,
                        poll_seconds=jobs_poll_seconds,
                        batch_size=jobs_batch_size,
                    )
                )
            )
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
