# SPDX-License-Identifier: AGPL-3.0-only
"""Shared state across replicas: rate-limit windows and pending OIDC login state
(ADR-0009 §2, #103).

`SharedState` is the one interface `auth.ratelimit.RateLimiter`,
`auth.login_password.PasswordAuthenticator` and `auth.login_oidc.
OidcAuthenticator` depend on instead of their own process-local structures: a
fixed window per key (`window_hit`/`window_peek`) plus single-use,
TTL-bounded pending state (`put_pending`/`take_pending`). Three
implementations today, all passing the same contract suite (`tests/auth/
test_shared_state.py`):

- `InMemorySharedState`: today's single-replica behaviour, unchanged -
  everything lives in this process, bounded to `max_keys` entries per kind
  of key (windows, pending), least-recently-used evicted first, the same
  policy `auth.ratelimit.RateLimiter`'s old `TokenBucket` map used - with an
  injectable clock for tests. `http.py`'s `create_app` builds one as the
  default, before it even knows whether a database is configured.
- `PostgresSharedState`: the shared backend once `services.pool` exists
  (`http.py`'s `lifespan`). Rate-limit windows live in the UNLOGGED
  `rate_limits` table (`db/migrations/0006_shared_state.sql`) - loss-tolerant
  by design, one UPSERT per hit, atomic under concurrent hits on the same
  key because Postgres's own row lock on the conflicting tuple serializes
  them. Pending login state reuses the existing `oauth_pending` table (the
  same migration adds `kind`, so these rows - `kind != 'authorize'` - are
  never confused with `auth.store.save_pending`'s own `/authorize`-parked
  ones, which `auth.store.get_pending` now filters to `kind = 'authorize'`
  only): the key is hashed with SHA-256 before it ever touches a query,
  never stored in plaintext (CLAUDE.md: "token hashes only"), and the
  payload is encrypted at rest with the same `ClientSecretCipher` a DCR
  client's `client_secret` already uses (`auth.store`) - `put_pending`
  refuses to run without one, rather than ever storing a `code_verifier`/
  `nonce` pair in the clear.

- `ValkeySharedState`: the preferred shared backend once `VALKEY_URL` is
  configured (`http.py`'s `create_app`, ADR-0009 §2, #104) - takes precedence
  over `PostgresSharedState` when both are configured. Rate-limit windows are
  one key per `key`, `INCR`ed and given a TTL of `window_seconds` with
  `PEXPIRE ... NX` (only the first hit of a window sets it, inside the same
  `MULTI`/`EXEC` pipeline as the `INCR`, so a window key is never left
  without a TTL even under concurrent first hits) - loss-tolerant by design,
  same as `rate_limits`: Valkey runs without persistence (ADR-0009 §2), so a
  restart merely resets open windows and drops pending logins in progress,
  never silently wrong data. Pending login state is `SET ... PX` (the TTL)
  under a key hashed with SHA-256 (`_hash_key`, same as `PostgresSharedState`
  - CLAUDE.md: "token hashes only"; rate-limit window keys, unlike pending
  ones, carry no secret and stay plain, same as `rate_limits.key`), payload
  encrypted at rest with the same `ClientSecretCipher` `PostgresSharedState`
  uses - `put_pending` refuses the same way without one; `take_pending` is
  `GETDEL`, single-use by construction. `redis.asyncio.Redis` is imported
  lazily, inside `build_valkey_shared_state`, never at module import time -
  the `redis` package is the optional `valkey` extra (CLAUDE.md: few
  dependencies), not installed unless a deployment actually sets
  `VALKEY_URL`; `http.py`'s `create_app` turns the resulting `ImportError`
  into a `ServerConfigError` naming `VALKEY_URL` and the extra, rather than
  falling back to Postgres or in-process state silently.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

import asyncpg

from memory_manager.auth.store import ClientSecretCipher

if TYPE_CHECKING:
    from redis.asyncio import Redis

__all__ = [
    "InMemorySharedState",
    "PostgresSharedState",
    "SharedState",
    "ValkeySharedState",
    "build_valkey_shared_state",
]

_DEFAULT_MAX_KEYS = 10_000

#: `oauth_pending.kind` for a `PostgresSharedState` pending login - anything other
#: than `'authorize'`, which `auth.store.save_pending`/`get_pending` own exclusively.
_PENDING_KIND = "login_pending"

#: Default key prefixes `ValkeySharedState` namespaces its two kinds of key under -
#: configurable so a test (or a deployment sharing one Valkey instance across more
#: than this server) can give each run its own, isolated prefix.
_DEFAULT_VALKEY_WINDOW_PREFIX = "mm:window:"
_DEFAULT_VALKEY_PENDING_PREFIX = "mm:pending:"


def _evict_lru_if_needed[V](keys: OrderedDict[str, V], max_keys: int) -> None:
    while len(keys) > max_keys:
        keys.popitem(last=False)


class SharedState(Protocol):
    """Fixed-window counters plus single-use, TTL-bounded pending state, shared
    across however many replicas use the same backend instance."""

    async def window_hit(self, key: str, *, window_seconds: float) -> tuple[int, float]:
        """Record one hit for `key`'s fixed window of `window_seconds`.

        The window starts at the first hit for `key` and resets once
        `window_seconds` have passed since it started - a fixed window, never
        a sliding one. Returns the count *after* this hit and the number of
        seconds left until the window resets, for the caller to compare
        against its own limit and build a `Retry-After` header from.
        """
        ...

    async def window_peek(self, key: str, *, window_seconds: float) -> tuple[int, float]:
        """`window_hit`'s return shape, without recording a hit - `(0, window_seconds)`
        for a key with no window open yet, or one that has already reset."""
        ...

    async def put_pending(self, key: str, payload: str, *, ttl_seconds: float) -> None:
        """Park `payload` under `key` until `take_pending` consumes it or `ttl_seconds`
        pass, whichever comes first."""
        ...

    async def take_pending(self, key: str) -> str | None:
        """The `payload` parked under `key`, consumed in the same step (single use) -
        `None` if `key` is unknown, already taken, or expired."""
        ...


@dataclass
class _Window:
    start: float
    count: int


@dataclass
class _Pending:
    payload: str
    expires_at: float


class InMemorySharedState:
    """Today's single-replica behaviour: every window and every pending entry lives
    in this process - `http.py`'s default, before it is even known whether a
    database is configured, and what every test that is not specifically about
    `PostgresSharedState` runs against.

    `clock` defaults to `time.monotonic` (never wall-clock time, which can jump
    backwards); tests inject a fake one. `max_keys` bounds windows and pending
    entries independently, least-recently-used evicted first - the same trade-off
    `auth.ratelimit.RateLimiter`'s old `TokenBucket` map made, now one level down.
    """

    def __init__(
        self, *, max_keys: int = _DEFAULT_MAX_KEYS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._windows: OrderedDict[str, _Window] = OrderedDict()
        self._pending: OrderedDict[str, _Pending] = OrderedDict()

    async def window_hit(self, key: str, *, window_seconds: float) -> tuple[int, float]:
        now = self._clock()
        window = self._windows.get(key)
        if window is None or now - window.start >= window_seconds:
            window = _Window(start=now, count=1)
            self._windows[key] = window
            _evict_lru_if_needed(self._windows, self._max_keys)
        else:
            window.count += 1
            self._windows.move_to_end(key)
        return window.count, max(0.0, window_seconds - (now - window.start))

    async def window_peek(self, key: str, *, window_seconds: float) -> tuple[int, float]:
        now = self._clock()
        window = self._windows.get(key)
        if window is None or now - window.start >= window_seconds:
            return 0, window_seconds
        return window.count, max(0.0, window_seconds - (now - window.start))

    async def put_pending(self, key: str, payload: str, *, ttl_seconds: float) -> None:
        now = self._clock()
        self._pending[key] = _Pending(payload=payload, expires_at=now + ttl_seconds)
        self._pending.move_to_end(key)
        _evict_lru_if_needed(self._pending, self._max_keys)

    async def take_pending(self, key: str) -> str | None:
        entry = self._pending.pop(key, None)
        if entry is None:
            return None
        if self._clock() >= entry.expires_at:
            return None
        return entry.payload


class PostgresSharedState:
    """The shared backend once a database is configured (`http.py`'s `lifespan`,
    `services.pool is not None`).

    `cipher` is required only for `put_pending`/`take_pending` - pending login state
    carries a `code_verifier` and a `nonce`, credentials worth encrypting at rest,
    unlike a bare rate-limit count; `window_hit`/`window_peek` never need one.
    """

    def __init__(self, pool: asyncpg.Pool, *, cipher: ClientSecretCipher | None = None) -> None:
        self._pool = pool
        self._cipher = cipher

    async def window_hit(self, key: str, *, window_seconds: float) -> tuple[int, float]:
        interval = timedelta(seconds=window_seconds)
        row = await self._pool.fetchrow(
            """
            insert into rate_limits (key, window_start, count)
            values ($1, now(), 1)
            on conflict (key) do update set
                window_start = case
                    when now() - rate_limits.window_start >= $2 then now()
                    else rate_limits.window_start
                end,
                count = case
                    when now() - rate_limits.window_start >= $2 then 1
                    else rate_limits.count + 1
                end
            returning count,
                greatest(0, extract(epoch from ($2 - (now() - window_start))))::float8
                    as remaining
            """,
            key,
            interval,
        )
        if row is None:  # pragma: no cover - defensive: an upsert always returns one row
            raise RuntimeError("window_hit's upsert returned no row")
        return row["count"], row["remaining"]

    async def window_peek(self, key: str, *, window_seconds: float) -> tuple[int, float]:
        interval = timedelta(seconds=window_seconds)
        row = await self._pool.fetchrow(
            """
            select count,
                greatest(0, extract(epoch from ($2 - (now() - window_start))))::float8
                    as remaining
            from rate_limits
            where key = $1 and now() - window_start < $2
            """,
            key,
            interval,
        )
        if row is None:
            return 0, window_seconds
        return row["count"], row["remaining"]

    async def put_pending(self, key: str, payload: str, *, ttl_seconds: float) -> None:
        if self._cipher is None:
            raise RuntimeError(
                "PostgresSharedState.put_pending requires a cipher "
                "(OAUTH_CLIENT_SECRET_KEY) - refuses to store pending login state "
                "unencrypted"
            )
        encrypted = self._cipher.encrypt(payload)
        await self._pool.execute(
            """
            insert into oauth_pending (id_hash, client_id, kind, params, expires_at)
            values ($1, null, $2, $3, now() + $4)
            """,
            _hash_key(key),
            _PENDING_KIND,
            json.dumps({"payload": encrypted}),
            timedelta(seconds=ttl_seconds),
        )

    async def take_pending(self, key: str) -> str | None:
        row = await self._pool.fetchrow(
            """
            delete from oauth_pending
            where id_hash = $1 and kind = $2 and expires_at > now()
            returning params
            """,
            _hash_key(key),
            _PENDING_KIND,
        )
        if row is None:
            return None
        if self._cipher is None:  # pragma: no cover - defensive: put_pending already refused
            return None
        params = json.loads(row["params"])
        return self._cipher.decrypt(params["payload"])


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class ValkeySharedState:
    """The preferred shared backend once `VALKEY_URL` is configured (`http.py`'s
    `create_app`, ADR-0009 §2, #104) - see the module docstring for the key shapes.

    `client` is a connected `redis.asyncio.Redis` (built by `build_valkey_shared_state`,
    the only place that imports `redis` at all); `window_prefix`/`pending_prefix` let a
    deployment (or a test) namespace this instance's keys separately from anything
    else sharing the same Valkey, the same role `rate_limits`/`oauth_pending` being
    dedicated tables plays for `PostgresSharedState`. `cipher` is required only for
    `put_pending`/`take_pending`, exactly like `PostgresSharedState`.
    """

    def __init__(
        self,
        client: Redis,
        *,
        cipher: ClientSecretCipher | None = None,
        window_prefix: str = _DEFAULT_VALKEY_WINDOW_PREFIX,
        pending_prefix: str = _DEFAULT_VALKEY_PENDING_PREFIX,
    ) -> None:
        self._client = client
        self._cipher = cipher
        self._window_prefix = window_prefix
        self._pending_prefix = pending_prefix

    async def window_hit(self, key: str, *, window_seconds: float) -> tuple[int, float]:
        window_key = f"{self._window_prefix}{key}"
        ttl_ms = max(1, round(window_seconds * 1000))
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(window_key)
            # `NX`: only the first hit of a window sets the TTL - inside the same
            # `MULTI`/`EXEC` as the `INCR` above, so a window key is never left
            # without a TTL even under concurrent first hits on the same key.
            pipe.pexpire(window_key, ttl_ms, nx=True)
            pipe.pttl(window_key)
            count, _, remaining_ms = await pipe.execute()
        if int(remaining_ms) < 0:  # pragma: no cover - defensive: the NX PEXPIRE above
            return int(count), window_seconds  # always sets a TTL before this PTTL runs
        return int(count), int(remaining_ms) / 1000.0

    async def window_peek(self, key: str, *, window_seconds: float) -> tuple[int, float]:
        window_key = f"{self._window_prefix}{key}"
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.get(window_key)
            pipe.pttl(window_key)
            value, remaining_ms = await pipe.execute()
        if value is None:
            return 0, window_seconds
        remaining = int(remaining_ms) / 1000.0 if int(remaining_ms) >= 0 else window_seconds
        return int(value), remaining

    async def put_pending(self, key: str, payload: str, *, ttl_seconds: float) -> None:
        if self._cipher is None:
            raise RuntimeError(
                "ValkeySharedState.put_pending requires a cipher (OAUTH_CLIENT_SECRET_KEY) "
                "- refuses to store pending login state unencrypted"
            )
        encrypted = self._cipher.encrypt(payload)
        pending_key = f"{self._pending_prefix}{_hash_key(key)}"
        ttl_ms = max(1, round(ttl_seconds * 1000))
        await self._client.set(pending_key, encrypted, px=ttl_ms)

    async def take_pending(self, key: str) -> str | None:
        pending_key = f"{self._pending_prefix}{_hash_key(key)}"
        encrypted = await self._client.getdel(pending_key)
        if encrypted is None:
            return None
        if isinstance(encrypted, bytes):  # pragma: no cover - decode_responses=True means str
            encrypted = encrypted.decode("utf-8")
        if self._cipher is None:  # pragma: no cover - defensive: put_pending already refused
            return None
        return self._cipher.decrypt(encrypted)

    async def aclose(self) -> None:
        """Closes the underlying connection pool - called from `http.py`'s `lifespan`,
        the same place that closes an `OidcAuthenticator`'s `httpx.AsyncClient`."""
        await self._client.aclose()


def build_valkey_shared_state(
    url: str,
    *,
    cipher: ClientSecretCipher | None = None,
    window_prefix: str = _DEFAULT_VALKEY_WINDOW_PREFIX,
    pending_prefix: str = _DEFAULT_VALKEY_PENDING_PREFIX,
) -> ValkeySharedState:
    """A `ValkeySharedState` connected to `url` (`VALKEY_URL`) - does not connect yet,
    `redis.asyncio.Redis.from_url` only builds a lazy connection pool.

    Raises `ImportError` if the `redis` package is not installed - the `valkey` extra
    (`memory-manager[valkey]`) is optional (CLAUDE.md: few dependencies); `http.py`'s
    `create_app` turns that into a `ServerConfigError` naming `VALKEY_URL` and the
    extra, rather than falling back to Postgres or in-process state silently. This is
    the one place in this module that imports `redis` at all - the rest of
    `ValkeySharedState` only ever sees the `Redis` instance handed to it.
    """
    from redis.asyncio import Redis as _Redis

    client = _Redis.from_url(url, decode_responses=True)
    return ValkeySharedState(
        client, cipher=cipher, window_prefix=window_prefix, pending_prefix=pending_prefix
    )
