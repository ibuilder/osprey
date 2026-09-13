"""Worker task logic — the loop that actually runs in production.

These functions are what the ARQ cron jobs call. The behaviour that matters most
here is not the happy path but the failure handling: SPEC's "one source down !=
system down" rule lives in `poll_connection` and `renew_subscriptions`, and until
now nothing asserted it at this layer.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from osprey.connectors.base import registry
from osprey.models import Connection, ConnectionStatus, Org, Project
from osprey.workers import tasks


async def _org_project(session, name: str = "Worker Co"):
    org = Org(name=name)
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name="Tower B")
    session.add(project)
    await session.flush()
    return org, project


async def _connection(session, org, project, *, source_type="filedrop", status=None):
    conn = Connection(
        org_id=org.id,
        project_id=project.id,
        source_type=source_type,
        account_ref="acct",
        status=status or ConnectionStatus.active,
    )
    session.add(conn)
    await session.flush()
    return conn


# --------------------------------------------------------------------------- #
# poll_connection
# --------------------------------------------------------------------------- #
async def test_polling_an_unknown_connection_reports_rather_than_raises(session):
    """A queued job for a deleted connection must not crash the worker."""
    assert await tasks.poll_connection(session, "does-not-exist") == {
        "error": "connection not found"
    }


async def test_a_successful_poll_marks_the_connection_active_and_stamps_the_cursor(session):
    org, project = await _org_project(session)
    conn = await _connection(session, org, project, status=ConnectionStatus.degraded)
    conn.last_error = "an earlier failure"
    session.add(conn)

    result = await tasks.poll_connection(session, conn.id)

    assert result["connection_id"] == conn.id
    # poll_connection leaves committing to its caller, so flush before refreshing
    # or the re-SELECT reloads the pre-poll row and the assertions test nothing.
    await session.flush()
    await session.refresh(conn)
    # Recovery must clear the previous error, not just flip the status.
    assert conn.status == ConnectionStatus.active
    assert conn.last_error is None
    assert conn.last_sync is not None


async def test_a_failing_source_degrades_that_connection_only(session, monkeypatch):
    """SPEC golden rule: one source down is not the system down."""
    org, project = await _org_project(session)
    broken = await _connection(session, org, project)
    healthy = await _connection(session, org, project)

    connector = registry.get("filedrop")

    async def boom(self, conn, since):
        if conn.id == broken.id:
            raise RuntimeError("provider returned 503")
        return
        yield  # pragma: no cover

    monkeypatch.setattr(type(connector), "poll", boom)

    result = await tasks.poll_connection(session, broken.id)
    assert result["created"] == 0  # reported, not raised

    await session.flush()
    await session.refresh(broken)
    assert broken.status == ConnectionStatus.degraded
    assert "503" in broken.last_error

    await tasks.poll_connection(session, healthy.id)
    await session.flush()
    await session.refresh(healthy)
    assert healthy.status == ConnectionStatus.active


async def test_a_long_provider_error_is_truncated_before_storage(session, monkeypatch):
    """A megabyte of provider HTML must not become a database row."""
    org, project = await _org_project(session)
    conn = await _connection(session, org, project)

    async def boom(self, conn, since):
        raise RuntimeError("x" * 5000)
        yield  # pragma: no cover

    monkeypatch.setattr(type(registry.get("filedrop")), "poll", boom)
    await tasks.poll_connection(session, conn.id)

    await session.flush()
    await session.refresh(conn)
    assert len(conn.last_error) <= 500


# --------------------------------------------------------------------------- #
# poll_all_active
# --------------------------------------------------------------------------- #
async def test_poll_all_skips_revoked_connections(session):
    """A revoked connection has no credentials left; polling it only makes noise."""
    org, project = await _org_project(session)
    await _connection(session, org, project)
    await _connection(session, org, project, status=ConnectionStatus.revoked)

    result = await tasks.poll_all_active(session)
    assert result["connections"] == 1


async def test_poll_all_includes_degraded_connections(session):
    """Degraded means "failing", not "give up" -- the next cycle must retry it."""
    org, project = await _org_project(session)
    await _connection(session, org, project, status=ConnectionStatus.degraded)

    assert (await tasks.poll_all_active(session))["connections"] == 1


async def test_poll_all_continues_past_a_failing_connection(session, monkeypatch):
    org, project = await _org_project(session)
    first = await _connection(session, org, project)
    await _connection(session, org, project)

    async def boom(self, conn, since):
        if conn.id == first.id:
            raise RuntimeError("down")
        return
        yield  # pragma: no cover

    monkeypatch.setattr(type(registry.get("filedrop")), "poll", boom)

    # Both are visited; the failure is absorbed into the per-connection status.
    assert (await tasks.poll_all_active(session))["connections"] == 2


async def test_poll_all_with_nothing_configured(session):
    assert await tasks.poll_all_active(session) == {"connections": 0, "created": 0}


# --------------------------------------------------------------------------- #
# renew_subscriptions
# --------------------------------------------------------------------------- #
async def test_renewal_failure_does_not_abort_the_batch(session, monkeypatch):
    """Losing every webhook because one tenant's token expired would be severe."""
    org, project = await _org_project(session)
    first = await _connection(session, org, project)
    await _connection(session, org, project)

    calls: list[str] = []

    async def flaky(session_, conn, *, notify_base=""):
        calls.append(conn.id)
        if conn.id == first.id:
            raise RuntimeError("token expired")
        return True

    monkeypatch.setattr(tasks, "sync_subscription", flaky)

    result = await tasks.renew_subscriptions(session, notify_base="https://osprey.test")
    assert result == {"checked": 2, "renewed": 1}
    assert len(calls) == 2  # the second was still attempted


