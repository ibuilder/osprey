"""Request rate limiting — fixed-window counters, Redis-backed when available.

Two backends, chosen by ``OSPREY_RATE_LIMIT_BACKEND``:

``memory``
    A per-process dict. Correct for one replica and for the desktop bundle, which
    is the only place Osprey runs single-process. With N replicas the effective
    limit is N x the configured one, so this is a *floor*, not a guarantee.

``redis``
    ``INCR`` + ``EXPIRE`` on a key bucketed by window. Shared across replicas, and
    the only backend that actually enforces a global limit.

``auto`` (default) picks Redis when the ``prod`` extra is installed and the
configured Redis answers, and falls back to memory otherwise — so a deployment
never fails to boot because its cache is down, it just degrades to per-process
limits and says so once.

Windows are fixed rather than sliding on purpose: a sliding log costs a sorted set
per client and the burst it prevents (2x the limit across a window boundary) is not
worth that at this scale. The credential limiter compensates by stacking a
per-minute and a per-hour window, which a boundary burst cannot both evade.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass

from ..config import settings

log = logging.getLogger("osprey.ratelimit")


@dataclass(frozen=True)
class Decision:
    """Outcome of one limiter check."""

    allowed: bool
    limit: int
    remaining: int
    reset_after: int  # seconds until the window rolls over

    @property
    def retry_after(self) -> int:
        return max(1, self.reset_after)


class Limiter:
    """Counts hits per (key, window). Subclasses supply the storage."""

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> Decision:
        raise NotImplementedError

    async def reset(self, key: str, *, window_seconds: int) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class MemoryLimiter(Limiter):
    """Per-process counters. Old windows are dropped as they are encountered."""

    def __init__(self) -> None:
        self._counts: dict[tuple[str, int], int] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _bucket(window_seconds: int) -> int:
        return int(time.time()) // window_seconds

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> Decision:
        bucket = self._bucket(window_seconds)
        async with self._lock:
            # Drop every other window for this key; the dict would otherwise grow
            # without bound across a long-lived process.
            for k in [k for k in self._counts if k[0] == key and k[1] != bucket]:
                del self._counts[k]
            count = self._counts.get((key, bucket), 0) + 1
            self._counts[(key, bucket)] = count
        reset = window_seconds - (int(time.time()) % window_seconds)
        return Decision(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            reset_after=reset,
        )

    async def reset(self, key: str, *, window_seconds: int) -> None:
        bucket = self._bucket(window_seconds)
        async with self._lock:
            self._counts.pop((key, bucket), None)


class RedisLimiter(Limiter):
    """Shared counters via ``INCR``/``EXPIRE``, pipelined into one round trip."""

    def __init__(self, client) -> None:
        self._redis = client

    @staticmethod
    def _key(key: str, window_seconds: int) -> str:
        return f"osprey:rl:{window_seconds}:{int(time.time()) // window_seconds}:{key}"

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> Decision:
        redis_key = self._key(key, window_seconds)
        try:
            pipe = self._redis.pipeline()
            pipe.incr(redis_key)
            # Re-applied every hit. Cheap, and it repairs a key that somehow lost
            # its TTL, which would otherwise lock a client out permanently.
            pipe.expire(redis_key, window_seconds)
            count = (await pipe.execute())[0]
        except Exception as exc:  # noqa: BLE001
            # Fail open. A limiter that takes the API down with it when the cache
            # blips has converted a availability control into an outage.
            log.warning("rate limit backend unavailable, allowing request: %s", exc)
            return Decision(allowed=True, limit=limit, remaining=limit, reset_after=window_seconds)
        reset = window_seconds - (int(time.time()) % window_seconds)
        return Decision(
            allowed=int(count) <= limit,
            limit=limit,
            remaining=max(0, limit - int(count)),
            reset_after=reset,
        )

    async def reset(self, key: str, *, window_seconds: int) -> None:
        try:
            await self._redis.delete(self._key(key, window_seconds))
        except Exception as exc:  # noqa: BLE001
            log.warning("rate limit reset failed: %s", exc)

    async def close(self) -> None:
        # Best effort: the process is going away regardless.
        with contextlib.suppress(Exception):  # pragma: no cover
            await self._redis.aclose()


_LIMITER: Limiter | None = None


async def build_limiter() -> Limiter:
    """Construct the configured backend, falling back to memory on any problem."""
    backend = settings.rate_limit_backend
    if backend == "memory":
        return MemoryLimiter()
    try:
        import redis.asyncio as redis_asyncio
    except ImportError:
        if backend == "redis":
            log.warning("OSPREY_RATE_LIMIT_BACKEND=redis but redis is not installed")
        return MemoryLimiter()
    try:
        client = redis_asyncio.from_url(settings.redis_url, decode_responses=True)
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        level = log.warning if backend == "redis" else log.info
        level("rate limiting falling back to in-process counters (%s)", exc)
        return MemoryLimiter()
    log.info("rate limiting backed by Redis")
    return RedisLimiter(client)


async def get_limiter() -> Limiter:
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = await build_limiter()
    return _LIMITER


def set_limiter(limiter: Limiter | None) -> None:
    """Install a limiter (tests, and the app factory at startup)."""
    global _LIMITER
    _LIMITER = limiter


async def close_limiter() -> None:
    global _LIMITER
    if _LIMITER is not None:
        await _LIMITER.close()
    _LIMITER = None


async def check(key: str, *, limit: int, window_seconds: int) -> Decision:
    """Record one hit against ``key`` and say whether it is allowed."""
    if not settings.rate_limit_enabled:
        return Decision(allowed=True, limit=limit, remaining=limit, reset_after=window_seconds)
    limiter = await get_limiter()
    return await limiter.hit(key, limit=limit, window_seconds=window_seconds)


async def check_all(*checks: tuple[str, int, int]) -> Decision:
    """Apply several windows to one request; the first refusal wins.

    Each check is ``(key, limit, window_seconds)``. Every window is recorded even
    once one has refused, so a client cannot keep its hourly counter low by
    tripping the per-minute limit first.
    """
    refused: Decision | None = None
    tightest: Decision | None = None
    for key, limit, window in checks:
        decision = await check(key, limit=limit, window_seconds=window)
        if not decision.allowed and (refused is None or decision.reset_after > refused.reset_after):
            refused = decision
        if tightest is None or decision.remaining < tightest.remaining:
            tightest = decision
    return refused or tightest or Decision(True, 0, 0, 0)
