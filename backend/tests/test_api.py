"""REST API: auth, project, connection, ingest, hotlist, items, exports, RBAC, webhook."""

from __future__ import annotations

import hashlib
import hmac
import json

RFI_EMAIL = """\
From: pm@gc.com
To: eng@ae.com
Subject: RFI-0500 — curtain wall anchor spacing
Date: Wed, 22 Jul 2026 09:00:00 -0500
Message-ID: <rfi-0500@gc.com>

Please confirm anchor spacing for the curtain wall. Blocks fabrication. Due 2026-07-30.
"""


async def _make_project_with_connection(client):
    pr = await client.post("/projects", json={"name": "Tower B"})
    assert pr.status_code == 201, pr.text
    project_id = pr.json()["id"]
    cr = await client.post(
        "/connections",
        json={"project_id": project_id, "source_type": "filedrop", "account_ref": "drop@in.osprey"},
    )
    assert cr.status_code == 201, cr.text
    return project_id, cr.json()["id"]


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    ready = await client.get("/ready")
    assert ready.json()["ready"] is True
    assert "filedrop" in ready.json()["connectors"]


async def test_auth_register_login(client):
    r = await client.post(
        "/auth/register",
        json={"email": "a@b.com", "password": "password123", "org_name": "Acme"},
    )
    assert r.status_code == 201
    assert r.json()["role"] == "owner"
    r2 = await client.post("/auth/login", json={"email": "a@b.com", "password": "password123"})
    assert r2.status_code == 200
    r3 = await client.post("/auth/login", json={"email": "a@b.com", "password": "wrong"})
    assert r3.status_code == 401


async def test_end_to_end_hotlist_and_exports(auth_client):
    client, _ = auth_client
    project_id, conn_id = await _make_project_with_connection(client)

    fwd = await client.post(
        f"/connections/{conn_id}/forward", json={"kind": "email", "raw": RFI_EMAIL}
    )
    assert fwd.status_code == 202, fwd.text
    assert fwd.json()["created"] == 1

    hot = await client.get(f"/projects/{project_id}/hotlist", params={"refresh": "true"})
    assert hot.status_code == 200
    payload = hot.json()
    assert payload["item_count"] == 1
    assert payload["items"][0]["category"] == "rfi"

    items = await client.get(f"/projects/{project_id}/items")
    assert items.status_code == 200
    item_id = items.json()[0]["id"]

    detail = await client.get(f"/items/{item_id}")
    assert detail.status_code == 200
    assert detail.json()["factors"]  # explainable breakdown present

    xlsx = await client.get(f"/projects/{project_id}/hotlist/export", params={"format": "xlsx"})
    assert xlsx.status_code == 200
    assert xlsx.content[:2] == b"PK"
    pdf = await client.get(f"/projects/{project_id}/hotlist/export", params={"format": "pdf"})
    assert pdf.content[:5] == b"%PDF-"

    # Action feedback loop.
    act = await client.post(f"/items/{item_id}/actions", json={"type": "escalate"})
    assert act.status_code == 201


async def test_rbac_viewer_cannot_act(auth_client, client):
    owner_client, owner = auth_client
    project_id, conn_id = await _make_project_with_connection(owner_client)
    await owner_client.post(
        f"/connections/{conn_id}/forward", json={"kind": "email", "raw": RFI_EMAIL}
    )
    await owner_client.get(f"/projects/{project_id}/hotlist", params={"refresh": "true"})
    item_id = (await owner_client.get(f"/projects/{project_id}/items")).json()[0]["id"]

    # A viewer token (forge a low-privilege principal in the same org).
    from osprey.models import Role
    from osprey.security.auth import Principal, create_access_token

    viewer = Principal(user_id="v1", org_id=owner["org_id"], role=Role.viewer, email="v@x.com")
    vtoken = create_access_token(viewer)

    r = await client.post(
        f"/items/{item_id}/actions",
        json={"type": "dismiss"},
        headers={"Authorization": f"Bearer {vtoken}"},
    )
    assert r.status_code == 403


async def test_unauthenticated_is_rejected(client):
    r = await client.get("/projects")
    assert r.status_code == 401


async def test_webhook_requires_valid_signature(auth_client):
    client, _ = auth_client
    project_id, conn_id = await _make_project_with_connection(client)
    body = json.dumps({"kind": "email", "raw": RFI_EMAIL}).encode()

    # Wrong signature -> 401.
    bad = await client.post(
        "/webhooks/filedrop",
        params={"connection_id": conn_id},
        content=body,
        headers={"X-Osprey-Signature": "sha256=deadbeef"},
    )
    assert bad.status_code == 401

    # Correct HMAC -> accepted + ingested.
    sig = hmac.new(b"test-hmac-secret", body, hashlib.sha256).hexdigest()
    ok = await client.post(
        "/webhooks/filedrop",
        params={"connection_id": conn_id},
        content=body,
        headers={"X-Osprey-Signature": f"sha256={sig}"},
    )
    assert ok.status_code == 202, ok.text
    assert ok.json()["created"] == 1


async def test_graph_webhook_validation_handshake(client):
    r = await client.post("/webhooks/outlook", params={"validationToken": "tok-123"})
    assert r.status_code == 200
    assert r.text == "tok-123"
