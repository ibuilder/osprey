"""Live-hotlist WebSocket: authorization, subscribe/broadcast, and hub bookkeeping."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from osprey.api.ws import hub
from osprey.main import app
from osprey.models import Membership, Org, Project, Role, User
from osprey.security.auth import Principal, create_access_token
from osprey.security.passwords import hash_password


def _client() -> TestClient:
    """TestClient WITHOUT entering lifespan.

    These tests need only routing, auth, and the hub. Running the lifespan would
    start the app on TestClient's own portal loop and reuse the engine created in
    pytest's loop — harmless on aiosqlite, but asyncpg pins connections to a loop.
    """
    return TestClient(app)


@pytest.fixture
async def tenant(session):
    """A real org, user, membership and project, plus a token for that user.

    The endpoint now confirms the subject exists and the project belongs to their
    org, so a forged principal over invented ids is no longer enough to connect —
    which is the point.
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
    return {"org": org, "user": user, "project": project, "token": token}


def _refused(client: TestClient, url: str) -> bool:
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
    client = _client()
    for qs in ("", "?token=not-a-jwt"):
        assert _refused(client, f"/ws/projects/p1/hotlist{qs}")


async def test_ws_rejects_a_project_in_another_tenant(tenant, session):
    """The boundary RLS exists to hold must hold over WebSocket too."""
    other_org = Org(name="Someone Else")
    session.add(other_org)
    await session.flush()
    other_project = Project(org_id=other_org.id, name="Not Yours")
    session.add(other_project)
    await session.commit()

    assert _refused(_client(), f"/ws/projects/{other_project.id}/hotlist?token={tenant['token']}")


async def test_ws_rejects_an_unknown_project(tenant):
    assert _refused(_client(), f"/ws/projects/does-not-exist/hotlist?token={tenant['token']}")


async def test_ws_rejects_a_revoked_token(tenant, session):
    """Signing someone out must close this door as well as the REST one."""
    user = tenant["user"]
    user.token_version += 1
    session.add(user)
    await session.commit()

    assert _refused(
        _client(), f"/ws/projects/{tenant['project'].id}/hotlist?token={tenant['token']}"
    )


async def test_ws_rejects_a_deactivated_user(tenant, session):
    user = tenant["user"]
    user.is_active = False
    session.add(user)
    await session.commit()

    assert _refused(
        _client(), f"/ws/projects/{tenant['project'].id}/hotlist?token={tenant['token']}"
    )


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
async def test_ws_accepts_an_authorized_subscriber_and_greets(tenant):
    project_id = tenant["project"].id
    with _client().websocket_connect(
        f"/ws/projects/{project_id}/hotlist?token={tenant['token']}"
    ) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "connected"
        assert hello["project_id"] == project_id
        assert hello["org_id"] == tenant["org"].id


async def test_ws_receives_broadcast_payload(tenant):
    """A snapshot published to the hub reaches a subscribed client."""
    project_id = tenant["project"].id
    with _client().websocket_connect(
        f"/ws/projects/{project_id}/hotlist?token={tenant['token']}"
    ) as ws:
        assert ws.receive_json()["type"] == "connected"
        hub.publish(project_id, {"item_count": 3, "items": []})
        msg = ws.receive_json()
        assert msg["type"] == "hotlist"
        assert msg["payload"]["item_count"] == 3


async def test_ws_unsubscribes_on_disconnect(tenant):
    project_id = tenant["project"].id
    with _client().websocket_connect(
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
