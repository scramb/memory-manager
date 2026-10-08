# SPDX-License-Identifier: AGPL-3.0-only
"""Fixed-window rate limiting on a `SharedState` (#39, #103, #122, ADR-0009 §2).

`RateLimiter` used to hold one `TokenBucket` per key in this process's own
memory - single-replica by design. It now delegates every check to an
injected `auth.shared_state.SharedState`, so the same limit is enforced
across however many replicas share that state: `InMemorySharedState` (the
default, unchanged single-replica behaviour) or `PostgresSharedState` (once
a database is configured, `http.py`'s `lifespan`).

`per_minute`/`burst` turn into one fixed window per key: `limit` calls
(`max(1, floor(burst))`) within `window_seconds` (`burst * 60 / per_minute`)
- the average throughput a token bucket of that capacity and refill rate
would allow, approximated as a fixed window instead because that is what a
shared-state backend can enforce in one round trip, without per-backend
bucket state (`SharedState.window_hit`'s own docstring: a fixed window, not
sliding). `allow()` is async now - every check is a `SharedState` round
trip, not a local in-memory read.

`name` namespaces every key this instance ever hits or peeks (`f"{name}:{key}"`)
- required, not optional, so a call site cannot forget it. Before #122, every
`RateLimiter` built by `http.py`'s `create_app` shared one `SharedState` with no
prefix of its own: `mcp_limiter` and `write_limiter` both hit the exact same
`token:<sha>`/`ip:<addr>` key (`http._request_key`) under different windows, so
a write counted against the MCP budget and vice versa. `name` is part of the
key, not metadata alongside it, so two `RateLimiter`s with different `name`s
can never collide even if a caller passes them the same `key`.
"""

from __future__ import annotations

import math

from memory_manager.auth.shared_state import SharedState

__all__ = ["RateLimiter"]


class RateLimiter:
    """One fixed window per key, `limit` calls per `window_seconds`, backed by `state`
    (a `SharedState` - see the module docstring for why that replaced the old
    per-process `TokenBucket` map).

    `name` namespaces this instance's keys in `state` (module docstring: #122) -
    two `RateLimiter`s sharing one `state` but given different `name`s never see
    each other's hits, even on the same `key`.
    """

    def __init__(self, *, state: SharedState, per_minute: float, burst: float, name: str) -> None:
        if per_minute <= 0 or burst <= 0:
            raise ValueError("per_minute and burst must both be positive")
        if not name:
            raise ValueError("name must be a non-empty rate limiter prefix")
        self._state = state
        self._limit = max(1, math.floor(burst))
        self._window_seconds = burst * 60.0 / per_minute
        self._name = name

    @property
    def name(self) -> str:
        """This instance's own prefix - also the `limiter` label value
        `observability.metrics.record_rate_limit_hit` records a rejection
        under (`http.py`'s call sites read it off the `RateLimiter` they
        just rejected on, rather than naming it a second time)."""
        return self._name

    def _namespaced(self, key: str) -> str:
        return f"{self._name}:{key}"

    async def allow(self, key: str) -> tuple[bool, float]:
        """Whether `key` may make one more call right now.

        `(True, 0.0)` if `key`'s current window is still within `limit`;
        `(False, retry_after)` otherwise, where `retry_after` is the number of
        seconds left until the window resets - the exact value a 429
        response's `Retry-After` reports.
        """
        count, remaining = await self._state.window_hit(
            self._namespaced(key), window_seconds=self._window_seconds
        )
        if count <= self._limit:
            return True, 0.0
        return False, remaining
