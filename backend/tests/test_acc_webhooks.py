"""ACC webhooks: opt-in only, idempotent hook registration, and signed callbacks.

Endpoint paths, the hook body, the 409-on-duplicate and 400-when-a-token-exists
behaviours, and the ``x-adsk-signature: sha1hash=<hex HMAC-SHA1>`` scheme follow
Autodesk's published Webhooks API reference and its official signature-verification
sample. Nothing here talks to a live ACC project.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlparse

import httpx
import respx

from osprey.config import settings
from osprey.connectors import acc
from osprey.connectors.base import Connection as ConnView
from osprey.connectors.service import sync_subscription, to_view
from osprey.models import Connection, ConnectionStatus, Org, Project
from osprey.security import crypto
from osprey.security.oauth import verify_state

API = "https://developer.api.autodesk.com"
PROJECT = "0f9e8d7c-6b5a-4938-2716-0a1b2c3d4e5f"
HOOKS = f"{API}/webhooks/v1/systems/autodesk.construction.issues/events"
NOTIFY = "https://osprey.example/webhooks/acc?connection_id=c1"


def _view(scopes: list[str], **tokens: str) -> ConnView:
    return ConnView(
        id="c1",
        source_type="acc",
        account_ref=f"b.{PROJECT}",
        tokens={"access_token": "tok", **tokens},
        scopes=scopes,
    )


def _created(event: str, hook_id: str) -> httpx.Response:
    return httpx.Response(201, headers={"Location": f"{HOOKS}/{event}/hooks/{hook_id}"})


async def _row(session, *, scopes: list[str], tokens: dict) -> Connection:
    org = Org(name="ACC Co")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name="Tower C")
    session.add(project)
    await session.flush()
    row = Connection(
        org_id=org.id,
        project_id=project.id,
        source_type="acc",
        account_ref=PROJECT,
        scopes=scopes,
        encrypted_tokens=crypto.seal(tokens),
        status=ConnectionStatus.active,
    )
    session.add(row)
    await session.commit()
    return row


# --------------------------------------------------------------------------- #
# Opt-in
# --------------------------------------------------------------------------- #
async def test_without_the_opt_in_nothing_is_registered():
    with respx.mock() as mock:
        state = await acc.AccConnector().ensure_subscription(_view(["data:read"]), NOTIFY)

    assert state is None
    assert not mock.calls  # not a single request to Autodesk


def test_the_write_scope_is_optional_and_explained():
    connector = acc.AccConnector()

    assert connector.scopes == ["data:read"]
    assert "data:write" not in connector.scopes
    assert "never edits project data" in connector.optional_scopes["data:write"]


async def test_authorize_requests_the_write_scope_only_when_opted_into(auth_client, monkeypatch):
    client, _ = auth_client
    monkeypatch.setattr(settings, "acc_client_id", "aps-client")
    project_id = (await client.post("/projects", json={"name": "Tower C"})).json()["id"]
    redirect = "http://127.0.0.1:53682/callback"

    async def authorize(optional: list[str]) -> httpx.Response:
        return await client.post(
            "/connections/authorize",
            json={
                "project_id": project_id,
                "source_type": "acc",
                "redirect_uri": redirect,
                "optional_scopes": optional,
            },
        )

    plain = parse_qs(urlparse((await authorize([])).json()["authorize_url"]).query)
    assert plain["scope"] == ["data:read"]

    opted = (await authorize(["data:write"])).json()
    qs = parse_qs(urlparse(opted["authorize_url"]).query)
    assert qs["scope"] == ["data:read data:write"]
    assert verify_state(opted["state"])["opt"] == ["data:write"]

    refused = await authorize(["account:write"])
    assert refused.status_code == 400
    assert "does not offer" in refused.json()["detail"]

    sources = {s["source_type"]: s for s in (await client.get("/connections/sources")).json()}
    assert "data:write" in sources["acc"]["optional_scopes"]


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
async def test_first_sync_registers_a_secret_and_both_hooks():
    with respx.mock() as mock:
        token = mock.post(f"{API}/webhooks/v1/tokens").respond(200)
        created = mock.post(f"{HOOKS}/issue.created-1.0/hooks").mock(
            return_value=_created("issue.created-1.0", "hook-c")
        )
        updated = mock.post(f"{HOOKS}/issue.updated-1.0/hooks").mock(
            return_value=_created("issue.updated-1.0", "hook-u")
        )
        state = await acc.AccConnector().ensure_subscription(
            _view(["data:read", "data:write"]), NOTIFY
        )

    assert state.subscription_id == "hook-c,hook-u"
    secret = json.loads(token.calls.last.request.content)["token"]
    assert state.client_state == secret
    assert 32 <= len(secret) <= 64 and secret.isalnum()
    body = json.loads(created.calls.last.request.content)
    assert body["callbackUrl"] == NOTIFY
    assert body["scope"] == {"project": PROJECT}  # Issues hooks scope to the bare project id
    assert body["autoReactivateHook"] is True
    assert updated.called
    assert created.calls.last.request.headers["authorization"] == "Bearer tok"


async def test_an_existing_secret_for_the_user_is_replaced():
    with respx.mock() as mock:
        mock.post(f"{API}/webhooks/v1/tokens").respond(400, json={"detail": ["exists"]})
        put = mock.put(f"{API}/webhooks/v1/tokens/@me").respond(200)
        mock.post(url__regex=rf"{HOOKS}/.+/hooks$").mock(return_value=_created("e", "h"))
        state = await acc.AccConnector().ensure_subscription(
            _view(["data:read", "data:write"]), NOTIFY
        )

    assert put.called
    assert json.loads(put.calls.last.request.content)["token"] == state.client_state


async def test_a_duplicate_hook_is_found_rather_than_failing():
    listing = {
        "data": [
            {"hookId": "someone-else", "callbackUrl": "https://other.example/cb"},
            {"hookId": "ours", "callbackUrl": NOTIFY},
        ]
    }
    with respx.mock() as mock:
        mock.post(f"{API}/webhooks/v1/tokens").respond(200)
        mock.post(url__regex=rf"{HOOKS}/.+/hooks$").respond(409)
        # The listing carries scopeName/scopeValue in its query string.
        listed = mock.get(url__regex=rf"{HOOKS}/.+/hooks\?.*$").respond(200, json=listing)
        state = await acc.AccConnector().ensure_subscription(
            _view(["data:read", "data:write"]), NOTIFY
        )

    assert state.subscription_id == "ours,ours"
    assert listed.calls.last.request.url.params["scopeValue"] == PROJECT


async def test_renewal_keeps_live_hooks_and_recreates_a_missing_one():
    view = _view(
        ["data:read", "data:write"], webhook_secret="a" * 48, subscription_id="hook-c,hook-gone"
    )
    # The token route exists to prove it is NOT called, so it must not be required.
    with respx.mock(assert_all_called=False) as mock:
        token = mock.post(f"{API}/webhooks/v1/tokens").respond(200)
        mock.get(f"{HOOKS}/issue.created-1.0/hooks/hook-c").respond(200, json={"status": "active"})
        mock.get(f"{HOOKS}/issue.updated-1.0/hooks/hook-gone").respond(404)
        recreated = mock.post(f"{HOOKS}/issue.updated-1.0/hooks").mock(
            return_value=_created("issue.updated-1.0", "hook-u2")
        )
        state = await acc.AccConnector().ensure_subscription(view, NOTIFY)

    assert state.subscription_id == "hook-c,hook-u2"
    assert state.client_state == "a" * 48  # the secret is stable across renewals
    assert not token.called
    assert recreated.call_count == 1


async def test_the_secret_is_stored_where_the_webhook_route_reads_it(session):
    row = await _row(session, scopes=["data:read", "data:write"], tokens={"access_token": "tok"})
    with respx.mock() as mock:
        mock.post(f"{API}/webhooks/v1/tokens").respond(200)
        mock.post(url__regex=rf"{HOOKS}/.+/hooks$").mock(return_value=_created("e", "h"))
        state = await sync_subscription(session, row, notify_base="https://osprey.example")

    tokens = to_view(row).tokens
    assert tokens["webhook_secret"] == state.client_state
    assert tokens["access_token"] == "tok"  # not clobbered


# --------------------------------------------------------------------------- #
# Callbacks
# --------------------------------------------------------------------------- #
def test_signature_verification():
    connector = acc.AccConnector()
    raw = b'{"hook": {"event": "issue.updated-1.0"}, "payload": {"x": 1}}'
    good = "sha1hash=" + hmac.new(b"s3cret", raw, hashlib.sha1).hexdigest()

    assert connector.verify_webhook_signature(raw, {"x-adsk-signature": good}, "s3cret")
    assert not connector.verify_webhook_signature(raw, {"x-adsk-signature": good}, "wrong")
    assert not connector.verify_webhook_signature(raw + b" ", {"x-adsk-signature": good}, "s3cret")
    assert not connector.verify_webhook_signature(raw, {}, "s3cret")


async def test_a_signed_callback_polls_the_project(client, session, monkeypatch):
    row = await _row(
        session,
        scopes=["data:read", "data:write"],
        tokens={"access_token": "tok", "webhook_secret": "s3cret"},
    )
    polled: list[str] = []

    async def fake_poll(_session, connection_id):
        polled.append(connection_id)
        return {"created": 2}

    monkeypatch.setattr("osprey.workers.tasks.poll_connection", fake_poll)
    raw = json.dumps({"hook": {"event": "issue.updated-1.0"}, "payload": {}}).encode()

    signed = await client.post(
        "/webhooks/acc",
        params={"connection_id": row.id},
        content=raw,
        headers={
            "x-adsk-signature": acc.sign_body(raw, "s3cret"),
            "content-type": "application/json",
        },
    )
    assert signed.status_code == 202, signed.text
    assert signed.json()["actions"] == ["polled:2"]
    assert polled == [row.id]

    forged = await client.post(
        "/webhooks/acc",
        params={"connection_id": row.id},
        content=raw,
        headers={"x-adsk-signature": acc.sign_body(raw, "not-the-secret")},
    )
    assert forged.status_code == 401
    assert polled == [row.id]


async def test_a_connection_that_never_subscribed_rejects_every_callback(client, session):
    row = await _row(session, scopes=["data:read"], tokens={"access_token": "tok"})
    raw = b'{"hook": {}}'

    resp = await client.post(
        "/webhooks/acc",
        params={"connection_id": row.id},
        content=raw,
        headers={"x-adsk-signature": acc.sign_body(raw, "")},
    )
    assert resp.status_code == 401
