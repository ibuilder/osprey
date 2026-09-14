"""Queued tenant erasure: a tenant too large to erase inline drains on the worker.

Inline erasure (the default) is covered in test_governance.py. These tests turn
the inline limit down so a small seeded tenant counts as "large".
"""

from __future__ import annotations

from sqlalchemy import select

from osprey.config import settings
from osprey.engine import retention
from osprey.models import (
    AuditLog,
    Connection,
    ConnectionStatus,
    Item,
    Org,
    Project,
    Score,
    Signal,
    SourceKind,
    User,
)
from osprey.workers import tasks


async def _bulk(session, org_id: str, *, signals: int = 4) -> Connection:
    """A project with one scored item and several signals."""
    project = Project(org_id=org_id, name="Big Site")
    session.add(project)
    await session.flush()
    connection = Connection(
        org_id=org_id,
        project_id=project.id,
        source_type="filedrop",
        account_ref="drop",
        status=ConnectionStatus.active,
    )
    session.add(connection)
    await session.flush()
    item = Item(project_id=project.id, title="Thing", summary="")
    session.add(item)
    await session.flush()
    session.add(Score(item_id=item.id, total=50.0))
    for n in range(signals):
        session.add(
            Signal(
                project_id=project.id,
                connection_id=connection.id,
                source_type="filedrop",
                source_kind=SourceKind.email,
                external_id=f"bulk-{n}",
                item_id=item.id,
            )
        )
    await session.commit()
    return connection


async def _queue_deletion(owner_client, monkeypatch, *, limit: int = 1):
    monkeypatch.setattr(settings, "erasure_inline_max_rows", limit)
    return await owner_client.post("/orgs/current/delete", json={"confirm_org_name": "Tower B GC"})


async def test_a_tenant_over_the_limit_is_queued_not_erased(auth_client, session, monkeypatch):
    owner_client, owner = auth_client
    await _bulk(session, owner["org_id"])

    resp = await _queue_deletion(owner_client, monkeypatch)
    assert resp.status_code == 202
    assert resp.json()["completed"] is False
    assert resp.json()["requested_at"]

    session.expire_all()
    org = await session.get(Org, owner["org_id"])
    assert org is not None and org.deletion_requested_at is not None
    assert len((await session.execute(select(Signal))).scalars().all()) == 4


async def test_a_tenant_under_the_limit_is_still_erased_inline(auth_client, session, monkeypatch):
    owner_client, owner = auth_client
    await _bulk(session, owner["org_id"], signals=1)

    resp = await _queue_deletion(owner_client, monkeypatch, limit=10_000)
    assert resp.status_code == 200
    assert resp.json()["completed"] is True
    session.expire_all()
    assert await session.get(Org, owner["org_id"]) is None


async def test_a_queued_tenant_is_locked_but_can_watch_its_deletion(
    auth_client, session, monkeypatch
):
    owner_client, owner = auth_client
    await _bulk(session, owner["org_id"])
    await _queue_deletion(owner_client, monkeypatch)

    assert (await owner_client.get("/projects")).status_code == 423
    status = await owner_client.get("/orgs/current/deletion-status")
    assert status.status_code == 200
    assert status.json()["requested_at"]
    assert status.json()["completed"] is False


async def test_the_worker_drains_a_queued_tenant_in_batches(auth_client, session, monkeypatch):
    owner_client, owner = auth_client
    await _bulk(session, owner["org_id"], signals=5)
    await _queue_deletion(owner_client, monkeypatch)
    monkeypatch.setattr(settings, "erasure_batch_rows", 2)

    runs = 0
    while True:
        runs += 1
        result = await tasks.drain_erasures(session)
        await session.commit()
        if result["finished"]:
            break
        assert result == {"queued": 1, "finished": 0}
        assert runs < 20, "erasure never finished"

    # 1 score + 5 signals + 1 item + audit rows, two at a time: several runs.
    assert runs >= 4
    session.expire_all()
    assert await session.get(Org, owner["org_id"]) is None
    for model in (Project, Signal, Item, Score, Connection):
        assert (await session.execute(select(model))).scalars().all() == []
    assert (
        await session.execute(select(AuditLog).where(AuditLog.org_id == owner["org_id"]))
    ).scalars().all() == []
    assert await session.get(User, owner["user_id"]) is None
    assert await tasks.drain_erasures(session) == {"queued": 0, "finished": 0}


