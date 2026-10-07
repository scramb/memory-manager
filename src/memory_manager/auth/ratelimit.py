# SPDX-License-Identifier: AGPL-3.0-only
"""In-process token-bucket rate limiting (#39).

Single replica by design (CLAUDE.md: few dependencies, no external service
for something this small) - every bucket lives in this process's memory, so
running more than one replica of the HTTP server gives each its own,
independent limit rather than a shared one. Good enough for the deployment
shape this project targets (one server process per vault); a distributed
limiter is explicitly out of scope for this task.

`RateLimiter` holds one `TokenBucket` per key (a hashed bearer token or a
client IP, chosen by the caller - see `memory_manager.http`'s middleware),
bounded to `max_keys` entries so an attacker cycling through fake keys
cannot grow this unboundedly: the least-recently-used key is evicted first,
the same trade-off a bounded LRU cache always makes.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["RateLimiter", "TokenBucket"]

_DEFAULT_MAX_KEYS = 10_000


@dataclass
class TokenBucket:
    """A single token bucket: `capacity` tokens, refilled at `refill_per_second`.

    `tokens`/`updated_at` are the bucket's mutable state, advanced lazily by
    `take()` to whatever `now` the caller passes in - no background timer,
    so an idle bucket costs nothing between requests.
    """

    capacity: float
    refill_per_second: float
    tokens: float
    updated_at: float

    def take(self, now: float, *, cost: float = 1.0) -> tuple[bool, float]:
        """Try to take `cost` tokens as of `now`.

        Returns `(True, 0.0)` if the bucket had enough tokens (already
        deducted); `(False, retry_after)` otherwise, where `retry_after` is
        the number of seconds until the bucket would hold `cost` tokens
        again - the exact value a 429 response's `Retry-After` reports.
        """
        elapsed = max(0.0, now - self.updated_at)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.updated_at = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True, 0.0
        missing = cost - self.tokens
        retry_after = (
            missing / self.refill_per_second if self.refill_per_second > 0 else float("inf")
        )
        return False, retry_after


class RateLimiter:
    """One `TokenBucket` per key, created lazily on first use with `per_minute`/`burst`.

    `clock` defaults to `time.monotonic` (never wall-clock time, which can
    jump backwards); tests inject a fake one to advance time without
    sleeping for real.
    """

    def __init__(
        self,
        *,
        per_minute: float,
        burst: float,
        max_keys: int = _DEFAULT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if per_minute <= 0 or burst <= 0:
            raise ValueError("per_minute and burst must both be positive")
        if max_keys <= 0:
            raise ValueError("max_keys must be positive")
        self._refill_per_second = per_minute / 60.0
        self._capacity = burst
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()

    def allow(self, key: str, *, cost: float = 1.0) -> tuple[bool, float]:
        """Whether `key` may make one more call right now (`cost` tokens' worth).

        Every call - allowed or not - counts as an access for this key's
        position in the LRU order, so a key making only rejected calls is
        still kept warm rather than evicted first.
        """
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(
                capacity=self._capacity,
                refill_per_second=self._refill_per_second,
                tokens=self._capacity,
                updated_at=now,
            )
            self._buckets[key] = bucket
            self._evict_if_needed()
        else:
            self._buckets.move_to_end(key)
        return bucket.take(now, cost=cost)

    def _evict_if_needed(self) -> None:
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)
