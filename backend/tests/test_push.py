"""Device registration + critical-item push notification."""

from __future__ import annotations

from osprey.engine.notify import (
    LoggingPushSender,
    PushMessage,
    PushSender,
    critical_items,
    notify_critical,
)
from osprey.models import Device, Org, utcnow


async def test_register_device(auth_client):
    client, _ = auth_client
    r = await client.post("/devices", json={"platform": "ios", "token": "apns-token-abc"})
    assert r.status_code == 201
    assert r.json()["platform"] == "ios"
    # Idempotent re-register returns the same device.
    r2 = await client.post("/devices", json={"platform": "ios", "token": "apns-token-abc"})
    assert r2.json()["id"] == r.json()["id"]
    listed = (await client.get("/devices")).json()
    assert len(listed) == 1


def test_critical_items_filter():
    payload = {
        "items": [
            {"item_id": "1", "bucket": "act_today", "what": "Notice"},
            {"item_id": "2", "bucket": "this_week", "what": "Invoice"},
        ]
    }
    crit = critical_items(payload)
    assert [c["item_id"] for c in crit] == ["1"]


async def test_notify_critical_pushes_to_devices(session):
    org = Org(name="Push Co")
    session.add(org)
    await session.flush()
    session.add(
        Device(org_id=org.id, user_id="u1", platform="android", token="fcm-1", created_at=utcnow())
    )
    await session.flush()

    payload = {
        "items": [
            {
                "item_id": "1",
                "bucket": "act_today",
                "what": "Notice of delay",
                "recommended_action": "Respond",
            }
        ]
    }
    sent = await notify_critical(session, org_id=org.id, payload=payload)
    assert sent == 1


async def test_logging_sender():
    sender = LoggingPushSender()
    ok = await sender.send(
        Device(org_id="o", user_id="u", platform="web", token="t"),
        PushMessage("t", "b", {}),
    )
    assert ok is True


# ---- Concrete APNs / FCM senders ------------------------------------------- #
def test_apns_payload_shape():
    from osprey.engine.push_senders import apns_payload

    payload = apns_payload(PushMessage("🔴 Notice", "Respond now", {"item_id": "i1", "score": 88}))
    assert payload["aps"]["alert"] == {"title": "🔴 Notice", "body": "Respond now"}
    assert payload["aps"]["sound"] == "default"
    assert payload["item_id"] == "i1"  # custom data lifted to top level


def test_fcm_message_shape():
    from osprey.engine.push_senders import fcm_message

    msg = fcm_message("dev-token", PushMessage("Title", "Body", {"item_id": "i1", "score": 88}))
    assert msg["message"]["token"] == "dev-token"
    assert msg["message"]["notification"] == {"title": "Title", "body": "Body"}
    assert msg["message"]["data"] == {"item_id": "i1", "score": "88"}  # data must be strings


def test_build_sender_defaults_to_logging():
    from osprey.engine.push_senders import build_sender

    assert isinstance(build_sender(), LoggingPushSender)  # unconfigured => logging


async def test_composite_routes_by_platform():
    from osprey.engine.push_senders import CompositePushSender

    class Recorder(PushSender):
        def __init__(self, name):
            self.name = name
            self.hits = 0

        async def send(self, device, message):
            self.hits += 1
            return True

    ios, android, web = Recorder("ios"), Recorder("android"), Recorder("web")
    composite = CompositePushSender(ios=ios, android=android, web=web)
    await composite.send(
        Device(org_id="o", user_id="u", platform="ios", token="a"), PushMessage("t", "b", {})
    )
    await composite.send(
        Device(org_id="o", user_id="u", platform="android", token="b"), PushMessage("t", "b", {})
    )
    await composite.send(
        Device(org_id="o", user_id="u", platform="web", token="c"), PushMessage("t", "b", {})
    )
    assert ios.hits == 1
    assert android.hits == 1
    assert web.hits == 1  # web routes to the web sender


# ---- Web Push (VAPID) ------------------------------------------------------ #
def test_parse_subscription_valid_and_invalid():
    from osprey.engine.push_senders import parse_subscription

    sub = parse_subscription('{"endpoint": "https://push/x", "keys": {"p256dh": "a", "auth": "b"}}')
    assert sub["endpoint"] == "https://push/x"
    import pytest

    with pytest.raises(ValueError):
        parse_subscription('{"no": "endpoint"}')


def test_webpush_notification_shape():
    from osprey.engine.push_senders import webpush_notification

    n = webpush_notification(PushMessage("Title", "Body", {"item_id": "i1"}))
    assert n == {"title": "Title", "body": "Body", "data": {"item_id": "i1"}}


async def test_webpush_sender_without_lib_returns_false():
    # pywebpush isn't installed in the test env -> send skips gracefully.
    from osprey.engine.push_senders import WebPushSender

    ok = await WebPushSender().send(
        Device(org_id="o", user_id="u", platform="web", token='{"endpoint":"x","keys":{}}'),
        PushMessage("t", "b", {}),
    )
    assert ok is False