async def test_draining_leaves_other_tenants_alone(auth_client, session, monkeypatch):
    owner_client, owner = auth_client
    await _bulk(session, owner["org_id"])
    other = Org(name="Neighbour Co")
    session.add(other)
    await session.flush()
    other_id = other.id
    other_connection_id = (await _bulk(session, other_id, signals=2)).id
    await _queue_deletion(owner_client, monkeypatch)

    for _ in range(10):
        if (await tasks.drain_erasures(session))["finished"]:
            break
    await session.commit()
    session.expire_all()
    remaining = (await session.execute(select(Signal))).scalars().all()
    assert {s.connection_id for s in remaining} == {other_connection_id}
    assert await session.get(Org, other_id) is not None


async def test_row_count_covers_the_bulk_tables(session):
    org = Org(name="Counted Co")
    session.add(org)
    await session.flush()
    await _bulk(session, org.id, signals=3)
    # 3 signals + 1 item + 1 score; no audit rows for a directly seeded org.
    assert await retention.count_org_rows(session, org.id) == 5


async def test_a_queued_tenant_is_not_polled_or_renewed(auth_client, session, monkeypatch):
    owner_client, owner = auth_client
    await _bulk(session, owner["org_id"])
    other = Org(name="Still Here Co")
    session.add(other)
    await session.flush()
    await _bulk(session, other.id, signals=1)
    await _queue_deletion(owner_client, monkeypatch)

    polled: list[str] = []

    async def fake_poll(session, connection_id):
        polled.append(connection_id)
        return {"created": 0}

    monkeypatch.setattr(tasks, "poll_connection", fake_poll)
    result = await tasks.poll_all_active(session)
    assert result["connections"] == 1

    renewed: list[str] = []

    async def fake_sync(session, row, *, notify_base=""):
        renewed.append(row.org_id)
        return False

    monkeypatch.setattr(tasks, "sync_subscription", fake_sync)
    await tasks.renew_subscriptions(session)
    assert renewed == [other.id]


async def test_a_queued_tenant_refuses_webhooks_as_if_gone(
    auth_client, client, session, monkeypatch
):
    owner_client, owner = auth_client
    connection = await _bulk(session, owner["org_id"])

    # Before queuing, an unsigned callback reaches authentication and fails there.
    before = await client.post(f"/webhooks/filedrop?connection_id={connection.id}", json={})
    assert before.status_code == 401

    await _queue_deletion(owner_client, monkeypatch)
    after = await client.post(f"/webhooks/filedrop?connection_id={connection.id}", json={})
    assert after.status_code == 404


async def test_a_queued_tenant_runs_no_scheduled_scripts(auth_client, session, monkeypatch):
    from osprey.models import ScriptTask
    from osprey.scripts import service as scripts

    owner_client, owner = auth_client
    connection = await _bulk(session, owner["org_id"])
    session.add(
        ScriptTask(
            org_id=owner["org_id"],
            project_id=connection.project_id,
            name="nightly",
            schedule_minutes=5,
        )
    )
    await session.commit()
    monkeypatch.setattr(settings, "feature_scripts", True)
    ran: list[str] = []

    async def fake_run(session, task):
        ran.append(task.name)
        return {}

    monkeypatch.setattr(scripts, "run_task", fake_run)
    assert (await scripts.run_due_scripts(session))["ran"] == 1

    await _queue_deletion(owner_client, monkeypatch)
    ran.clear()
    session.expire_all()
    assert (await scripts.run_due_scripts(session))["ran"] == 0
    assert ran == []
