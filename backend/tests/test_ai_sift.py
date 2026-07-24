"""User-connectable AI sift-to-hotlist."""

from __future__ import annotations

LD_EMAIL = """\
From: owner@dev.com
To: pm@gc.com
Subject: Liquidated damages exposure on Tower B
Date: Wed, 22 Jul 2026 08:00:00 -0500
Message-ID: <ld-1@dev.com>

Per the contract, liquidated damages of $5,000 per day apply after substantial
completion. We are tracking a potential 10-day slip. Please advise.
"""

RFI_EMAIL = """\
From: pm@gc.com
To: eng@ae.com
Subject: RFI-0500 — anchor spacing
Date: Wed, 22 Jul 2026 09:00:00 -0500
Message-ID: <rfi-0500@gc.com>

Please confirm anchor spacing for the curtain wall.
"""


async def _setup(client):
    project_id = (await client.post("/projects", json={"name": "Tower B"})).json()["id"]
    conn_id = (
        await client.post(
            "/connections",
            json={"project_id": project_id, "source_type": "filedrop", "account_ref": "drop"},
        )
    ).json()["id"]
    for raw in (LD_EMAIL, RFI_EMAIL):
        await client.post(f"/connections/{conn_id}/forward", json={"kind": "email", "raw": raw})
    await client.get(f"/projects/{project_id}/hotlist", params={"refresh": "true"})
    return project_id


async def test_ai_connection_create_hides_key(auth_client):
    client, _ = auth_client
    resp = await client.post(
        "/ai/connections",
        json={
            "provider": "claude",
            "label": "My Claude",
            "model": "claude-sonnet-5",
            "api_key": "sk-secret",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["has_key"] is True
    assert "api_key" not in body and "sk-secret" not in str(body)  # never returned
    listed = (await client.get("/ai/connections")).json()
    assert listed[0]["provider"] == "claude"


async def test_sift_pushes_matches_to_hotlist(auth_client):
    client, _ = auth_client
    project_id = await _setup(client)

    resp = await client.post(
        f"/ai/projects/{project_id}/sift",
        json={"instruction": "liquidated damages", "lookback_days": 3650},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["scanned_signals"] >= 2
    assert len(data["findings"]) >= 1
    finding = data["findings"][0]
    # The LD email is cited by the finding.
    assert finding["matched_signal_ids"]
    assert finding["item_id"]

    # The AI finding now appears on the hotlist as a real, scored item.
    hot = (await client.get(f"/projects/{project_id}/hotlist")).json()
    ai_items = [i for i in hot["items"] if "liquidated damages" in i["what"].lower()]
    assert ai_items


async def test_sift_no_match_returns_empty(auth_client):
    client, _ = auth_client
    project_id = await _setup(client)
    resp = await client.post(
        f"/ai/projects/{project_id}/sift",
        json={"instruction": "asbestos abatement zzz", "lookback_days": 3650},
    )
    assert resp.status_code == 200
    assert resp.json()["findings"] == []
