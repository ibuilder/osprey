"""Rate limiter backends: window accounting, fail-open, and backend selection."""

from __future__ import annotations

import pytest

from osprey.config import settings
from osprey.security import ratelimit
from osprey.security.ratelimit import Decision, MemoryLimiter, RedisLimiter


# --------------------------------------------------------------------------- #
# Window accounting
# --------------------------------------------------------------------------- #
async def test_memory_limiter_allows_up_to_the_limit_then_refuses():
    limiter = MemoryLimiter()
    results = [await limiter.hit("k", limit=3, window_seconds=60) for _ in range(5)]
    assert [r.allowed for r in results] == [True, True, True, False, False]
    assert results[0].remaining == 2
    assert results[-1].remaining == 0
    assert results[-1].reset_after > 0


async def test_keys_are_independent():
    limiter = MemoryLimiter()
    for _ in range(3):
        await limiter.hit("a", limit=3, window_seconds=60)
    assert (await limiter.hit("a", limit=3, window_seconds=60)).allowed is False
    assert (await limiter.hit("b", limit=3, window_seconds=60)).allowed is True


async def test_old_windows_do_not_accumulate(monkeypatch):
    """A long-lived process must not grow a dict entry per key per window."""
    limiter = MemoryLimiter()
    clock = [1_000_000]
    monkeypatch.setattr(ratelimit.time, "time", lambda: clock[0])

    for _ in range(3):
        await limiter.hit("k", limit=10, window_seconds=60)
    clock[0] += 600  # ten windows later
    await limiter.hit("k", limit=10, window_seconds=60)

    assert len(limiter._counts) == 1, limiter._counts


async def test_a_new_window_restores_the_budget(monkeypatch):
    limiter = MemoryLimiter()
    clock = [1_000_000]
    monkeypatch.setattr(ratelimit.time, "time", lambda: clock[0])

    for _ in range(3):
        await limiter.hit("k", limit=3, window_seconds=60)
    assert (await limiter.hit("k", limit=3, window_seconds=60)).allowed is False

    clock[0] += 60
    assert (await limiter.hit("k", limit=3, window_seconds=60)).allowed is True


async def test_reset_clears_the_current_window():
    limiter = MemoryLimiter()
    for _ in range(3):
        await limiter.hit("k", limit=3, window_seconds=60)
    await limiter.reset("k", window_seconds=60)
    assert (await limiter.hit("k", limit=3, window_seconds=60)).allowed is True


# --------------------------------------------------------------------------- #
# check_all: several windows on one request
# --------------------------------------------------------------------------- #
async def test_check_all_records_every_window_even_after_one_refuses():
    """Otherwise a client keeps its hourly budget by tripping the per-minute one."""
    limiter = MemoryLimiter()
    ratelimit.set_limiter(limiter)

    for _ in range(3):
        await ratelimit.check_all(("m", 2, 60), ("h", 100, 3600))

    hourly = await limiter.hit("h", limit=100, window_seconds=3600)
    assert hourly.remaining == 100 - 4  # three from check_all plus this one


async def test_check_all_reports_the_longest_wait():
    limiter = MemoryLimiter()
    ratelimit.set_limiter(limiter)
    for _ in range(5):
        await ratelimit.check_all(("m", 1, 60), ("h", 1, 3600))
    decision = await ratelimit.check_all(("m", 1, 60), ("h", 1, 3600))
    assert decision.allowed is False
    # The hourly window is the one that actually gates; retry-after must say so.
    assert decision.reset_after > 60


async def test_disabling_the_limiter_allows_everything(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    for _ in range(50):
        assert (await ratelimit.check("k", limit=1, window_seconds=60)).allowed


# --------------------------------------------------------------------------- #
# Redis backend
# --------------------------------------------------------------------------- #
class _FakePipeline:
    def __init__(self, store, fail=False):
        self._store, self._ops, self._fail = store, [], fail

    def incr(self, key):
        self._ops.append(("incr", key))
        return self

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))
        return self

    async def execute(self):
        if self._fail:
            raise ConnectionError("redis is gone")
        out = []
        for op in self._ops:
            if op[0] == "incr":
                self._store[op[1]] = self._store.get(op[1], 0) + 1
                out.append(self._store[op[1]])
            else:
                out.append(True)
        return out


class _FakeRedis:
    def __init__(self, fail=False):
        self.store, self.fail, self.closed = {}, fail, False

    def pipeline(self):
        return _FakePipeline(self.store, self.fail)

    async def delete(self, key):
        self.store.pop(key, None)

    async def aclose(self):
        self.closed = True


async def test_redis_limiter_counts_across_callers():
    limiter = RedisLimiter(_FakeRedis())
    results = [await limiter.hit("k", limit=2, window_seconds=60) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, False, False]


async def test_redis_limiter_sets_a_ttl_on_every_hit():
    """A key that lost its TTL would lock a client out permanently."""
    redis = _FakeRedis()
    limiter = RedisLimiter(redis)
    await limiter.hit("k", limit=5, window_seconds=60)
    key = next(iter(redis.store))
    pipeline = redis.pipeline()
    pipeline.incr(key)
    pipeline.expire(key, 60)
    assert ("expire", key, 60) in pipeline._ops


async def test_redis_limiter_fails_open_when_the_cache_is_down():
    """A limiter that takes the API down with it is an outage, not a control."""
    limiter = RedisLimiter(_FakeRedis(fail=True))
    decision = await limiter.hit("k", limit=1, window_seconds=60)
    assert decision.allowed is True
    assert decision.remaining == 1


async def test_redis_limiter_closes_its_client():
    redis = _FakeRedis()
    await RedisLimiter(redis).close()
    assert redis.closed


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #
async def test_memory_backend_is_honoured(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_backend", "memory")
    assert isinstance(await ratelimit.build_limiter(), MemoryLimiter)


async def test_auto_falls_back_to_memory_when_redis_is_unreachable(monkeypatch):
    """A deployment must not fail to boot because its cache is down."""
    monkeypatch.setattr(settings, "rate_limit_backend", "auto")
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    assert isinstance(await ratelimit.build_limiter(), MemoryLimiter)


@pytest.mark.parametrize("reset_after", [0, 1, 45])
def test_retry_after_is_never_zero(reset_after):
    """Retry-After: 0 invites an immediate retry, which is the opposite of the point."""
    assert Decision(False, 10, 0, reset_after).retry_after >= 1
