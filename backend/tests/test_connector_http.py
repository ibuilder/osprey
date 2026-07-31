"""Connector HTTP layer: rate limiting, 429 handling, retry/backoff.

Providers throttle per OAuth application and answer with ``429`` plus a hint
about when to return. These tests pin the two failure modes that matter: a
throttled request must be retried at the time the provider asked for, and a
request that stays throttled must surface as an error rather than looking like
"this source had no data".

Time and sleep are injected throughout, so nothing here actually waits.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from osprey.connectors.http import (
    RateLimitedTransport,
    connector_client,
    limiter_for,
    parse_reset_at,
    parse_retry_after,
    reset_limiters,
)
from osprey.connectors.ratelimit import RateLimiter


class Clock:
    """A monotonic fake clock that advances only when something sleeps."""

    def __init__(self) -> None:
        self.t = 1_000_000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def _transport(responses: list[httpx.Response], clock: Clock, **kwargs) -> RateLimitedTransport:
    """A transport that serves ``responses`` in order, then repeats the last."""
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    limiter = RateLimiter(1000.0, now=clock.now, sleep=clock.sleep)
    return RateLimitedTransport(
        limiter,
        inner=httpx.MockTransport(handler),
        sleep=clock.sleep,
        now=clock.now,
        **kwargs,
    )


# -- header parsing ----------------------------------------------------------- #


def test_retry_after_accepts_seconds():
    assert parse_retry_after("30", now=0.0) == 30.0


def test_retry_after_accepts_http_date():
    now = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
    header = format_datetime(now + timedelta(seconds=45), usegmt=True)
    assert parse_retry_after(header, now=now.timestamp()) == pytest.approx(45.0, abs=1)


def test_retry_after_in_the_past_is_not_negative():
    now = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
    header = format_datetime(now - timedelta(seconds=60), usegmt=True)
    assert parse_retry_after(header, now=now.timestamp()) == 0.0


def test_retry_after_ignores_junk():
    assert parse_retry_after("soon please", now=0.0) is None
    assert parse_retry_after(None, now=0.0) is None


def test_reset_at_is_relative_to_now():
    # Procore reports an absolute epoch second, not a delta.
    assert parse_reset_at("1500", now=1440.0) == 60.0
    assert parse_reset_at("not-a-number", now=0.0) is None


# -- retry behaviour ---------------------------------------------------------- #


async def test_429_is_retried_and_succeeds():
    clock = Clock()
    transport = _transport(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"ok": True}),
        ],
        clock,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get("https://example.test/x")

    assert resp.status_code == 200
    # Waited exactly as long as the provider asked — not a guess.
    assert clock.slept == [7.0]


async def test_reset_header_is_used_when_retry_after_is_absent():
    clock = Clock()
    transport = _transport(
        [
            httpx.Response(429, headers={"X-Rate-Limit-Reset": str(int(clock.t) + 12)}),
            httpx.Response(200),
        ],
        clock,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("https://example.test/x")

    assert clock.slept == [12.0]


async def test_unhinted_429_backs_off_exponentially():
    clock = Clock()
    transport = _transport([httpx.Response(429)], clock, max_attempts=4, backoff_base=0.5)
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get("https://example.test/x")

    assert resp.status_code == 429
    assert clock.slept == [0.5, 1.0, 2.0]  # 3 waits before the 4th and final try


async def test_backoff_is_capped():
    clock = Clock()
    transport = _transport(
        [httpx.Response(503)], clock, max_attempts=5, backoff_base=10.0, backoff_cap=15.0
    )
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("https://example.test/x")

    assert clock.slept == [10.0, 15.0, 15.0, 15.0]


async def test_persistent_throttle_surfaces_as_an_error():
    """The failure mode that matters: a throttle must never look like no data."""
    clock = Clock()
    transport = _transport([httpx.Response(429)], clock, max_attempts=2)
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get("https://example.test/x")

    assert resp.status_code == 429
    with pytest.raises(httpx.HTTPStatusError):
        resp.raise_for_status()


async def test_server_errors_are_retried():
    clock = Clock()
    transport = _transport([httpx.Response(502), httpx.Response(200)], clock)
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get("https://example.test/x")

    assert resp.status_code == 200


async def test_client_errors_are_not_retried():
    """403 is a real answer; retrying it just burns the rate limit."""
    clock = Clock()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403)

    limiter = RateLimiter(1000.0, now=clock.now, sleep=clock.sleep)
    transport = RateLimitedTransport(
        limiter, inner=httpx.MockTransport(handler), sleep=clock.sleep, now=clock.now
    )
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get("https://example.test/x")

    assert resp.status_code == 403
    assert calls == 1
    assert clock.slept == []


async def test_retried_response_body_is_readable():
    """A retry must drain the discarded response, and still return a usable one."""
    clock = Clock()
    transport = _transport(
        [httpx.Response(429, text="slow down"), httpx.Response(200, json={"v": 1})], clock
    )
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get("https://example.test/x")

    assert resp.json() == {"v": 1}


# -- pacing ------------------------------------------------------------------- #


async def test_requests_are_paced_by_the_token_bucket():
    clock = Clock()
    limiter = RateLimiter(2.0, burst=2.0, now=clock.now, sleep=clock.sleep)
    transport = RateLimitedTransport(
        limiter,
        inner=httpx.MockTransport(lambda r: httpx.Response(200)),
        sleep=clock.sleep,
        now=clock.now,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        for _ in range(4):
            await client.get("https://example.test/x")

    # Two burst tokens are free; the next two wait for the bucket to refill.
    assert clock.slept == [0.5, 0.5]


def test_limiters_are_shared_per_source():
    """Providers meter per OAuth app, so two connections must share one bucket."""
    reset_limiters()
    try:
        assert limiter_for("procore") is limiter_for("procore")
        assert limiter_for("procore") is not limiter_for("gmail")
    finally:
        reset_limiters()


async def test_connector_client_is_a_usable_async_client():
    async with connector_client("gmail", timeout=5) as client:
        assert isinstance(client, httpx.AsyncClient)
