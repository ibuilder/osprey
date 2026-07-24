"""Live-hotlist WebSocket: auth, subscribe/broadcast, and hub bookkeeping."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from osprey.api.ws import hub
from osprey.main import app
from osprey.models import Role
from osprey.security.auth import Principal, create_access_token


def _token(org_id: str = "org-1") -> str:
    return create_access_token(
        Principal(user_id="u1", org_id=org_id, role=Role.owner, email="o@x.com")
    )


def test_ws_rejects_missing_or_bad_token():
    """No token / garbage token must be refused before the socket is accepted."""
    with TestClient(app) as client:
        for qs in ("", "?token=not-a-jwt"):
            try:
                with client.websocket_connect(f"/ws/projects/p1/hotlist{qs}"):
                    raise AssertionError("connection should have been rejected")
            except Exception as exc:  # noqa: BLE001 - starlette raises on 4401 close
                assert "should have been rejected" not in str(exc)


def test_ws_accepts_valid_token_and_greets():
    with (
        TestClient(app) as client,
        client.websocket_connect(f"/ws/projects/p1/hotlist?token={_token()}") as ws,
    ):
        hello = ws.receive_json()
        assert hello["type"] == "connected"
        assert hello["project_id"] == "p1"
        assert hello["org_id"] == "org-1"


def test_ws_receives_broadcast_payload():
    """A snapshot published to the hub reaches a subscribed client."""
    with (
        TestClient(app) as client,
        client.websocket_connect(f"/ws/projects/p2/hotlist?token={_token()}") as ws,
    ):
        assert ws.receive_json()["type"] == "connected"
        hub.publish("p2", {"item_count": 3, "items": []})
        msg = ws.receive_json()
        assert msg["type"] == "hotlist"
        assert msg["payload"]["item_count"] == 3


def test_ws_unsubscribes_on_disconnect():
    with (
        TestClient(app) as client,
        client.websocket_connect(f"/ws/projects/p3/hotlist?token={_token()}") as ws,
    ):
        ws.receive_json()
        assert hub._subs.get("p3")  # subscribed while open
    # Publishing after close must not raise and the project key is cleaned up.
    hub.publish("p3", {"item_count": 0})
    assert not hub._subs.get("p3")


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
