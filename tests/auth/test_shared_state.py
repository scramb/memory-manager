# SPDX-License-Identifier: AGPL-3.0-only
"""Contract tests for `auth.shared_state.SharedState` (#103, #104), run against all
three implementations: `InMemorySharedState`, `PostgresSharedState` (`pool`,
`tests/auth/conftest.py`) and `ValkeySharedState` (`valkey_url`, `tests/conftest.py`).

Real sleeps, no fake clock - unlike `auth.ratelimit.RateLimiter`'s own unit
tests (`test_limits_audit.py`), this suite is also what `PostgresSharedState`
and `ValkeySharedState` have to pass, and neither backend's own clock is
something a test can fake.

`InMemorySharedState`'s bounded-LRU behaviour (moved here from
`test_limits_audit.py`, #103) is not part of the shared contract:
`PostgresSharedState`/`ValkeySharedState` have no in-process bound to test -
their rows/keys grow with distinct keys until something else sweeps them.
Valkey's own `PEXPIRE` already does that per key; `PostgresSharedState`'s
own `sweep_expired_windows` (#106, exercised below in
`TestPostgresSharedStateSweep`, called periodically by `http.py`'s cleanup
loop) is what plays that role for `rate_limits`.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import sys
from collections.abc import AsyncIterator
from typing import cast

import asyncpg
import pytest
import pytest_asyncio
from redis.asyncio import Redis

from memory_manager.app import Services
from memory_manager.auth.shared_state import (
    InMemorySharedState,
    PostgresSharedState,
    SharedState,
    ValkeySharedState,
    build_valkey_shared_state,
)
from memory_manager.auth.store import ClientSecretCipher
from memory_manager.config import ServerConfig, ServerConfigError
from memory_manager.http import create_app

#: A Fernet key generated once for this test module only - not a credential
#: protecting anything real, just what `PostgresSharedState`'s/`ValkeySharedState`'s
#: `cipher` requires.
_CIPHER_KEY = "f2F7cFcySJJZnWoa6ZFYlsU-MQSxl8_Fw6bT_DLoLJ0="


def _postgres_state(pool: asyncpg.Pool) -> PostgresSharedState:
    return PostgresSharedState(pool, cipher=ClientSecretCipher(_CIPHER_KEY))


def _unique_valkey_prefixes() -> tuple[str, str]:
    """A fresh `(window_prefix, pending_prefix)` pair - every test that talks to
    Valkey gets its own, so tests sharing the one `MM_TEST_VALKEY_URL` instance
    never see each other's keys (the role a fresh database plays for Postgres)."""
    unique = f"test:{secrets.token_hex(8)}:"
    return f"{unique}window:", f"{unique}pending:"


def _valkey_state(url: str) -> ValkeySharedState:
    window_prefix, pending_prefix = _unique_valkey_prefixes()
    return build_valkey_shared_state(
        url,
        cipher=ClientSecretCipher(_CIPHER_KEY),
        window_prefix=window_prefix,
        pending_prefix=pending_prefix,
    )


@pytest.fixture
def _postgres_shared_state(pool: asyncpg.Pool) -> SharedState:
    return _postgres_state(pool)


@pytest_asyncio.fixture
async def _valkey_shared_state(valkey_url: str) -> AsyncIterator[SharedState]:
    state = _valkey_state(valkey_url)
    try:
        yield state
    finally:
        await state.aclose()


@pytest.fixture(params=["in_memory", "postgres", "valkey"])
def shared_state(request: pytest.FixtureRequest) -> SharedState:
    """Deliberately a plain (non-async) fixture that resolves `_postgres_shared_state`/
    `_valkey_shared_state` through `request.getfixturevalue`, conditionally, rather
    than requesting either as a direct parameter (which pytest would resolve for
    every param, including `in_memory` - defeating the point): `request.
    getfixturevalue` on an async fixture only works from *outside* an already-running
    event loop, so this has to be a plain fixture doing the branching, not an async
    one calling `request.getfixturevalue` from inside its own coroutine body
    (`pytest-asyncio`'s `_wrap_asyncgen_fixture` raises "Runner.run() cannot be called
    from a running event loop" the moment it tries that nested lookup) - `tests/
    auth/conftest.py`'s `pool` and `valkey_url` stay exactly as direct parameters of
    `_postgres_shared_state`/`_valkey_shared_state` themselves, the normal, non-nested
    shape that already works.
    """
    if request.param == "in_memory":
        return InMemorySharedState()
    # Only resolved for its own branch - an in-memory case must not depend on a
    # reachable Postgres or Valkey, and neither backend depends on the other.
    return cast(SharedState, request.getfixturevalue(f"_{request.param}_shared_state"))


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


