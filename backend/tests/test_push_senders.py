"""APNs, FCM and Web Push senders against mocked HTTP.

The payload builders were already tested; the senders themselves -- provider-token
signing and caching, endpoint selection, and the never-raise failure contract --
were not. HTTP is mocked with respx, keys are generated per test, and nothing
leaves the process.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from osprey.config import settings
from osprey.engine import push_senders
from osprey.engine.notify import LoggingPushSender, PushMessage
from osprey.models import Device

MESSAGE = PushMessage("Notice of delay", "Respond in writing", {"item_id": "i1", "score": 88})


def _pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _device(platform: str, token: str) -> Device:
    return Device(org_id="o", user_id="u", platform=platform, token=token)


@pytest.fixture(autouse=True)
def plain_http(monkeypatch):
    """APNs asks httpx for HTTP/2, which needs the optional `h2` package.

    Drop the flag so respx can serve the request; what is under test is the
    request the sender builds, not the transport's protocol negotiation.
    """

    def client(**kwargs):
        kwargs.pop("http2", None)
        return httpx.AsyncClient(**kwargs)

    monkeypatch.setattr(push_senders, "httpx", SimpleNamespace(AsyncClient=client))


# --------------------------------------------------------------------------- #
# APNs
# --------------------------------------------------------------------------- #
@pytest.fixture
def apns(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(settings, "apns_team_id", "TEAM123")
    monkeypatch.setattr(settings, "apns_key_id", "KEY456")
    monkeypatch.setattr(settings, "apns_private_key", _pem(key))
    monkeypatch.setattr(settings, "apns_bundle_id", "dev.ospreyhq.mobile")
    monkeypatch.setattr(settings, "apns_use_sandbox", True)
    return key.public_key()


async def test_apns_sends_a_signed_alert_to_the_sandbox(apns):
    with respx.mock() as mock:
        route = mock.post(f"{push_senders.APNS_SANDBOX}/3/device/tok-1").respond(200)
        ok = await push_senders.ApnsSender().send(_device("ios", "tok-1"), MESSAGE)

    assert ok is True
    request = route.calls.last.request
    assert request.headers["apns-topic"] == "dev.ospreyhq.mobile"
    assert request.headers["apns-push-type"] == "alert"
    token = request.headers["authorization"].removeprefix("bearer ")
    assert jwt.get_unverified_header(token)["kid"] == "KEY456"
    # Signed by the configured key, not merely well-formed.
    assert jwt.decode(token, apns, algorithms=["ES256"])["iss"] == "TEAM123"
    assert json.loads(request.content)["aps"]["alert"]["title"] == "Notice of delay"


async def test_apns_reuses_its_provider_token(apns):
    # Apple throttles provider-token churn; one token should serve many sends.
    sender = push_senders.ApnsSender()
    with respx.mock() as mock:
        route = mock.post(url__startswith=push_senders.APNS_SANDBOX).respond(200)
        await sender.send(_device("ios", "a"), MESSAGE)
        await sender.send(_device("ios", "b"), MESSAGE)

    first, second = (c.request.headers["authorization"] for c in route.calls)
    assert first == second


async def test_apns_uses_production_when_not_sandboxed(apns, monkeypatch):
    monkeypatch.setattr(settings, "apns_use_sandbox", False)
    with respx.mock() as mock:
        route = mock.post(f"{push_senders.APNS_PROD}/3/device/tok").respond(200)
        assert await push_senders.ApnsSender().send(_device("ios", "tok"), MESSAGE) is True
    assert route.called


async def test_apns_rejection_and_network_errors_return_false(apns):
    sender = push_senders.ApnsSender()
    with respx.mock() as mock:
        mock.post(f"{push_senders.APNS_SANDBOX}/3/device/gone").respond(
            410, json={"reason": "Unregistered"}
        )
        mock.post(f"{push_senders.APNS_SANDBOX}/3/device/down").mock(
            side_effect=httpx.ConnectError("no route")
        )
        assert await sender.send(_device("ios", "gone"), MESSAGE) is False
        assert await sender.send(_device("ios", "down"), MESSAGE) is False


# --------------------------------------------------------------------------- #
# FCM
# --------------------------------------------------------------------------- #
@pytest.fixture
def fcm(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    account = {
        "client_email": "push@proj.iam.gserviceaccount.com",
        "private_key": _pem(key),
        "token_uri": push_senders.GOOGLE_TOKEN_URL,
        "project_id": "proj-from-account",
    }
    monkeypatch.setattr(settings, "fcm_service_account_json", json.dumps(account))
    monkeypatch.setattr(settings, "fcm_project_id", "")
    return key.public_key()


def _fcm_send_url(project: str) -> str:
    return f"https://fcm.googleapis.com/v1/projects/{project}/messages:send"


async def test_fcm_exchanges_a_signed_assertion_then_sends(fcm):
    with respx.mock() as mock:
        token_route = mock.post(push_senders.GOOGLE_TOKEN_URL).respond(
            200, json={"access_token": "ya29.test"}
        )
        send_route = mock.post(_fcm_send_url("proj-from-account")).respond(200)
        ok = await push_senders.FcmSender().send(_device("android", "fcm-tok"), MESSAGE)

    assert ok is True
    form = parse_qs(token_route.calls.last.request.content.decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    claims = jwt.decode(
        form["assertion"][0],
        fcm,
        algorithms=["RS256"],
        audience=push_senders.GOOGLE_TOKEN_URL,
    )
    assert claims["scope"] == push_senders.FCM_SCOPE
    sent = send_route.calls.last.request
    assert sent.headers["authorization"] == "Bearer ya29.test"
    assert json.loads(sent.content)["message"]["token"] == "fcm-tok"


async def test_fcm_caches_the_access_token_and_honours_a_project_override(fcm, monkeypatch):
    monkeypatch.setattr(settings, "fcm_project_id", "proj-override")
    sender = push_senders.FcmSender()
    with respx.mock() as mock:
        token_route = mock.post(push_senders.GOOGLE_TOKEN_URL).respond(
            200, json={"access_token": "ya29.test"}
        )
        mock.post(_fcm_send_url("proj-override")).respond(200)
        assert await sender.send(_device("android", "a"), MESSAGE) is True
        assert await sender.send(_device("android", "b"), MESSAGE) is True

    assert token_route.call_count == 1


async def test_fcm_does_not_send_when_the_token_exchange_fails(fcm):
    # The send route exists to prove it is NOT called, so it must not be required.
    with respx.mock(assert_all_called=False) as mock:
        mock.post(push_senders.GOOGLE_TOKEN_URL).respond(401, json={"error": "invalid_grant"})
        send_route = mock.post(_fcm_send_url("proj-from-account")).respond(200)
        ok = await push_senders.FcmSender().send(_device("android", "tok"), MESSAGE)

    assert ok is False
    assert not send_route.called


async def test_fcm_rejection_returns_false(fcm):
    with respx.mock() as mock:
        mock.post(push_senders.GOOGLE_TOKEN_URL).respond(200, json={"access_token": "ya29.test"})
        mock.post(_fcm_send_url("proj-from-account")).respond(404, json={"error": "NOT_FOUND"})
        assert await push_senders.FcmSender().send(_device("android", "stale"), MESSAGE) is False


# --------------------------------------------------------------------------- #
# Web Push
# --------------------------------------------------------------------------- #
SUBSCRIPTION = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}


async def test_webpush_sends_the_subscription_with_vapid_claims(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setitem(
        sys.modules, "pywebpush", SimpleNamespace(webpush=lambda **kw: calls.append(kw))
    )
    monkeypatch.setattr(settings, "vapid_private_key", "vapid-private")
    monkeypatch.setattr(settings, "vapid_subject", "mailto:ops@example.com")

    ok = await push_senders.WebPushSender().send(_device("web", json.dumps(SUBSCRIPTION)), MESSAGE)

    assert ok is True
    assert calls[0]["subscription_info"] == SUBSCRIPTION
    assert calls[0]["vapid_claims"] == {"sub": "mailto:ops@example.com"}
    assert json.loads(calls[0]["data"])["title"] == "Notice of delay"


async def test_webpush_failures_return_false(monkeypatch):
    def refuse(**_kw):
        raise RuntimeError("410 Gone")

    monkeypatch.setitem(sys.modules, "pywebpush", SimpleNamespace(webpush=refuse))
    sender = push_senders.WebPushSender()

    assert await sender.send(_device("web", json.dumps(SUBSCRIPTION)), MESSAGE) is False
    assert await sender.send(_device("web", "not a subscription"), MESSAGE) is False


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def test_build_sender_honours_the_configured_provider(monkeypatch, apns, fcm):
    monkeypatch.setattr(settings, "vapid_private_key", "vapid-private")

    for provider, expected in [
        ("apns", push_senders.ApnsSender),
        ("fcm", push_senders.FcmSender),
        ("webpush", push_senders.WebPushSender),
        ("auto", push_senders.CompositePushSender),
        ("logging", LoggingPushSender),
    ]:
        monkeypatch.setattr(settings, "push_provider", provider)
        assert isinstance(push_senders.build_sender(), expected), provider


def test_build_sender_falls_back_to_logging_when_credentials_are_missing(monkeypatch):
    monkeypatch.setattr(settings, "apns_private_key", "")
    monkeypatch.setattr(settings, "fcm_service_account_json", "")
    monkeypatch.setattr(settings, "vapid_private_key", "")
    for provider in ("apns", "fcm", "webpush", "auto"):
        monkeypatch.setattr(settings, "push_provider", provider)
        assert isinstance(push_senders.build_sender(), LoggingPushSender), provider
