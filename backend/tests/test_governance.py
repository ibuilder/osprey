"""Retention, subject-access export, and right-to-delete."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from osprey.config import settings
from osprey.engine import retention
from osprey.models import (
    Connection,
    Item,
    ItemStatus,
    Org,
    Project,
    Signal,
    SourceKind,
    User,
    utcnow,
)
from osprey.security import audit


def _aged(days: int):
    """A timestamp `days` old, in the convention this backend stores."""
    stamp = utcnow() - timedelta(days=days)
    return stamp.replace(tzinfo=None) if settings.is_sqlite else stamp


async def _seed(session, org_id: str, *, age_days: int, status=ItemStatus.done):
    """A project with one item and one signal, both aged `age_days`.

    Items age on `updated_at` and signals on `ingested_at` -- the columns the
    retention windows actually measure.
    """
    project = Project(org_id=org_id, name="Retention Site")
    session.add(project)
    await session.flush()
    connection = Connection(
        org_id=org_id, project_id=project.id, source_type="filedrop", account_ref="drop"
    )
    session.add(connection)
    await session.flush()
    item = Item(
        project_id=project.id,
        title="Old thing",
        summary="",
        status=status,
        created_at=_aged(age_days),
        updated_at=_aged(age_days),
    )
    session.add(item)
    await session.flush()
    signal = Signal(
        project_id=project.id,
        connection_id=connection.id,
        source_type="filedrop",
        source_kind=SourceKind.email,
        external_id=f"ext-{age_days}-{status.value}",
        title="Old signal",
        body="",
        ingested_at=_aged(age_days),
    )
    session.add(signal)
    await session.commit()
    return project, item, signal


# --------------------------------------------------------------------------- #
# Retention policy
# --------------------------------------------------------------------------- #
async def test_retention_defaults_to_keeping_everything(auth_client):
    owner_client, _ = auth_client
    body = (await owner_client.get("/orgs/current/retention")).json()
    assert body["effective_signal_days"] == 0
    assert body["effective_item_days"] == 0


async def test_a_tenant_can_shorten_its_own_window(auth_client):
    owner_client, _ = auth_client
    resp = await owner_client.put(
        "/orgs/current/retention", json={"signal_days": 30, "item_days": 90}
    )
    assert resp.status_code == 200
    assert resp.json()["effective_signal_days"] == 30

    again = (await owner_client.get("/orgs/current/retention")).json()
    assert again["signal_days"] == 30
    assert again["item_days"] == 90


async def test_org_override_beats_the_deployment_default(monkeypatch):
    monkeypatch.setattr(settings, "retention_signal_days", 365)
    org = Org(name="X", retention_signal_days=7)
    assert retention.effective_windows(org)[0] == 7
    # None inherits.
    assert retention.effective_windows(Org(name="Y"))[0] == 365


async def test_preview_reports_what_would_be_deleted(auth_client, session):
    owner_client, owner = auth_client
    await _seed(session, owner["org_id"], age_days=400)
    await owner_client.put("/orgs/current/retention", json={"signal_days": 30, "item_days": 30})

    preview = (await owner_client.get("/orgs/current/retention/preview")).json()
    assert preview["signals"] == 1
    assert preview["items"] == 1
    assert preview["cutoff_signal"]


async def test_preview_does_not_delete(auth_client, session):
    owner_client, owner = auth_client
    await _seed(session, owner["org_id"], age_days=400)
    await owner_client.put("/orgs/current/retention", json={"signal_days": 30, "item_days": 30})

    await owner_client.get("/orgs/current/retention/preview")
    assert len((await session.execute(select(Signal))).scalars().all()) == 1


async def test_purge_removes_expired_rows(auth_client, session):
    owner_client, owner = auth_client
    await _seed(session, owner["org_id"], age_days=400)
    await owner_client.put("/orgs/current/retention", json={"signal_days": 30, "item_days": 30})

    removed = (await owner_client.post("/orgs/current/retention/run")).json()
    assert removed["signals"] == 1
    assert removed["items"] == 1
    assert (await session.execute(select(Signal))).scalars().all() == []


async def test_an_open_item_is_never_purged_however_old(auth_client, session):
    """An unanswered notice deadline is precisely what must not vanish quietly."""
    owner_client, owner = auth_client
    await _seed(session, owner["org_id"], age_days=3650, status=ItemStatus.open)
    await owner_client.put("/orgs/current/retention", json={"signal_days": 0, "item_days": 1})

    removed = (await owner_client.post("/orgs/current/retention/run")).json()
    assert removed["items"] == 0
    assert len((await session.execute(select(Item))).scalars().all()) == 1


async def test_recent_rows_survive(auth_client, session):
    owner_client, owner = auth_client
    await _seed(session, owner["org_id"], age_days=1)
    await owner_client.put("/orgs/current/retention", json={"signal_days": 30, "item_days": 30})

    removed = (await owner_client.post("/orgs/current/retention/run")).json()
    assert removed["signals"] == 0
    assert removed["items"] == 0


async def test_purging_a_signal_does_not_require_purging_its_item(auth_client, session):
    """Signals age out faster than items; the item keeps the decision record."""
    owner_client, owner = auth_client
    project, item, signal = await _seed(session, owner["org_id"], age_days=200)
    signal.item_id = item.id
    session.add(signal)
    await session.commit()

    await owner_client.put("/orgs/current/retention", json={"signal_days": 30, "item_days": 3650})
    removed = (await owner_client.post("/orgs/current/retention/run")).json()
    assert removed["signals"] == 1
    assert removed["items"] == 0
    assert await session.get(Item, item.id) is not None


async def test_only_an_owner_may_change_retention(auth_client, client):
    owner_client, _ = auth_client
    invite = (
        await owner_client.post(
            "/orgs/current/invites", json={"email": "adm-ret@example.com", "role": "admin"}
        )
    ).json()
    admin = (
        await client.post(
            "/invites/accept", json={"token": invite["token"], "password": "Sup3rSecret!pass"}
        )
    ).json()
    headers = {"Authorization": f"Bearer {admin['access_token']}"}

    # An admin may look...
    assert (await client.get("/orgs/current/retention", headers=headers)).status_code == 200
    # ...but not change it.
    resp = await client.put("/orgs/current/retention", json={"signal_days": 1}, headers=headers)
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Audit chain under truncation
# --------------------------------------------------------------------------- #
async def test_audit_prefix_truncation_keeps_the_chain_verifiable(session, monkeypatch):
    org = Org(name="AuditCo")
    session.add(org)
    await session.flush()
    for index in range(6):
        entry = await audit.record(
            session, org_id=org.id, actor="a@b.c", action=f"act.{index}", target=str(index)
        )
        if index < 3:
            entry.created_at = _aged(400)
            session.add(entry)
    await session.commit()

    monkeypatch.setattr(settings, "retention_audit_days", 30)
    removed = await retention._purge_audit_prefix(session, org.id)
    await session.commit()
    assert removed == 3

    detail = await audit.verify_chain_detail(session, org.id)
    assert detail["valid"] is True
    # ...but it must not claim to be a complete chain any more.
    assert detail["anchored_at_genesis"] is False


async def test_a_tampered_audit_record_is_detected(session):
    org = Org(name="TamperCo")
    session.add(org)
    await session.flush()
    for index in range(4):
        await audit.record(session, org_id=org.id, actor="a@b.c", action=f"x.{index}")
    await session.commit()

    from osprey.models import AuditLog

    rows = (
        (await session.execute(select(AuditLog).where(AuditLog.org_id == org.id))).scalars().all()
    )
    rows[2].action = "x.tampered"
    session.add(rows[2])
    await session.commit()

    detail = await audit.verify_chain_detail(session, org.id)
    assert detail["valid"] is False
    assert detail["broken_at"] == 2


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
async def test_export_returns_the_tenant_contents(auth_client, session):
    owner_client, owner = auth_client
    await _seed(session, owner["org_id"], age_days=1)

    resp = await owner_client.get("/orgs/current/export")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    body = resp.json()
    assert body["org"]["id"] == owner["org_id"]
    assert body["members"][0]["email"] == "owner@example.com"
    assert len(body["signals"]) == 1
    assert body["format_version"] == 1


async def test_export_never_includes_connector_tokens(auth_client, session):
    """Exporting the vault would defeat the point of having one."""
    from osprey.security.crypto import seal

    owner_client, owner = auth_client
    project = Project(org_id=owner["org_id"], name="P")
    session.add(project)
    await session.flush()
    session.add(
        Connection(
            org_id=owner["org_id"],
            project_id=project.id,
            source_type="outlook",
            encrypted_tokens=seal({"access_token": "super-secret-value"}),
        )
    )
    await session.commit()

    body = (await owner_client.get("/orgs/current/export")).json()
    assert body["connections"][0]["has_credentials"] is True
    assert "encrypted_tokens" not in body["connections"][0]
    assert "super-secret-value" not in (await owner_client.get("/orgs/current/export")).text


async def test_export_requires_owner(auth_client, client):
    owner_client, _ = auth_client
    invite = (
        await owner_client.post(
            "/orgs/current/invites", json={"email": "pm-exp@example.com", "role": "pm"}
        )
    ).json()
    pm = (
        await client.post(
            "/invites/accept", json={"token": invite["token"], "password": "Sup3rSecret!pass"}
        )
    ).json()
    resp = await client.get(
        "/orgs/current/export", headers={"Authorization": f"Bearer {pm['access_token']}"}
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Right to delete
# --------------------------------------------------------------------------- #
async def test_erasure_requires_the_exact_org_name(auth_client):
    owner_client, _ = auth_client
    resp = await owner_client.post("/orgs/current/delete", json={"confirm_org_name": "wrong"})
    assert resp.status_code == 400


async def test_erasure_removes_the_tenant(auth_client, session):
    owner_client, owner = auth_client
    await _seed(session, owner["org_id"], age_days=1)

    resp = await owner_client.post("/orgs/current/delete", json={"confirm_org_name": "Tower B GC"})
    assert resp.status_code == 200
    assert resp.json()["completed"] is True

    assert await session.get(Org, owner["org_id"]) is None
    assert (await session.execute(select(Project))).scalars().all() == []
    assert (await session.execute(select(Signal))).scalars().all() == []
    # The user had no other membership, so their account goes too.
    assert await session.get(User, owner["user_id"]) is None


async def test_erasure_keeps_a_user_who_belongs_to_another_tenant(auth_client, client, session):
    owner_client, owner = auth_client
    # Same person, second org.
    second = await client.post(
        "/auth/register",
        json={
            "email": "multi@example.com",
            "password": "Sup3rSecret!pass",
            "org_name": "Second Co",
        },
    )
    second_user = second.json()["user_id"]
    invite = (
        await owner_client.post(
            "/orgs/current/invites", json={"email": "multi@example.com", "role": "pm"}
        )
    ).json()
    await client.post(
        "/invites/accept",
        json={"token": invite["token"], "password": "Sup3rSecret!pass"},
    )

    await owner_client.post("/orgs/current/delete", json={"confirm_org_name": "Tower B GC"})
    assert await session.get(User, second_user) is not None


async def test_settings_endpoint_reports_the_posture(auth_client):
    owner_client, _ = auth_client
    body = (await owner_client.get("/orgs/current/settings")).json()
    assert "retention" in body
    assert body["audit"]["valid"] is True
    assert body["rls_enabled"] == settings.rls_enabled
