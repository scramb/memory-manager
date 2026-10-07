# SPDX-License-Identifier: AGPL-3.0-only
"""Fixed-window rate limiting on a `SharedState` (#39, #103, ADR-0009 §2).

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
"""

from __future__ import annotations

import math

from memory_manager.auth.shared_state import SharedState

__all__ = ["RateLimiter"]


class RateLimiter:
    """One fixed window per key, `limit` calls per `window_seconds`, backed by `state`
    (a `SharedState` - see the module docstring for why that replaced the old
    per-process `TokenBucket` map).
    """

    def __init__(self, *, state: SharedState, per_minute: float, burst: float) -> None:
        if per_minute <= 0 or burst <= 0:
            raise ValueError("per_minute and burst must both be positive")
        self._state = state
        self._limit = max(1, math.floor(burst))
        self._window_seconds = burst * 60.0 / per_minute

    async def allow(self, key: str) -> tuple[bool, float]:
        """Whether `key` may make one more call right now.

        `(True, 0.0)` if `key`'s current window is still within `limit`;
        `(False, retry_after)` otherwise, where `retry_after` is the number of
        seconds left until the window resets - the exact value a 429
        response's `Retry-After` reports.
        """
        count, remaining = await self._state.window_hit(key, window_seconds=self._window_seconds)
        if count <= self._limit:
            return True, 0.0
        return False, remaining
