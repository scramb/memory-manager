# SPDX-License-Identifier: AGPL-3.0-only
"""Shared state across replicas: rate-limit windows and pending OIDC login state
(ADR-0009 §2, #103).

`SharedState` is the one interface `auth.ratelimit.RateLimiter`,
`auth.login_password.PasswordAuthenticator` and `auth.login_oidc.
OidcAuthenticator` depend on instead of their own process-local structures: a
fixed window per key (`window_hit`/`window_peek`) plus single-use,
TTL-bounded pending state (`put_pending`/`take_pending`). Two implementations
today, both passing the same contract suite (`tests/auth/
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

A third implementation (Valkey, #104) is deliberately not anticipated by any
Postgres-specific type in `SharedState` itself - `asyncpg`/`pool` appear only
inside `PostgresSharedState`.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

import asyncpg

from memory_manager.auth.store import ClientSecretCipher

__all__ = ["InMemorySharedState", "PostgresSharedState", "SharedState"]

_DEFAULT_MAX_KEYS = 10_000

#: `oauth_pending.kind` for a `PostgresSharedState` pending login - anything other
#: than `'authorize'`, which `auth.store.save_pending`/`get_pending` own exclusively.
_PENDING_KIND = "login_pending"


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
