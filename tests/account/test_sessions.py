# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `memory_manager.account.sessions` (#228).

A fake clock (same pattern as `tests/auth/test_limits_audit.py`'s `_FakeClock`,
returning a `datetime` instead of a `float`) drives the idle and absolute expiry
checks deterministically - no real sleeping for a 30-minute or an 8-hour window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from memory_manager.account import sessions
from memory_manager.auth import store, users

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeClock:
    def __init__(self, now: datetime = _CREATED) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _disabled_user(pool: asyncpg.Pool, oid: str) -> None:
    await users.upsert_user(pool, oid, tid="tid-1", display_name="Alice")
    await users.mark_disabled(pool, oid)


class TestCreateAndLookup:
    async def test_lookup_returns_the_session_created(self, pool: asyncpg.Pool) -> None:
        clock = _FakeClock()
        session_id = await sessions.create(
            pool, subject="admin", login_mode="password", clock=clock
        )

        info = await sessions.lookup(pool, session_id, clock=clock)

        assert info is not None
        assert info.subject == "admin"
        assert info.oid is None
        assert info.roles == ()
        assert info.login_mode == "password"

    async def test_lookup_returns_none_for_an_unknown_session_id(self, pool: asyncpg.Pool) -> None:
        assert await sessions.lookup(pool, "mms_does-not-exist") is None

    async def test_raw_session_id_is_never_stored(self, pool: asyncpg.Pool) -> None:
        session_id = await sessions.create(pool, subject="admin", login_mode="password")

        rows = await pool.fetch("select session_hash from account_sessions")

        assert len(rows) == 1
        assert rows[0]["session_hash"] != session_id
        assert session_id not in rows[0]["session_hash"]

    async def test_create_rejects_an_unknown_login_mode(self, pool: asyncpg.Pool) -> None:
        with pytest.raises(ValueError, match="login_mode"):
            await sessions.create(pool, subject="admin", login_mode="carrier-pigeon")


class TestIdleExpiry:
    async def test_lookup_slides_the_idle_window_forward(self, pool: asyncpg.Pool) -> None:
        clock = _FakeClock()
        session_id = await sessions.create(
            pool, subject="admin", login_mode="password", clock=clock
        )

        clock.now += timedelta(minutes=29)
        assert await sessions.lookup(pool, session_id, clock=clock) is not None

        # Idle again for another 29 minutes from the *second* lookup - still inside
        # the 30-minute window because the first lookup slid `last_seen_at` forward.
        clock.now += timedelta(minutes=29)
        assert await sessions.lookup(pool, session_id, clock=clock) is not None

    async def test_lookup_returns_none_after_30_minutes_idle(self, pool: asyncpg.Pool) -> None:
        clock = _FakeClock()
        session_id = await sessions.create(
            pool, subject="admin", login_mode="password", clock=clock
        )

        clock.now += timedelta(minutes=31)

        assert await sessions.lookup(pool, session_id, clock=clock) is None


class TestAbsoluteExpiry:
    async def test_lookup_returns_none_after_8_hours_despite_activity(
        self, pool: asyncpg.Pool
    ) -> None:
        clock = _FakeClock()
        session_id = await sessions.create(
            pool, subject="admin", login_mode="password", clock=clock
        )

        # Stay active well inside the idle window, every 10 minutes, right up to the
        # 8-hour absolute lifetime - activity alone must not extend it.
        for _ in range(47):
            clock.now += timedelta(minutes=10)
            assert await sessions.lookup(pool, session_id, clock=clock) is not None

        clock.now += timedelta(minutes=10)
        assert await sessions.lookup(pool, session_id, clock=clock) is None


class TestDisabledUser:
    async def test_a_disabled_users_session_is_rejected(self, pool: asyncpg.Pool) -> None:
        clock = _FakeClock()
        await _disabled_user(pool, "oid-alice")
        session_id = await sessions.create(
            pool,
            subject="alice",
            login_mode="entra",
            oid="oid-alice",
            roles=["Memory.User"],
            clock=clock,
        )

        assert await sessions.lookup(pool, session_id, clock=clock) is None

    async def test_an_enabled_users_session_still_works(self, pool: asyncpg.Pool) -> None:
        clock = _FakeClock()
        await users.upsert_user(pool, "oid-bob", tid="tid-1", display_name="Bob")
        session_id = await sessions.create(
            pool,
            subject="bob",
            login_mode="entra",
            oid="oid-bob",
            roles=["Memory.User"],
            clock=clock,
        )

        info = await sessions.lookup(pool, session_id, clock=clock)

        assert info is not None
        assert info.oid == "oid-bob"
        assert info.roles == ("Memory.User",)


class TestRevoke:
    async def test_revoke_removes_the_session(self, pool: asyncpg.Pool) -> None:
        session_id = await sessions.create(pool, subject="admin", login_mode="password")

        removed = await sessions.revoke(pool, session_id)

        assert removed is True
        assert await sessions.lookup(pool, session_id) is None

    async def test_revoke_reports_false_for_an_unknown_session(self, pool: asyncpg.Pool) -> None:
        assert await sessions.revoke(pool, "mms_does-not-exist") is False


class TestCsrf:
    async def test_verify_csrf_accepts_the_matching_token(self) -> None:
        token = sessions.csrf_token("mms_session-1", "delete-my-memory")

        assert sessions.verify_csrf("mms_session-1", "delete-my-memory", token) is True

    async def test_verify_csrf_rejects_a_token_for_a_different_form(self) -> None:
        token = sessions.csrf_token("mms_session-1", "delete-my-memory")

        assert sessions.verify_csrf("mms_session-1", "export-my-memory", token) is False

    async def test_verify_csrf_rejects_a_token_for_a_different_session(self) -> None:
        token = sessions.csrf_token("mms_session-1", "delete-my-memory")

        assert sessions.verify_csrf("mms_session-2", "delete-my-memory", token) is False

    async def test_verify_csrf_rejects_garbage(self) -> None:
        assert sessions.verify_csrf("mms_session-1", "delete-my-memory", "not-a-token") is False


class TestCleanup:
    async def test_cleanup_removes_only_absolute_expired_sessions(self, pool: asyncpg.Pool) -> None:
        # `store.cleanup`'s own SQL filters on Postgres's real `now()`, not the
        # injected clock `create`/`lookup` take - so the expired session here is
        # backdated against real wall-clock time, and the live one uses the default
        # (real) clock, unlike every other test in this module.
        past_clock = _FakeClock(now=datetime.now(UTC) - timedelta(hours=9))
        expired_session_id = await sessions.create(
            pool, subject="admin", login_mode="password", clock=past_clock
        )
        live_session_id = await sessions.create(pool, subject="admin", login_mode="password")

        stats = await store.cleanup(pool)

        assert stats.sessions == 1
        assert await pool.fetchval("select count(*) from account_sessions") == 1
        assert await sessions.lookup(pool, expired_session_id) is None
        assert await sessions.lookup(pool, live_session_id) is not None
