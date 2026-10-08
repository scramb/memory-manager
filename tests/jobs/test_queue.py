# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the `jobs` outbox (#218, ADR-0007 §2/§4).

Three groups, cheapest first:

- `jobs.enqueue`/`claim`/`complete`/`fail`/`fail_or_retry` exercised directly
  against a migrated pool - the plain-SQL contract each one makes on its own.
- `worker.consume_jobs`, the `LISTEN`/poll-fallback loop that claims and
  dispatches against a handler registry - including the concurrency claim
  `FOR UPDATE SKIP LOCKED` exists for: several workers never handling the
  same row twice.
- The one security boundary this table has: the app role holds `INSERT`
  only (`db/rls.py`'s `grant_app_role`), never `SELECT`/`UPDATE`/`DELETE`.
"""

from __future__ import annotations

import asyncio
import secrets
from collections import Counter
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from memory_manager import jobs
from memory_manager.db.migrate import migrate
from memory_manager.db.rls import grant_app_role, request_identity
from memory_manager.vault.ulid import new_ulid
from memory_manager.worker import JobHandler, consume_jobs

_PROBE_KIND = "probe"


@pytest_asyncio.fixture
async def pool(test_database_url: str) -> AsyncIterator[asyncpg.Pool]:
    """A migrated pool against a fresh database - every test below's own connection."""
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
    """One `consume_jobs` task plus the `stop` event and dedicated `LISTEN`
    connection it owns - `stop_and_join` tears down all three in order.
    """

    task: asyncio.Task[None]
    stop: asyncio.Event
    listen_conn: asyncpg.Connection

    async def stop_and_join(self, *, timeout: float = 5.0) -> None:
        self.stop.set()
        await asyncio.wait_for(self.task, timeout=timeout)
        await self.listen_conn.close()


async def _start_worker(
    test_database_url: str,
    pool: asyncpg.Pool,
    handlers: Mapping[str, JobHandler],
    *,
    kinds: tuple[str, ...] = (_PROBE_KIND,),
    poll_seconds: float = 0.2,
    batch_size: int = 10,
    max_attempts: int = jobs.DEFAULT_MAX_ATTEMPTS,
) -> _Worker:
    """Start one `consume_jobs` loop on its own dedicated `LISTEN` connection
    (`consume_jobs`'s own docstring: never a pool connection for that)."""
    listen_conn = await asyncpg.connect(test_database_url)
    stop = asyncio.Event()
    task = asyncio.create_task(
        consume_jobs(
            pool,
            listen_conn,
            handlers,
            kinds=kinds,
            stop=stop,
            poll_seconds=poll_seconds,
            batch_size=batch_size,
            max_attempts=max_attempts,
        )
    )
    return _Worker(task=task, stop=stop, listen_conn=listen_conn)


# --- `jobs.py` directly ------------------------------------------------------


async def test_enqueue_then_claim_then_complete_marks_the_job_done(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn, conn.transaction():
        job_id = await jobs.enqueue(conn, _PROBE_KIND, {"note_id": "n-1"})

    claimed = await jobs.claim(pool, (_PROBE_KIND,))
    assert [job.id for job in claimed] == [job_id]
    assert claimed[0].payload == {"note_id": "n-1"}
    assert claimed[0].attempts == 1

    await jobs.complete(pool, job_id)

    row = await pool.fetchrow("select state, locked_at from jobs where id = $1", job_id)
    assert row is not None
    assert row["state"] == "done"
    assert row["locked_at"] is None


async def test_enqueue_rolled_back_with_its_transaction_never_runs(
    test_database_url: str, pool: asyncpg.Pool
) -> None:
    conn = await asyncpg.connect(test_database_url)
    try:
        tx = conn.transaction()
        await tx.start()
        await jobs.enqueue(conn, _PROBE_KIND, {"note_id": "rolled-back"})
        await tx.rollback()
    finally:
        await conn.close()

    assert await jobs.claim(pool, (_PROBE_KIND,)) == []
    count = await pool.fetchval("select count(*) from jobs")
    assert count == 0


async def test_a_payload_with_note_content_is_rejected(pool: asyncpg.Pool) -> None:
    oversized = {"body": "x" * 4096}  # note content, not an IDs-only payload

    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(jobs.PayloadTooLarge):
            await jobs.enqueue(conn, _PROBE_KIND, oversized)

    count = await pool.fetchval("select count(*) from jobs")
    assert count == 0


async def test_claim_reclaims_a_stale_running_job_left_by_a_crashed_worker(
    pool: asyncpg.Pool,
) -> None:
    async with pool.acquire() as conn, conn.transaction():
        job_id = await jobs.enqueue(conn, _PROBE_KIND, {"note_id": "n-stale"})

    first = await jobs.claim(pool, (_PROBE_KIND,), stale_after=timedelta(seconds=999))
    assert [job.id for job in first] == [job_id]

    # A fresh `claim` sees nothing: the row is `running` and not yet stale.
    assert await jobs.claim(pool, (_PROBE_KIND,), stale_after=timedelta(seconds=999)) == []

    # The crashed worker's `locked_at` ages past `stale_after` - backdated
    # directly, standing in for time actually passing.
    await pool.execute(
        "update jobs set locked_at = now() - interval '1 hour' where id = $1", job_id
    )

    reclaimed = await jobs.claim(pool, (_PROBE_KIND,), stale_after=timedelta(seconds=1))
    assert [job.id for job in reclaimed] == [job_id]
    assert reclaimed[0].attempts == 2  # claimed twice: the crash, then the reclaim


async def test_fail_or_retry_retries_then_fails_once_max_attempts_is_reached(
    pool: asyncpg.Pool,
) -> None:
    async with pool.acquire() as conn, conn.transaction():
        job_id = await jobs.enqueue(conn, _PROBE_KIND, {"note_id": "n-retry"})

    claimed = await jobs.claim(pool, (_PROBE_KIND,))
    assert claimed[0].attempts == 1
    await jobs.fail_or_retry(pool, job_id, error="boom-1", max_attempts=2)

    row = await pool.fetchrow("select state, attempts, last_error from jobs where id = $1", job_id)
    assert row is not None
    assert row["state"] == "pending"
    assert row["last_error"] == "boom-1"

    # Make the retry immediately claimable instead of waiting out the backoff.
    await pool.execute("update jobs set run_after = now() where id = $1", job_id)

    claimed_again = await jobs.claim(pool, (_PROBE_KIND,))
    assert claimed_again[0].attempts == 2
    await jobs.fail_or_retry(pool, job_id, error="boom-2", max_attempts=2)

    row = await pool.fetchrow("select state, last_error from jobs where id = $1", job_id)
    assert row is not None
    assert row["state"] == "failed"
    assert row["last_error"] == "boom-2"


# --- `worker.consume_jobs` ---------------------------------------------------


async def test_four_concurrent_workers_handle_500_jobs_exactly_once_each(
    test_database_url: str, pool: asyncpg.Pool
) -> None:
    total = 500
    async with pool.acquire() as conn:
        for i in range(total):
            async with conn.transaction():
                await jobs.enqueue(conn, _PROBE_KIND, {"i": i})

    handled: list[int] = []
    lock = asyncio.Lock()

    async def handler(_pool: asyncpg.Pool, payload: Mapping[str, object]) -> None:
        value = payload["i"]
        assert isinstance(value, int)
        async with lock:
            handled.append(value)

    workers = [
        await _start_worker(
            test_database_url, pool, {_PROBE_KIND: handler}, poll_seconds=0.2, batch_size=25
        )
        for _ in range(4)
    ]
    try:
        for _ in range(200):  # polled, bounded wait: 200 * 0.05s = 10s ceiling
            if len(handled) >= total:
                break
            await asyncio.sleep(0.05)
    finally:
        for worker in workers:
            await worker.stop_and_join()

    assert len(handled) == total
    counts = Counter(handled)
    assert set(counts) == set(range(total))
    assert all(count == 1 for count in counts.values()), (
        "at least one job was handled by more than one worker"
    )

    states = {row["state"] for row in await pool.fetch("select state from jobs")}
    assert states == {"done"}


async def test_notify_wakes_an_idle_worker_without_waiting_for_the_poll(
    test_database_url: str, pool: asyncpg.Pool
) -> None:
    handled = asyncio.Event()

    async def handler(_pool: asyncpg.Pool, _payload: Mapping[str, object]) -> None:
        handled.set()

    # A long poll interval: if the job is handled at all within this test's
    # own short timeout below, it was the NOTIFY that woke the worker, not
    # the poll fallback.
    worker = await _start_worker(test_database_url, pool, {_PROBE_KIND: handler}, poll_seconds=30.0)
    try:
        await asyncio.sleep(0.1)  # let the worker's first empty `claim` + `LISTEN` arm
        async with pool.acquire() as conn, conn.transaction():
            await jobs.enqueue(conn, _PROBE_KIND, {"note_id": "n-notify"})

        await asyncio.wait_for(handled.wait(), timeout=2.0)
    finally:
        await worker.stop_and_join()


async def test_a_lost_notify_is_picked_up_by_the_poll(
    test_database_url: str, pool: asyncpg.Pool
) -> None:
    handled = asyncio.Event()

    async def handler(_pool: asyncpg.Pool, _payload: Mapping[str, object]) -> None:
        handled.set()

    worker = await _start_worker(test_database_url, pool, {_PROBE_KIND: handler}, poll_seconds=0.2)
    try:
        # A row inserted with a plain INSERT, bypassing `jobs.enqueue`'s own
        # `pg_notify` entirely - standing in for a NOTIFY genuinely lost (the
        # docstring's "a notification lost ... is still caught by the next
        # poll_seconds timeout regardless"). Only the poll fallback can ever
        # find this row.
        await pool.execute(
            "insert into jobs (id, kind, payload) values ($1, $2, '{}'::jsonb)",
            new_ulid(),
            _PROBE_KIND,
        )

        await asyncio.wait_for(handled.wait(), timeout=2.0)
    finally:
        await worker.stop_and_join()


async def test_an_unknown_kind_fails_outright_without_a_registered_handler(
    test_database_url: str, pool: asyncpg.Pool
) -> None:
    async with pool.acquire() as conn, conn.transaction():
        job_id = await jobs.enqueue(conn, "no-such-kind", {})

    worker = await _start_worker(
        test_database_url, pool, {}, kinds=("no-such-kind",), poll_seconds=0.2
    )
    try:
        for _ in range(40):
            row = await pool.fetchrow("select state from jobs where id = $1", job_id)
            assert row is not None
            if row["state"] == "failed":
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("job with an unknown kind was never marked failed")
    finally:
        await worker.stop_and_join()

    row = await pool.fetchrow("select state, attempts, last_error from jobs where id = $1", job_id)
    assert row is not None
    assert row["state"] == "failed"
    assert row["attempts"] == 1  # unknown kind fails outright, never retried
    assert "no-such-kind" in (row["last_error"] or "")


async def test_a_failing_handler_is_retried_then_marked_failed(
    test_database_url: str, pool: asyncpg.Pool
) -> None:
    attempts: list[int] = []

    async def handler(_pool: asyncpg.Pool, _payload: Mapping[str, object]) -> None:
        attempts.append(len(attempts))
        raise RuntimeError("boom")

    async with pool.acquire() as conn, conn.transaction():
        job_id = await jobs.enqueue(conn, _PROBE_KIND, {"note_id": "n-fail"})

    worker = await _start_worker(
        test_database_url, pool, {_PROBE_KIND: handler}, poll_seconds=0.2, max_attempts=2
    )
    try:
        for _ in range(100):  # bounded wait: covers both attempts' backoff
            row = await pool.fetchrow("select state from jobs where id = $1", job_id)
            assert row is not None
            if row["state"] == "failed":
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("job was never marked failed after exhausting its retries")
    finally:
        await worker.stop_and_join()

    assert len(attempts) == 2
    row = await pool.fetchrow("select state, attempts, last_error from jobs where id = $1", job_id)
    assert row is not None
    assert row["state"] == "failed"
    assert row["attempts"] == 2
    assert row["last_error"] == "boom"


# --- the app role's one privilege: INSERT, nothing else ---------------------


@pytest_asyncio.fixture
async def app_role_db(admin_database_url: str) -> AsyncIterator[tuple[asyncpg.Connection, str]]:
    """A non-superuser owner connection plus an app role granted exactly what
    `grant_app_role` gives it - the same "mm is a superuser locally" reasoning
    `tests/db/test_rls.py`'s own `rls_db` fixture gives for needing this,
    trimmed to just the one role/table this test is about.
    """
    db_name = f"mm_test_jobs_{secrets.token_hex(8)}"
    owner_role = f"mm_test_owner_{secrets.token_hex(8)}"
    owner_password = secrets.token_urlsafe(16)
    app_role = f"mm_test_app_{secrets.token_hex(8)}"

    admin_conn = await asyncpg.connect(admin_database_url)
    owner_conn: asyncpg.Connection | None = None
    try:
        await admin_conn.execute(
            f"create role \"{owner_role}\" login password '{owner_password}' nosuperuser"
        )
        await admin_conn.execute(f'create database "{db_name}" owner "{owner_role}"')
        await admin_conn.execute(f'create role "{app_role}" nologin nosuperuser nobypassrls')
        await admin_conn.execute(f'grant "{app_role}" to "{owner_role}"')

        parsed = urlsplit(admin_database_url)
        bootstrap_url = f"{parsed.scheme}://{parsed.netloc}/{db_name}"
        bootstrap_conn = await asyncpg.connect(bootstrap_url)
        try:
            await bootstrap_conn.execute("create extension if not exists vector")
        finally:
            await bootstrap_conn.close()

        owner_url = urlunsplit(
            (
                parsed.scheme,
                f"{owner_role}:{owner_password}@{parsed.hostname}:{parsed.port}",
                f"/{db_name}",
                "",
                "",
            )
        )
        owner_conn = await asyncpg.connect(owner_url)
        await migrate(owner_conn)
        await grant_app_role(owner_conn, app_role)

        yield owner_conn, app_role
    finally:
        if owner_conn is not None:
            await owner_conn.close()
        await admin_conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity "
            "where datname = $1 and pid <> pg_backend_pid()",
            db_name,
        )
        await admin_conn.execute(f'drop database if exists "{db_name}"')
        await admin_conn.execute(f'drop role if exists "{owner_role}"')
        await admin_conn.execute(f'drop role if exists "{app_role}"')
        await admin_conn.close()


async def test_the_app_role_can_only_insert_never_read_update_or_delete_a_job(
    app_role_db: tuple[asyncpg.Connection, str],
) -> None:
    owner_conn, app_role = app_role_db

    async with request_identity(owner_conn, role=app_role, oid="oid-writer") as identified:
        job_id = await jobs.enqueue(identified, _PROBE_KIND, {"note_id": "n-app-role"})

    assert isinstance(job_id, str)

    async with request_identity(owner_conn, role=app_role, oid="oid-writer") as identified:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await identified.fetchval("select id from jobs where id = $1", job_id)

    async with request_identity(owner_conn, role=app_role, oid="oid-writer") as identified:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await identified.execute("update jobs set state = 'done' where id = $1", job_id)

    async with request_identity(owner_conn, role=app_role, oid="oid-writer") as identified:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await identified.execute("delete from jobs where id = $1", job_id)

    # The owner itself can still read the row the app role just inserted.
    row = await owner_conn.fetchrow("select state from jobs where id = $1", job_id)
    assert row is not None
    assert row["state"] == "pending"
