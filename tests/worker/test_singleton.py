# SPDX-License-Identifier: AGPL-3.0-only
"""`memory-manager worker` (#217, ADR-0009 §4): periodic singleton jobs run exactly
once across however many worker replicas run them, this process exposes
`/healthz`/`/readyz`/`/metrics` on `WorkerConfig.port`, drains gracefully on
`SIGTERM`, and `STORAGE_BACKEND=postgres` is the one backend it ever runs for
(the `"git"` backend keeps its cleanup sweep inside `serve --http` itself,
ADR-0009 §6).

Three groups of tests, cheapest first:

- The generic locking primitive (`_run_singleton`/`_lock_key`) against a real
  Postgres advisory lock, with a foreign connection standing in for a second
  worker replica - the same "exercised directly against the pool, not through
  a subprocess pair" reasoning `tests/e2e/test_replicas.py`'s own
  `test_cleanup_sweep_is_a_singleton_across_replicas` gives: the real job
  registered in production (`build_jobs`) runs on a fixed hourly interval, far
  too long to wait out here, and the lock itself - not any one job's business
  logic - is what these tests are about.
- `_job_loop`'s own scheduling (interval, `stop`), with a fast test-only `Job`
  substituted in - the seam `build_jobs` exists to fill with the real one.
- The HTTP surface and graceful shutdown: `/healthz`/`/readyz`/`/metrics` via
  `create_worker_app` (in-process, `httpx.ASGITransport`, the same pattern
  `tests/test_shutdown.py`'s `_running_app` uses) for `/readyz`'s own logic
  and `http.GracefulShutdownServer` reuse, then a real `memory-manager worker`
  subprocess (two of them, started against the same database) for "the
  process actually starts, serves those paths, and exits cleanly on
  SIGTERM" - a claim the in-process ASGI app alone cannot make.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import asyncpg
import httpx
import uvicorn
from http_fixtures import Server, free_port, wait_until_ready

from memory_manager.http import GracefulShutdownServer
from memory_manager.worker import Job, _job_loop, _lock_key, _run_singleton, create_worker_app

__all__: list[str] = []

_WORKER_CLI_ARGS = ("-m", "memory_manager.cli", "worker")
_WORKER_SHUTDOWN_TIMEOUT = 5.0


@asynccontextmanager
async def _run_worker_server(env: Mapping[str, str]) -> AsyncIterator[Server]:
    """Start `memory-manager worker` on a fresh free port with `env` merged onto the
    ambient environment (`WORKER_HOST`/`WORKER_PORT` are always overridden, last),
    wait for `/healthz`, yield a `Server`, then terminate it - the worker's own
    counterpart to `http_fixtures.run_http_server`, reusing that module's
    `free_port`/`wait_until_ready`/`Server` directly rather than duplicating them:
    both are already general enough (`wait_until_ready` only ever polls `/healthz`,
    which `worker.py` serves at the identical path). `Server.mcp_url` is unused here
    (the worker serves no MCP endpoint at all) - kept only because it is `Server`'s
    own field, not worth a separate dataclass for this one difference.
    """
    port = free_port()
    full_env = {**os.environ, **env, "WORKER_HOST": "127.0.0.1", "WORKER_PORT": str(port)}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *_WORKER_CLI_ARGS,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=full_env,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        await wait_until_ready(process, base_url)
        yield Server(process=process, base_url=base_url, mcp_url=f"{base_url}/mcp")
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_WORKER_SHUTDOWN_TIMEOUT)
            except TimeoutError:
                process.kill()
                await process.wait()


# --- the locking primitive (`_run_singleton`/`_lock_key`) -------------------


async def test_run_singleton_skips_while_a_foreign_connection_holds_the_lock(
    test_database_url: str,
) -> None:
    ran: list[str] = []

    async def run(_pool: asyncpg.Pool) -> None:
        ran.append("ran")

    job = Job(name="probe-held-lock", interval_seconds=3600.0, run=run)

    pool = await asyncpg.create_pool(test_database_url)
    foreign_conn = await asyncpg.connect(test_database_url)
    foreign_tx = foreign_conn.transaction()
    await foreign_tx.start()
    try:
        await foreign_conn.fetchval("select pg_advisory_xact_lock($1)", _lock_key(job.name))

        await _run_singleton(pool, job)

        assert ran == []
    finally:
        await foreign_tx.rollback()
        await foreign_conn.close()
        await pool.close()


async def test_run_singleton_runs_once_the_lock_holder_is_killed(test_database_url: str) -> None:
    """Closing the lock-holding connection outright (not a graceful rollback)
    simulates that worker replica being killed, not shut down cleanly - Postgres
    still rolls the transaction back and releases the advisory lock, exactly as
    `conn.transaction()`'s own `__aexit__` would: an xact-scoped lock is tied to
    the transaction, not to how it ended. A second attempt against the same job
    then succeeds, taking the now-free lock over."""
    ran: list[str] = []

    async def run(_pool: asyncpg.Pool) -> None:
        ran.append("ran")

    job = Job(name="probe-killed-holder", interval_seconds=3600.0, run=run)

    pool = await asyncpg.create_pool(test_database_url)
    try:
        foreign_conn = await asyncpg.connect(test_database_url)
        foreign_tx = foreign_conn.transaction()
        await foreign_tx.start()
        await foreign_conn.fetchval("select pg_advisory_xact_lock($1)", _lock_key(job.name))

        await _run_singleton(pool, job)
        assert ran == []

        await foreign_conn.close()

        await _run_singleton(pool, job)
        assert ran == ["ran"]
    finally:
        await pool.close()


async def test_lock_key_is_derived_from_the_job_name_so_two_jobs_never_collide(
    test_database_url: str,
) -> None:
    ran_a: list[str] = []
    ran_b: list[str] = []

    async def run_a(_pool: asyncpg.Pool) -> None:
        ran_a.append("ran")

    async def run_b(_pool: asyncpg.Pool) -> None:
        ran_b.append("ran")

    job_a = Job(name="probe-a", interval_seconds=3600.0, run=run_a)
    job_b = Job(name="probe-b", interval_seconds=3600.0, run=run_b)
    assert _lock_key(job_a.name) != _lock_key(job_b.name)

    pool = await asyncpg.create_pool(test_database_url)
    foreign_conn = await asyncpg.connect(test_database_url)
    foreign_tx = foreign_conn.transaction()
    await foreign_tx.start()
    try:
        # Holding job_a's lock must never block job_b's run.
        await foreign_conn.fetchval("select pg_advisory_xact_lock($1)", _lock_key(job_a.name))

        await _run_singleton(pool, job_a)
        await _run_singleton(pool, job_b)

        assert ran_a == []
        assert ran_b == ["ran"]
    finally:
        await foreign_tx.rollback()
        await foreign_conn.close()
        await pool.close()


# --- `_job_loop`'s own scheduling -------------------------------------------


async def test_job_loop_runs_the_job_repeatedly_until_stopped(test_database_url: str) -> None:
    runs: list[int] = []

    async def run(_pool: asyncpg.Pool) -> None:
        runs.append(len(runs))

    job = Job(name="probe-interval", interval_seconds=0.05, run=run)
    stop = asyncio.Event()
    pool = await asyncpg.create_pool(test_database_url)
    try:
        task = asyncio.create_task(_job_loop(pool, job, stop))
        await asyncio.sleep(0.23)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        await pool.close()

    # Four 0.05s intervals fit in 0.23s; generous bounds for a loaded CI box.
    assert 2 <= len(runs) <= 6


async def test_job_loop_survives_a_failing_job_and_keeps_scheduling(
    test_database_url: str,
) -> None:
    attempts: list[int] = []

    async def run(_pool: asyncpg.Pool) -> None:
        attempts.append(len(attempts))
        raise RuntimeError("boom")

    job = Job(name="probe-failing", interval_seconds=0.05, run=run)
    stop = asyncio.Event()
    pool = await asyncpg.create_pool(test_database_url)
    try:
        task = asyncio.create_task(_job_loop(pool, job, stop))
        await asyncio.sleep(0.23)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        await pool.close()

    assert len(attempts) >= 2


# --- the HTTP surface and graceful shutdown, in-process ---------------------


async def test_healthz_and_metrics_are_served(test_database_url: str) -> None:
    pool = await asyncpg.create_pool(test_database_url)
    job = Job(name="probe-noop", interval_seconds=3600.0, run=lambda _pool: asyncio.sleep(0))
    app = create_worker_app(pool, [job], shutdown_grace_seconds=5)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                healthz = await client.get("/healthz")
                assert healthz.status_code == 200
                assert healthz.json()["status"] == "ok"

                metrics = await client.get("/metrics")
                assert metrics.status_code == 200
                assert metrics.text  # the Prometheus text exposition format
    finally:
        await pool.close()


async def test_readyz_is_ready_once_the_job_loop_is_up_and_turns_503_when_draining(
    test_database_url: str,
) -> None:
    pool = await asyncpg.create_pool(test_database_url)
    job = Job(name="probe-noop", interval_seconds=3600.0, run=lambda _pool: asyncio.sleep(0))
    app = create_worker_app(pool, [job], shutdown_grace_seconds=5)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                before = await client.get("/readyz")
                assert before.status_code == 200
                body = before.json()
                assert body["ready"] is True
                assert body["database"] is True
                assert body["jobs_running"] is True

                uvicorn_config = uvicorn.Config(app, host="127.0.0.1", port=0)
                server = GracefulShutdownServer(uvicorn_config)
                server.handle_exit(signal.SIGTERM, None)

                after = await client.get("/readyz")
    finally:
        await pool.close()

    assert after.status_code == 503
    assert after.json() == {"ready": False, "draining": True}


async def test_readyz_is_not_ready_without_any_registered_job(test_database_url: str) -> None:
    pool = await asyncpg.create_pool(test_database_url)
    app = create_worker_app(pool, [], shutdown_grace_seconds=5)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.get("/readyz")
    finally:
        await pool.close()

    assert response.status_code == 503
    assert response.json()["jobs_running"] is False


# --- a real subprocess ------------------------------------------------------


def _worker_env(database_url: str) -> dict[str, str]:
    return {
        "STORAGE_BACKEND": "postgres",
        "DATABASE_URL": database_url,
        "SHUTDOWN_GRACE_SECONDS": "5",
    }


async def test_two_worker_processes_serve_their_own_healthz_and_metrics(
    test_database_url: str,
) -> None:
    """Two `memory-manager worker` processes against the same database (#217) -
    both come up cleanly and serve their own `/healthz`/`/metrics` without
    conflicting over the database (`migrate()` is safe to run concurrently,
    `db/migrate.py`'s own docstring) - the real job's hourly interval means
    neither is ever observed actually racing for its lock here (that is
    `test_run_singleton_runs_once_the_lock_holder_is_killed` above's job).
    """
    env = _worker_env(test_database_url)
    async with (
        _run_worker_server(env) as server1,
        _run_worker_server(env) as server2,
        httpx.AsyncClient(timeout=10.0) as client,
    ):
        for server in (server1, server2):
            healthz = await client.get(f"{server.base_url}/healthz")
            assert healthz.status_code == 200

            metrics = await client.get(f"{server.base_url}/metrics")
            assert metrics.status_code == 200

            readyz = await client.get(f"{server.base_url}/readyz")
            assert readyz.status_code == 200
            assert readyz.json()["ready"] is True


async def test_sigterm_turns_readyz_503_and_the_process_exits_cleanly(
    test_database_url: str,
) -> None:
    """`GracefulShutdownServer` (reused verbatim from `http.py`, module docstring) drains
    and exits the same way `tests/test_shutdown.py`'s own equivalent SIGTERM tests
    already pin for the `api` process: `-signal.SIGTERM`, not `0` - uvicorn's own
    `install_signal_handlers` is what the OS ultimately sees deliver the process's
    end, regardless of the clean, bounded shutdown `create_worker_app`'s `lifespan`
    runs first (`/readyz` turning 503 immediately, every job loop given up to
    `shutdown_grace_seconds` to finish on its own). "Exits cleanly" here means
    exactly what it means there: within `shutdown_grace_seconds`, never hanging, and
    never a non-graceful `-SIGKILL`.
    """
    env = _worker_env(test_database_url)
    async with _run_worker_server(env) as server:
        async with httpx.AsyncClient(timeout=10.0) as client:
            readyz = await client.get(f"{server.base_url}/readyz")
            assert readyz.status_code == 200

            server.process.send_signal(signal.SIGTERM)

            try:
                drained = await client.get(f"{server.base_url}/readyz", timeout=2.0)
            except httpx.TransportError:
                pass
            else:
                assert drained.status_code == 503

        returncode = await asyncio.wait_for(server.process.wait(), timeout=10.0)

    assert returncode == -signal.SIGTERM
