# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for #240: the worker's own `retention` job erases a deprovisioned
user's personal memory once `PERSONAL_RETENTION_DAYS` has passed.

Drives `worker._retention_job` directly against a real Postgres pool, the
same "exercise the job function directly, not through a subprocess" shape
`tests/worker/test_delta_sync.py` already uses for `_entra_delta_sync_job` -
`build_jobs`'s own wiring of this job (`worker_config`, scheduling, the
advisory lock) is not re-tested here, that is generic across every `Job`,
already covered by `tests/worker/test_singleton.py`.

A fixed `clock` (never the real `datetime.now(UTC)`) places a user's
`disabled_at` a controlled number of days before "now", without ever
sleeping `retention_days` days for real - the same "injectable clock" shape
`storage.postgres.PostgresBackend.__init__`'s own `clock` parameter uses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest_asyncio

from memory_manager.auth.users import enable_user, get_user, mark_disabled, upsert_user
from memory_manager.db.migrate import migrate
from memory_manager.worker import _RETENTION_ACTOR, _retention_job

_TID = "99999999-9999-9999-9999-999999999999"
_RETENTION_DAYS = 30
_NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def pool(test_database_url: str) -> AsyncIterator[asyncpg.Pool]:
    conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(conn, backend="postgres")
    finally:
        await conn.close()
    created_pool = await asyncpg.create_pool(test_database_url)
    try:
        yield created_pool
    finally:
        await created_pool.close()


def _clock() -> datetime:
    return _NOW


async def _disable_days_ago(pool: asyncpg.Pool, oid: str, *, days: int) -> None:
    """Stamp `oid.disabled_at` exactly `days` before `_NOW` - `auth.users.
    mark_disabled` always stamps `now()` (the real one), so this writes the
    column directly, the one thing a test of a day-granularity cutoff needs
    that no public function gives it."""
    await pool.execute(
        "update users set disabled_at = $2 where oid = $1", oid, _NOW - timedelta(days=days)
    )


async def _erasure_log_row(pool: asyncpg.Pool, oid: str) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "select actor, target_kind, target_ids from erasure_log where target_ids = $1", [oid]
    )


async def test_user_disabled_within_retention_is_kept(pool: asyncpg.Pool) -> None:
    await upsert_user(pool, "user-within", tid=_TID, display_name="User Within")
    await mark_disabled(pool, "user-within")
    await _disable_days_ago(pool, "user-within", days=29)

    await _retention_job(pool, retention_days=_RETENTION_DAYS, clock=_clock)

    assert await get_user(pool, "user-within") is not None
    assert await _erasure_log_row(pool, "user-within") is None


async def test_user_disabled_past_retention_is_erased(pool: asyncpg.Pool) -> None:
    await upsert_user(pool, "user-past", tid=_TID, display_name="User Past")
    await mark_disabled(pool, "user-past")
    await _disable_days_ago(pool, "user-past", days=31)

    await _retention_job(pool, retention_days=_RETENTION_DAYS, clock=_clock)

    assert await get_user(pool, "user-past") is None
    row = await _erasure_log_row(pool, "user-past")
    assert row is not None
    assert row["actor"] == _RETENTION_ACTOR
    assert row["target_kind"] == "user"


async def test_re_enabled_user_is_never_erased(pool: asyncpg.Pool) -> None:
    await upsert_user(pool, "user-re-enabled", tid=_TID, display_name="User Re-enabled")
    await mark_disabled(pool, "user-re-enabled")
    await _disable_days_ago(pool, "user-re-enabled", days=31)
    await enable_user(pool, "user-re-enabled", reason="admin: welcomed back")

    await _retention_job(pool, retention_days=_RETENTION_DAYS, clock=_clock)

    assert await get_user(pool, "user-re-enabled") is not None
    assert await _erasure_log_row(pool, "user-re-enabled") is None


async def test_two_rounds_against_the_same_user_erase_only_once(pool: asyncpg.Pool) -> None:
    """Simulates two worker replicas each winning the advisory lock for one round
    (`_run_singleton`'s own guarantee, generic across every `Job` and already
    covered by `tests/worker/test_singleton.py`) by running `_retention_job`
    itself twice in a row: the second round selects nothing, since the first
    round's `storage.postgres.PostgresBackend.erase` already removed `oid`'s
    `users` row entirely - the same idempotency `erase_user`'s own docstring
    gives every one of its callers.
    """
    await upsert_user(pool, "user-twice", tid=_TID, display_name="User Twice")
    await mark_disabled(pool, "user-twice")
    await _disable_days_ago(pool, "user-twice", days=31)

    await _retention_job(pool, retention_days=_RETENTION_DAYS, clock=_clock)
    await _retention_job(pool, retention_days=_RETENTION_DAYS, clock=_clock)

    count = await pool.fetchval(
        "select count(*) from erasure_log where target_ids = $1", ["user-twice"]
    )
    assert count == 1


async def test_retention_job_is_a_noop_without_any_disabled_user(pool: asyncpg.Pool) -> None:
    await upsert_user(pool, "user-enabled", tid=_TID, display_name="User Enabled")

    await _retention_job(pool, retention_days=_RETENTION_DAYS, clock=_clock)

    assert await get_user(pool, "user-enabled") is not None
    assert await pool.fetchval("select count(*) from erasure_log") == 0