async def test_renewal_only_considers_active_connections(session, monkeypatch):
    org, project = await _org_project(session)
    await _connection(session, org, project, status=ConnectionStatus.degraded)
    await _connection(session, org, project, status=ConnectionStatus.revoked)

    monkeypatch.setattr(tasks, "sync_subscription", lambda *a, **k: pytest.fail("unreachable"))

    assert (await tasks.renew_subscriptions(session))["checked"] == 0


# --------------------------------------------------------------------------- #
# refresh_project_task
# --------------------------------------------------------------------------- #
async def test_refresh_builds_a_snapshot_and_reports_counts(session):
    org, project = await _org_project(session)

    result = await tasks.refresh_project_task(session, project.id)

    assert result["project_id"] == project.id
    assert result["items"] == 0
    assert result["act_today"] == 0
    assert result["pushed"] == 0


async def test_refresh_surfaces_act_today_and_pushes(session, monkeypatch):
    """The count the worker reports is the one the push decision is made on."""
    org, project = await _org_project(session)

    async def fake_build(session_, project_id, generated_by=""):
        class _Snap:
            payload = {"item_count": 4, "buckets": {"act_today": {"count": 2}}}

        return _Snap()

    async def fake_notify(session_, *, org_id, payload):
        assert payload["buckets"]["act_today"]["count"] == 2
        return 3

    monkeypatch.setattr(tasks, "build_hotlist", fake_build)
    monkeypatch.setattr("osprey.engine.notify.notify_critical", fake_notify)

    result = await tasks.refresh_project_task(session, project.id)
    assert result == {"project_id": project.id, "act_today": 2, "items": 4, "pushed": 3}


# --------------------------------------------------------------------------- #
# purge_retention
# --------------------------------------------------------------------------- #
async def test_purge_reports_only_what_it_removed(session):
    """A nightly job that logs a wall of zeroes teaches operators to ignore it."""
    await _org_project(session)
    await session.commit()

    result = await tasks.purge_retention(session)
    assert result["purged"] == {}


async def test_purge_reaps_dead_refresh_tokens_even_with_no_retention_window(session):
    """Sessions accumulate on every sign-in whether or not retention is configured."""
    from datetime import timedelta

    from osprey.config import settings
    from osprey.models import RefreshToken, User, utcnow
    from osprey.security.auth import hash_secret

    org, _project = await _org_project(session)
    user = User(email="reaped@example.com")
    session.add(user)
    await session.flush()
    stale = utcnow() - timedelta(days=settings.refresh_token_ttl_days + 1)
    session.add(
        RefreshToken(
            org_id=org.id,
            user_id=user.id,
            token_hash=hash_secret("long-since-expired"),
            expires_at=stale.replace(tzinfo=None) if settings.is_sqlite else stale,
        )
    )
    await session.commit()

    result = await tasks.purge_retention(session)
    assert result["purged"].get("sessions") == 1
    assert (await session.execute(select(RefreshToken))).scalars().all() == []


# --------------------------------------------------------------------------- #
# Scheduled scripts
# --------------------------------------------------------------------------- #
async def test_scheduled_scripts_delegate_to_the_script_service(session, monkeypatch):
    async def fake_run(session_):
        return {"ran": 2, "emitted": 5}

    monkeypatch.setattr("osprey.scripts.service.run_due_scripts", fake_run)
    assert await tasks.run_scheduled_scripts(session) == {"ran": 2, "emitted": 5}
