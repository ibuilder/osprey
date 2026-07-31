"""Shared HTTP client for connector pollers: rate limiting + retry.

Every provider Osprey talks to publishes a rate limit, and every one of them
answers ``429`` with a hint about when to come back — Procore via ``Retry-After``
plus ``X-Rate-Limit-Reset``, Google and Graph via ``Retry-After``. Honouring those
is not optional politeness: a connector that ignores them gets throttled harder,
and one that treats ``429`` as "no data" (as the Procore poller used to) drops
real signals on the floor without a trace.

Rather than make every connector remember this, the behaviour lives in a
transport wrapper. Connectors keep calling ``client.get(...)`` normally and
:class:`RateLimitedTransport` handles pacing, backoff and retry underneath.

Limiters are shared per ``source_type`` for the life of the process, because
providers meter per OAuth application, not per connection — two projects polling
Procore draw from the same bucket.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime

import httpx

from ..config import settings
from .ratelimit import RateLimiter

# 429 is the rate-limit signal; the 5xx family covers the transient provider-side
# failures that a retry can genuinely fix. Anything else (401, 403, 404) is a real
# answer and is handed straight back to the connector.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

#: Providers that publish a limit meaningfully different from the default.
_RATE_OVERRIDES: dict[str, float] = {
    # Procore meters per client_id on a rolling minute; stay well inside it.
    "procore": 3.0,
}

_limiters: dict[str, RateLimiter] = {}


def limiter_for(source_type: str) -> RateLimiter:
    """Return the process-wide limiter for a source, creating it on first use."""
    limiter = _limiters.get(source_type)
    if limiter is None:
        rate = _RATE_OVERRIDES.get(source_type, settings.connector_rate_per_sec)
        limiter = RateLimiter(rate, burst=max(rate, settings.connector_rate_burst))
        _limiters[source_type] = limiter
    return limiter


def reset_limiters() -> None:
    """Drop cached limiters. For tests that change the configured rate."""
    _limiters.clear()


def parse_retry_after(value: str | None, *, now: float) -> float | None:
    """Seconds to wait per an HTTP ``Retry-After`` header, or None if unusable.

    The header is either a delta in seconds or an HTTP-date; both forms are in the
    wild, so both are accepted. A date in the past yields 0, not a negative sleep.
    """
    if not value:
        return None
    raw = value.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - now)


def parse_reset_at(value: str | None, *, now: float) -> float | None:
    """Seconds to wait per Procore's ``X-Rate-Limit-Reset`` (epoch seconds)."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()) - now)
    except ValueError:
        return None


class RateLimitedTransport(httpx.AsyncBaseTransport):
    """Paces requests through a token bucket and retries throttled/transient ones.

    Wrapping a transport rather than exposing a ``request()`` helper keeps every
    existing call site unchanged, and leaves respx — which patches the underlying
    transport — able to mock connectors in tests exactly as before.
    """

    def __init__(
        self,
        limiter: RateLimiter,
        *,
        max_attempts: int = 4,
        backoff_base: float = 0.5,
        backoff_cap: float = 60.0,
        inner: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._limiter = limiter
        self._max_attempts = max(1, max_attempts)
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._inner = inner if inner is not None else httpx.AsyncHTTPTransport()
        self._sleep = sleep
        self._now = now

    def _delay_for(self, response: httpx.Response, attempt: int) -> float:
        """How long to wait before retrying: obey the provider, else back off."""
        now = self._now()
        hinted = parse_retry_after(response.headers.get("Retry-After"), now=now)
        if hinted is None:
            hinted = parse_reset_at(response.headers.get("X-Rate-Limit-Reset"), now=now)
        if hinted is None:
            hinted = self._backoff_base * (2 ** (attempt - 1))
        return min(hinted, self._backoff_cap)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        for attempt in range(1, self._max_attempts + 1):
            await self._limiter.acquire()
            response = await self._inner.handle_async_request(request)
            if response.status_code not in RETRY_STATUS or attempt == self._max_attempts:
                # Out of retries: hand the failure back so the connector's
                # raise_for_status() reports it instead of it vanishing silently.
                return response
            delay = self._delay_for(response, attempt)
            # Drain before retrying, or the connection cannot be reused.
            await response.aread()
            await response.aclose()
            await self._sleep(delay)
        raise AssertionError("unreachable: the loop always returns")  # pragma: no cover

    async def aclose(self) -> None:
        await self._inner.aclose()


def connector_client(source_type: str, **kwargs) -> httpx.AsyncClient:
    """An ``AsyncClient`` that respects ``source_type``'s rate limit.

    Drop-in for ``httpx.AsyncClient(...)`` inside a connector's poll loop.
    """
    transport = RateLimitedTransport(
        limiter_for(source_type),
        max_attempts=settings.connector_max_attempts,
    )
    return httpx.AsyncClient(transport=transport, **kwargs)
