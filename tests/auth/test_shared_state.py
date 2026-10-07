# SPDX-License-Identifier: AGPL-3.0-only
"""Contract tests for `auth.shared_state.SharedState` (#103), run against both
implementations: `InMemorySharedState` and `PostgresSharedState` (`pool`,
`tests/auth/conftest.py`).

Real sleeps, no fake clock - unlike `auth.ratelimit.RateLimiter`'s own unit
tests (`test_limits_audit.py`), this suite is also what `PostgresSharedState`
has to pass, and Postgres's own clock (`now()`) is not something a test can
fake.

`InMemorySharedState`'s bounded-LRU behaviour (moved here from
`test_limits_audit.py`, #103) is not part of the shared contract:
`PostgresSharedState` has no in-process bound to test - its `rate_limits`
table and the `oauth_pending` rows it writes grow with distinct keys until
something else sweeps them (`auth.store.cleanup`/#106, out of scope here).
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
import pytest_asyncio

from memory_manager.auth.shared_state import InMemorySharedState, PostgresSharedState, SharedState
from memory_manager.auth.store import ClientSecretCipher

#: A Fernet key generated once for this test module only - not a credential
#: protecting anything real, just what `PostgresSharedState`'s `cipher` requires.
_CIPHER_KEY = "f2F7cFcySJJZnWoa6ZFYlsU-MQSxl8_Fw6bT_DLoLJ0="


def _postgres_state(pool: asyncpg.Pool) -> PostgresSharedState:
    return PostgresSharedState(pool, cipher=ClientSecretCipher(_CIPHER_KEY))


@pytest_asyncio.fixture(params=["in_memory", "postgres"])
async def shared_state(request: pytest.FixtureRequest, pool: asyncpg.Pool) -> SharedState:
    if request.param == "in_memory":
        return InMemorySharedState()
    return _postgres_state(pool)


# === Fixed-window counters ==========================================================


class TestWindowContract:
    async def test_first_hit_for_a_key_starts_the_count_at_one(
        self, shared_state: SharedState
    ) -> None:
        count, remaining = await shared_state.window_hit("k", window_seconds=60.0)
        assert count == 1
        assert 0 < remaining <= 60.0

    async def test_repeated_hits_within_the_window_accumulate(
        self, shared_state: SharedState
    ) -> None:
        await shared_state.window_hit("k", window_seconds=60.0)
        await shared_state.window_hit("k", window_seconds=60.0)
        count, _ = await shared_state.window_hit("k", window_seconds=60.0)
        assert count == 3

    async def test_two_keys_are_isolated_from_each_other(self, shared_state: SharedState) -> None:
        await shared_state.window_hit("a", window_seconds=60.0)
        count_a, _ = await shared_state.window_hit("a", window_seconds=60.0)
        count_b, _ = await shared_state.window_hit("b", window_seconds=60.0)
        assert count_a == 2
        assert count_b == 1

    async def test_peek_does_not_count_as_a_hit(self, shared_state: SharedState) -> None:
        await shared_state.window_hit("k", window_seconds=60.0)
        peeked, _ = await shared_state.window_peek("k", window_seconds=60.0)
        assert peeked == 1
        again, _ = await shared_state.window_hit("k", window_seconds=60.0)
        assert again == 2

    async def test_peek_on_a_key_with_no_window_yet_is_zero(
        self, shared_state: SharedState
    ) -> None:
        count, remaining = await shared_state.window_peek("never-hit", window_seconds=60.0)
        assert count == 0
        assert remaining == 60.0

    async def test_window_rolls_over_once_it_expires(self, shared_state: SharedState) -> None:
        await shared_state.window_hit("k", window_seconds=1.0)
        await shared_state.window_hit("k", window_seconds=1.0)
        await asyncio.sleep(1.2)

        count, _ = await shared_state.window_hit("k", window_seconds=1.0)
        assert count == 1

    async def test_n_concurrent_hits_on_one_key_count_exactly_n(
        self, shared_state: SharedState
    ) -> None:
        results = await asyncio.gather(
            *(shared_state.window_hit("concurrent", window_seconds=60.0) for _ in range(20))
        )
        assert sorted(count for count, _ in results) == list(range(1, 21))


# === Pending login state =============================================================


class TestPendingContract:
    async def test_put_then_take_returns_the_payload(self, shared_state: SharedState) -> None:
        await shared_state.put_pending("state-1", "payload-1", ttl_seconds=60.0)
        assert await shared_state.take_pending("state-1") == "payload-1"

    async def test_take_is_single_use(self, shared_state: SharedState) -> None:
        await shared_state.put_pending("state-1", "payload-1", ttl_seconds=60.0)
        await shared_state.take_pending("state-1")
        assert await shared_state.take_pending("state-1") is None

    async def test_take_on_an_unknown_key_is_none(self, shared_state: SharedState) -> None:
        assert await shared_state.take_pending("never-put") is None

    async def test_pending_expires_after_its_ttl(self, shared_state: SharedState) -> None:
        await shared_state.put_pending("state-1", "payload-1", ttl_seconds=0.5)
        await asyncio.sleep(0.8)
        assert await shared_state.take_pending("state-1") is None


# === PostgresSharedState-specific: at-rest secrecy and the cipher requirement ========


class TestPostgresSharedStateSecrecy:
    async def test_the_stored_row_carries_neither_plaintext_key_nor_plaintext_payload(
        self, pool: asyncpg.Pool
    ) -> None:
        state = _postgres_state(pool)
        key = "a-very-recognizable-state-value"
        secret_payload = "a-secret-payload"  # noqa: S105 - a fake test credential
        await state.put_pending(key, secret_payload, ttl_seconds=60.0)

        row = await pool.fetchrow(
            "select id_hash, params from oauth_pending where kind = 'login_pending'"
        )
        assert row is not None
        assert row["id_hash"] != key
        assert secret_payload not in row["params"]

    async def test_put_pending_without_a_cipher_refuses(self, pool: asyncpg.Pool) -> None:
        state = PostgresSharedState(pool)
        with pytest.raises(RuntimeError, match="cipher"):
            await state.put_pending("k", "payload", ttl_seconds=60.0)


# === InMemorySharedState-specific: bounded LRU (moved from test_limits_audit.py) ====


class TestInMemorySharedStateBoundedLru:
    async def test_a_third_key_evicts_the_least_recently_used_one_beyond_max_keys(
        self,
    ) -> None:
        state = InMemorySharedState(max_keys=2)

        await state.window_hit("a", window_seconds=60.0)
        await state.window_hit("b", window_seconds=60.0)
        await state.window_hit("c", window_seconds=60.0)  # evicts "a" (least recently used)

        # "a" is a brand-new window now - its previous count is gone.
        count, _ = await state.window_hit("a", window_seconds=60.0)
        assert count == 1

    async def test_revisiting_a_key_keeps_it_from_being_evicted_next(self) -> None:
        state = InMemorySharedState(max_keys=2)

        await state.window_hit("a", window_seconds=60.0)
        await state.window_hit("b", window_seconds=60.0)
        await state.window_hit("a", window_seconds=60.0)  # refreshes "a"'s position
        await state.window_hit("c", window_seconds=60.0)  # evicts "b", not "a"

        count_a, _ = await state.window_hit("a", window_seconds=60.0)
        assert count_a == 3  # "a" was never evicted
