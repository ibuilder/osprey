"""Live-hotlist WebSocket: authorization, subscribe/broadcast, and hub bookkeeping."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from osprey.api.ws import hub
from osprey.db import dispose
from osprey.main import app
from osprey.models import Membership, Org, Project, Role, User
from osprey.security.auth import Principal, create_access_token
from osprey.security.passwords import hash_password


def _bare_client() -> TestClient:
    """TestClient WITHOUT the app lifespan, for paths that never reach the database.

    Only the bad-token test uses it: the token is rejected before any query runs.
    """
    return TestClient(app)


@contextlib.contextmanager
def _app_client() -> Iterator[TestClient]:
    """TestClient WITH the app lifespan, for anything that queries the database.

    TestClient runs the app on its own event loop, and asyncpg pins pooled
    connections to the loop that opened them. An engine created on pytest's loop
    and then used here fails with "attached to a different loop" -- and because
    WebSocket authorization fails closed, that surfaced on Postgres as every
    happy-path test being refused, while every rejection test passed for the wrong
    reason. Entering the lifespan makes the app create, use and dispose its engine
    on this one loop. Callers must dispose the pytest-loop engine first (the
    ``tenant`` fixture does). aiosqlite tolerates the cross-loop case, which is why
    SQLite runs never showed it.
    """
    with TestClient(app) as client:
        yield client


@pytest.fixture
async def tenant(session):
    """A real org, user, membership and project, plus a token for that user.

    The endpoint confirms the subject exists and the project belongs to their org,
    so a forged principal over invented ids is not enough to connect -- which is
    the point.
    """
    org = Org(name="WS Co")
    session.add(org)
    await session.flush()
    user = User(email="ws@example.com", password_hash=hash_password("Sup3rSecret!pass"))
    session.add(user)
    await session.flush()
    session.add(Membership(org_id=org.id, user_id=user.id, role=Role.owner))
    project = Project(org_id=org.id, name="WS Project")
    session.add(project)
    await session.commit()

    token = create_access_token(
        Principal(
            user_id=user.id,
            org_id=org.id,
            role=Role.owner,
            email=user.email,
            token_version=user.token_version,
        )
    )
    # Release the global engine bound to pytest's loop, so the app client creates
    # its own on the loop it actually runs on. See _app_client.
    await dispose()
    return {"org": org, "user": user, "project": project, "token": token}


def _refused(url: str) -> bool:
    with _app_client() as client:
        try:
            with client.websocket_connect(url):
                return False
        except Exception:  # noqa: BLE001 - starlette raises on a 4401 close
            return True


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #
def test_ws_rejects_missing_or_bad_token():
    """No token / garbage token must be refused before the socket is accepted."""
    client = _bare_client()
    for qs in ("", "?token=not-a-jwt"):
        try:
            with client.websocket_connect(f"/ws/projects/p1/hotlist{qs}"):
                raise AssertionError("connection should have been rejected")
        except Exception as exc:  # noqa: BLE001 - starlette raises on a 4401 close
            assert "should have been rejected" not in str(exc)


async def test_ws_rejects_a_project_in_another_tenant(tenant, session):
    """The boundary RLS exists to hold must hold over WebSocket too."""
    other_org = Org(name="Someone Else")
    session.add(other_org)
    await session.flush()
    other_project = Project(org_id=other_org.id, name="Not Yours")
    session.add(other_project)
    await session.commit()

    assert _refused(f"/ws/projects/{other_project.id}/hotlist?token={tenant['token']}")


async def test_ws_rejects_an_unknown_project(tenant):
    assert _refused(f"/ws/projects/does-not-exist/hotlist?token={tenant['token']}")


async def test_ws_rejects_a_revoked_token(tenant, session):
    """Signing someone out must close this door as well as the REST one."""
    user = tenant["user"]
    user.token_version += 1
    session.add(user)
    await session.commit()

    assert _refused(f"/ws/projects/{tenant['project'].id}/hotlist?token={tenant['token']}")


async def test_ws_rejects_a_deactivated_user(tenant, session):
    user = tenant["user"]
    user.is_active = False
    session.add(user)
    await session.commit()

    assert _refused(f"/ws/projects/{tenant['project'].id}/hotlist?token={tenant['token']}")


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
# These are also what make the rejection tests above meaningful: authorization
# fails closed, so a rejection test cannot tell "refused for the right reason"
# from "the database lookup broke". A green happy path in the same harness proves
# the lookup works.
async def test_ws_accepts_an_authorized_subscriber_and_greets(tenant):
    project_id = tenant["project"].id
    with (
        _app_client() as client,
        client.websocket_connect(
            f"/ws/projects/{project_id}/hotlist?token={tenant['token']}"
        ) as ws,
    ):
        hello = ws.receive_json()
        assert hello["type"] == "connected"
        assert hello["project_id"] == project_id
        assert hello["org_id"] == tenant["org"].id


async def test_ws_receives_broadcast_payload(tenant):
    """A snapshot published to the hub reaches a subscribed client."""
    project_id = tenant["project"].id
    with (
        _app_client() as client,
        client.websocket_connect(
            f"/ws/projects/{project_id}/hotlist?token={tenant['token']}"
        ) as ws,
    ):
        assert ws.receive_json()["type"] == "connected"
        hub.publish(project_id, {"item_count": 3, "items": []})
        msg = ws.receive_json()
        assert msg["type"] == "hotlist"
        assert msg["payload"]["item_count"] == 3


async def test_ws_unsubscribes_on_disconnect(tenant):
    project_id = tenant["project"].id
    with _app_client() as client:
        with client.websocket_connect(
            f"/ws/projects/{project_id}/hotlist?token={tenant['token']}"
        ) as ws:
            ws.receive_json()
            assert hub._subs.get(project_id)  # subscribed while open
        # Publishing after close must not raise and the project key is cleaned up.
        hub.publish(project_id, {"item_count": 0})
        assert not hub._subs.get(project_id)


# --------------------------------------------------------------------------- #
# The hub itself
# --------------------------------------------------------------------------- #
async def test_hub_publish_is_safe_with_no_subscribers():
    hub.publish("nobody-listening", {"x": 1})  # must not raise


async def test_hub_publish_drops_when_queue_is_full():
    """A slow consumer must not block the publisher (bounded queue, no await)."""
    q = hub.subscribe("p-full")
    try:
        for i in range(50):  # far beyond the queue's maxsize
            hub.publish("p-full", {"n": i})
        assert q.qsize() <= 8  # bounded; excess dropped, not buffered
        assert isinstance(await asyncio.wait_for(q.get(), timeout=1), dict)
    finally:
        hub.unsubscribe("p-full", q)
