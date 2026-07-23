"""Admin console: stats, audit verification, connection health, feature flags."""

from __future__ import annotations


async def test_admin_stats_and_health(auth_client):
    client, _ = auth_client
    project_id = (await client.post("/projects", json={"name": "Tower B"})).json()["id"]
    await client.post(
        "/connections",
        json={"project_id": project_id, "source_type": "filedrop", "account_ref": "drop"},
    )

    stats = (await client.get("/admin/stats")).json()
    assert stats["projects"] == 1
    assert stats["connections"] == 1

    health = (await client.get("/admin/connections/health")).json()
    assert health[0]["source_type"] == "filedrop"
    assert health[0]["status"] == "active"


async def test_admin_audit_chain_intact(auth_client):
    client, _ = auth_client
    await client.post("/projects", json={"name": "P"})   # generates audit entries
    verify = (await client.get("/admin/audit/verify")).json()
    assert verify["audit_chain_intact"] is True


async def test_admin_features(auth_client):
    client, _ = auth_client
    feats = (await client.get("/admin/features")).json()
    assert feats["ai_sift"] is True
    assert feats["scripts"] is True


async def test_admin_requires_admin_role(auth_client, client):
    _, owner = auth_client
    from osprey.models import Role
    from osprey.security.auth import Principal, create_access_token

    viewer = Principal(user_id="v", org_id=owner["org_id"], role=Role.viewer, email="v@x.com")
    token = create_access_token(viewer)
    r = await client.get("/admin/stats", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
