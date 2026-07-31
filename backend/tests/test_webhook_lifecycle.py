"""Graph subscription lifecycle: authentication, renewal, and recovery.

Microsoft Graph signs nothing, so a notification is authenticated by the
``clientState`` secret Osprey supplied when subscribing. Graph also reports
subscription trouble out-of-band — ``reauthorizationRequired``,
``subscriptionRemoved``, ``missed`` — and a connector that ignores those goes
quiet while still reporting itself healthy. These tests cover both.
"""

from __future__ import annotations

import json
import re

import httpx
import respx

from osprey.connectors.outlook import OutlookConnector
from osprey.connectors.service import (
    handle_lifecycle,
    subscription_urls,
    sync_subscription,
    to_view,
)
from osprey.models import Connection, ConnectionStatus, Org, Project
from osprey.security import crypto


async def _connection(session, *, tokens: dict | None = None) -> Connection:
    org = Org(name="Lifecycle Co")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name="Tower C")
    session.add(project)
    await session.flush()
    conn = Connection(
        org_id=org.id,
        project_id=project.id,
        source_type="outlook",
        account_ref="pm@gc.com",
        status=ConnectionStatus.active,
        encrypted_tokens=crypto.seal(tokens or {"refresh_token": "r0"}),
    )
    session.add(conn)
    await session.flush()
    return conn


def _token_route():
    respx.post(re.compile(r"https://login\.microsoftonline\.com/.*/oauth2/v2\.0/token")).mock(
        return_value=httpx.Response(200, json={"access_token": "graph-token"})
    )


# -- payload classification --------------------------------------------------- #


def test_lifecycle_events_are_recognised():
    connector = OutlookConnector()
    payload = {
        "value": [
            {"lifecycleEvent": "reauthorizationRequired", "subscriptionId": "s1"},
            {"lifecycleEvent": "missed", "subscriptionId": "s1"},
        ]
    }
    assert connector.lifecycle_events(payload) == ["reauthorizationRequired", "missed"]


def test_data_notifications_are_not_lifecycle_events():
    connector = OutlookConnector()
    assert connector.lifecycle_events({"value": [{"resourceData": {"id": "m1"}}]}) == []


def test_client_state_requires_agreement_across_entries():
    """A payload mixing a genuine entry with a forged one must not authenticate."""
    connector = OutlookConnector()
    assert connector.webhook_client_state({"value": [{"clientState": "secret"}]}) == "secret"
    assert (
        connector.webhook_client_state(
            {"value": [{"clientState": "secret"}, {"clientState": "forged"}]}
        )
        is None
    )
    assert connector.webhook_client_state({"value": []}) is None


# -- subscribing + persistence ------------------------------------------------ #


@respx.mock
async def test_subscription_id_is_persisted_so_renewal_extends(session):
    """The bug this guards: an unpersisted id makes every renewal leak a new sub."""
    _token_route()
    create = respx.post("https://graph.microsoft.com/v1.0/subscriptions").mock(
        return_value=httpx.Response(201, json={"id": "sub-1"})
    )
    conn = await _connection(session)

    state = await sync_subscription(session, conn, notify_base="https://osprey.example")
    assert state is not None and state.subscription_id == "sub-1"

    # The id and the shared secret must survive on the connection...
    tokens = to_view(conn).tokens
    assert tokens["subscription_id"] == "sub-1"
    assert tokens["client_state"]
    # ...and the OAuth refresh token must not have been clobbered.
    assert tokens["refresh_token"] == "r0"

    # A lifecycle URL was supplied, or Graph never reports missed notifications.
    body = json.loads(create.calls[0].request.content)
    assert body["lifecycleNotificationUrl"].endswith(
        f"/webhooks/outlook/lifecycle?connection_id={conn.id}"
    )
    assert body["clientState"] == tokens["client_state"]

    # Second pass extends the existing subscription instead of creating another.
    patch = respx.patch("https://graph.microsoft.com/v1.0/subscriptions/sub-1").mock(
        return_value=httpx.Response(200, json={"id": "sub-1"})
    )
    again = await sync_subscription(session, conn, notify_base="https://osprey.example")
    assert patch.called
    assert create.call_count == 1
    # The secret is stable across renewals, so in-flight notifications keep validating.
    assert again.client_state == state.client_state


@respx.mock
async def test_expired_subscription_falls_back_to_creating_one(session):
    _token_route()
    respx.patch(re.compile(r"https://graph\.microsoft\.com/v1\.0/subscriptions/.*")).mock(
        return_value=httpx.Response(404, json={"error": {"code": "ResourceNotFound"}})
    )
    create = respx.post("https://graph.microsoft.com/v1.0/subscriptions").mock(
        return_value=httpx.Response(201, json={"id": "sub-2"})
    )
    conn = await _connection(session, tokens={"refresh_token": "r0", "subscription_id": "gone"})

    state = await sync_subscription(session, conn, notify_base="https://osprey.example")

    assert create.called
    assert state.subscription_id == "sub-2"


def test_subscription_urls_do_not_double_up_slashes():
    class _Row:
        id = "c1"
        source_type = "outlook"

    notify, lifecycle = subscription_urls("https://osprey.example/", _Row())
    assert notify == "https://osprey.example/webhooks/outlook?connection_id=c1"
    assert lifecycle == "https://osprey.example/webhooks/outlook/lifecycle?connection_id=c1"


# -- reacting to lifecycle events --------------------------------------------- #


@respx.mock
async def test_reauthorization_required_resubscribes(session):
    _token_route()
    respx.patch(re.compile(r"https://graph\.microsoft\.com/v1\.0/subscriptions/.*")).mock(
        return_value=httpx.Response(200, json={"id": "sub-1"})
    )
    conn = await _connection(
        session, tokens={"refresh_token": "r0", "subscription_id": "sub-1", "client_state": "cs"}
    )

    result = await handle_lifecycle(
        session, conn, ["reauthorizationRequired"], notify_base="https://osprey.example"
    )

    assert "resubscribed" in result["actions"]


