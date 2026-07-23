"""Async token-bucket rate limiter for connector pollers.

Every poller should wrap its provider calls so Osprey stays within each source's
rate limits. Time and sleep are injectable so the token math is deterministically
unit-tested without real waiting.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class RateLimiter:
    def __init__(
        self,
        rate_per_sec: float,
        burst: float | None = None,
        *,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        self._rate = rate_per_sec
        self._capacity = burst if burst is not None else max(1.0, rate_per_sec)
        self._tokens = self._capacity
        self._now = now
        self._sleep = sleep
        self._updated = now()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        t = self._now()
        elapsed = max(0.0, t - self._updated)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated = t

    async def acquire(self, n: float = 1.0) -> None:
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = n - self._tokens
                await self._sleep(deficit / self._rate)