# === PostgresSharedState-specific: the periodic rate-limit sweep (#106) =============


class TestPostgresSharedStateSweep:
    async def test_sweep_removes_an_expired_window_but_keeps_a_running_one(
        self, pool: asyncpg.Pool
    ) -> None:
        """A row backdated well past the threshold is gone afterwards; a window
        `window_hit` just opened (`window_start` effectively `now()`) survives -
        the invariant `sweep_expired_windows`'s docstring states: never shorten a
        window still being counted against."""
        state = _postgres_state(pool)
        await pool.execute(
            "insert into rate_limits (key, window_start, count) "
            "values ('stale', now() - interval '2 hours', 3)"
        )
        await state.window_hit("running", window_seconds=60.0)

        removed = await state.sweep_expired_windows(older_than_seconds=3600.0)

        assert removed == 1
        remaining = {row["key"] for row in await pool.fetch("select key from rate_limits")}
        assert remaining == {"running"}


# === ValkeySharedState-specific: at-rest secrecy and the cipher requirement ==========


class TestValkeySharedStateSecrecy:
    async def test_the_stored_value_carries_neither_plaintext_key_nor_plaintext_payload(
        self, valkey_url: str
    ) -> None:
        window_prefix, pending_prefix = _unique_valkey_prefixes()
        state = build_valkey_shared_state(
            valkey_url,
            cipher=ClientSecretCipher(_CIPHER_KEY),
            window_prefix=window_prefix,
            pending_prefix=pending_prefix,
        )
        key = "a-very-recognizable-state-value"
        secret_payload = "a-secret-payload"  # noqa: S105 - a fake test credential
        try:
            await state.put_pending(key, secret_payload, ttl_seconds=60.0)

            client = Redis.from_url(valkey_url, decode_responses=True)
            try:
                keys = [found async for found in client.scan_iter(match=f"{pending_prefix}*")]
                assert len(keys) == 1
                assert key not in keys[0]
                value = await client.get(keys[0])
                assert value is not None
                assert secret_payload not in value
            finally:
                await client.aclose()
        finally:
            await state.aclose()

    async def test_put_pending_without_a_cipher_refuses(self, valkey_url: str) -> None:
        window_prefix, pending_prefix = _unique_valkey_prefixes()
        state = build_valkey_shared_state(
            valkey_url, window_prefix=window_prefix, pending_prefix=pending_prefix
        )
        try:
            with pytest.raises(RuntimeError, match="cipher"):
                await state.put_pending("k", "payload", ttl_seconds=60.0)
        finally:
            await state.aclose()


# === create_app: VALKEY_URL set but the 'redis' package is not installed ============


class TestValkeyExtraMissing:
    async def test_create_app_refuses_with_valkey_url_but_without_redis_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No `MM_TEST_VALKEY_URL`/Postgres dependency - `redis` is simulated missing by
        making `import redis`/`import redis.asyncio` fail, the standard
        `sys.modules[name] = None` trick (not actually uninstalling the dev-only
        dependency `pyproject.toml`'s `dev` group installs for the tests above)."""
        monkeypatch.setitem(sys.modules, "redis", None)
        monkeypatch.setitem(sys.modules, "redis.asyncio", None)

        @contextlib.asynccontextmanager
        async def unreachable_services_factory() -> AsyncIterator[Services]:
            raise AssertionError(
                "services_factory must not be called: create_app has to raise before lifespan"
            )
            yield  # pragma: no cover - unreachable, only here to type this as a generator

        config = ServerConfig(valkey_url="redis://localhost:6379")
        with pytest.raises(ServerConfigError, match="valkey"):
            create_app(unreachable_services_factory, config)


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
