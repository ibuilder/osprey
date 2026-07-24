"""Production hardening: rate limiter, RLS helper, observability, subscription renewal."""

from __future__ import annotations

import pytest

from osprey.connectors.ratelimit import RateLimiter
from osprey.models import Connection, ConnectionStatus, Org, Project


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def advance(self, d: float) -> None:
        self.t += d


async def test_rate_limiter_bursts_then_throttles():
    clock = _Clock()
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)
        clock.advance(d)

    rl = RateLimiter(rate_per_sec=10, burst=2, now=clock.now, sleep=fake_sleep)
    await rl.acquire()  # burst token 1 — no wait
    await rl.acquire()  # burst token 2 — no wait
    assert sleeps == []
    await rl.acquire()  # empty bucket — must wait ~1/10s for a refill
    assert sleeps and sleeps[0] == pytest.approx(0.1, rel=0.05)


def test_rate_limiter_rejects_bad_rate():
    with pytest.raises(ValueError):
        RateLimiter(0)


async def test_rls_helper_is_noop_on_sqlite(session):
    from osprey.security.rls import set_current_org

    # No error, no effect on SQLite (RLS is Postgres-only).
    await set_current_org(session, "org-123")


def test_observability_disabled_by_default():
    from osprey.observability import setup_observability

    class _FakeApp:
        pass

    assert setup_observability(_FakeApp()) is False


async def test_renew_subscriptions_skips_non_subscription_sources(session):
    from osprey.workers.tasks import renew_subscriptions

    org = Org(name="Sub Co")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name="P")
    session.add(project)
    await session.flush()
    session.add(
        Connection(
            org_id=org.id,
            project_id=project.id,
            source_type="filedrop",
            account_ref="drop",
            status=ConnectionStatus.active,
        )
    )
    await session.flush()

    result = await renew_subscriptions(session, notify_base="https://osprey.example.com")
    assert result["checked"] == 1
    assert result["renewed"] == 0  # filedrop has no subscriptions