@respx.mock
async def test_subscription_removed_creates_a_fresh_subscription(session):
    """A removed subscription cannot be PATCHed, so the stale id must be dropped."""
    _token_route()
    patch = respx.patch(re.compile(r"https://graph\.microsoft\.com/v1\.0/subscriptions/.*")).mock(
        return_value=httpx.Response(404)
    )
    create = respx.post("https://graph.microsoft.com/v1.0/subscriptions").mock(
        return_value=httpx.Response(201, json={"id": "sub-new"})
    )
    conn = await _connection(
        session, tokens={"refresh_token": "r0", "subscription_id": "sub-old", "client_state": "cs"}
    )

    result = await handle_lifecycle(
        session, conn, ["subscriptionRemoved"], notify_base="https://osprey.example"
    )

    assert not patch.called, "must not try to extend a subscription Graph already removed"
    assert create.called
    assert "resubscribed" in result["actions"]
    assert to_view(conn).tokens["subscription_id"] == "sub-new"


@respx.mock
async def test_missed_triggers_a_catch_up_poll(session):
    """'missed' means Graph dropped notifications; only a poll recovers them."""
    _token_route()
    delta = respx.get(re.compile(r"https://graph\.microsoft\.com/v1\.0/me/mailFolders.*")).mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "m-missed",
                        "subject": "NOTICE OF DELAY — recovered after a missed callback",
                        "conversationId": "c1",
                        "from": {"emailAddress": {"address": "pm@gc.com"}},
                        "toRecipients": [],
                        "ccRecipients": [],
                        "body": {"content": "Written notice is required within 7 days."},
                        "receivedDateTime": "2026-07-30T09:00:00Z",
                    }
                ]
            },
        )
    )
    conn = await _connection(session)

    result = await handle_lifecycle(session, conn, ["missed"], notify_base="https://osprey.example")

    assert delta.called
    assert result["actions"] == ["polled:1"]


@respx.mock
async def test_resubscribe_failure_does_not_raise(session):
    """A provider outage must not turn into a 500 back at Graph."""
    _token_route()
    respx.patch(re.compile(r"https://graph\.microsoft\.com/v1\.0/subscriptions/.*")).mock(
        return_value=httpx.Response(500)
    )
    respx.post("https://graph.microsoft.com/v1.0/subscriptions").mock(
        return_value=httpx.Response(500)
    )
    conn = await _connection(
        session, tokens={"refresh_token": "r0", "subscription_id": "sub-1", "client_state": "cs"}
    )

    result = await handle_lifecycle(
        session, conn, ["reauthorizationRequired"], notify_base="https://osprey.example"
    )

    assert result["actions"] == []


# -- the HTTP surface --------------------------------------------------------- #


async def _api_connection(client) -> str:
    r = await client.post("/projects", json={"name": "Tower C"})
    project_id = r.json()["id"]
    r = await client.post(
        "/connections",
        json={
            "project_id": project_id,
            "source_type": "outlook",
            "account_ref": "pm@gc.com",
            "tokens": {"refresh_token": "r0", "client_state": "shared-secret"},
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def test_graph_notification_authenticates_with_client_state(auth_client):
    """Graph sends no HMAC — demanding one would reject every real notification."""
    client, _ = auth_client
    conn_id = await _api_connection(client)
    body = {
        "value": [
            {
                "clientState": "shared-secret",
                "resourceData": {
                    "id": "m1",
                    "subject": "RFI 104 response overdue",
                    "body": {"content": "Please respond."},
                    "receivedDateTime": "2026-07-30T09:00:00Z",
                },
            }
        ]
    }

    r = await client.post("/webhooks/outlook", params={"connection_id": conn_id}, json=body)

    assert r.status_code == 202, r.text
    assert r.json()["parsed"] == 1


async def test_graph_notification_with_wrong_client_state_is_rejected(auth_client):
    client, _ = auth_client
    conn_id = await _api_connection(client)

    r = await client.post(
        "/webhooks/outlook",
        params={"connection_id": conn_id},
        json={"value": [{"clientState": "guessed", "resourceData": {"id": "m1"}}]},
    )

    assert r.status_code == 401


async def test_graph_notification_without_client_state_is_rejected(auth_client):
    client, _ = auth_client
    conn_id = await _api_connection(client)

    r = await client.post(
        "/webhooks/outlook",
        params={"connection_id": conn_id},
        json={"value": [{"resourceData": {"id": "m1"}}]},
    )

    assert r.status_code == 401


async def test_lifecycle_endpoint_answers_the_validation_handshake(client):
    r = await client.post("/webhooks/outlook/lifecycle", params={"validationToken": "tok-9"})
    assert r.status_code == 200
    assert r.text == "tok-9"


async def test_lifecycle_endpoint_rejects_a_bad_secret(auth_client):
    client, _ = auth_client
    conn_id = await _api_connection(client)

    r = await client.post(
        "/webhooks/outlook/lifecycle",
        params={"connection_id": conn_id},
        json={"value": [{"lifecycleEvent": "missed", "clientState": "wrong"}]},
    )

    assert r.status_code == 401


async def test_lifecycle_endpoint_rejects_a_non_lifecycle_payload(auth_client):
    client, _ = auth_client
    conn_id = await _api_connection(client)

    r = await client.post(
        "/webhooks/outlook/lifecycle",
        params={"connection_id": conn_id},
        json={"value": [{"clientState": "shared-secret", "resourceData": {"id": "m1"}}]},
    )

    assert r.status_code == 400
