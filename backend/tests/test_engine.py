"""End-to-end engine: ingest -> cluster -> extract -> score -> hotlist."""

from __future__ import annotations

from sqlalchemy import func, select

from osprey.connectors.filedrop import FileDropConnector, parse_email
from osprey.engine.hotlist import refresh_project
from osprey.engine.ingest import ingest_events
from osprey.models import Bucket, Connection, ConnectionStatus, Org, Project, Signal

NOTICE_EMAIL = """\
From: pm@gc.com
To: owner@dev.com
Subject: NOTICE OF DELAY — differing site conditions at Tower B
Date: Wed, 22 Jul 2026 08:00:00 -0500
Message-ID: <notice-1@gc.com>

Pursuant to Section 8.3, this is formal notice of delay due to differing site
conditions. A written response is required within 7 days or the claim may be
deemed waived. Estimated exposure $180,000.
"""

INVOICE_EMAIL = """\
From: ap@sub.com
To: pm@gc.com
Subject: Pay Application 07 — retention release
Date: Wed, 22 Jul 2026 10:00:00 -0500
Message-ID: <inv-7@sub.com>

Attached is pay application 07 for $54,000 including retention release.
"""


async def _setup(session):
    org = Org(name="Tower B GC")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name="Tower B")
    session.add(project)
    await session.flush()
    conn_row = Connection(
        org_id=org.id, project_id=project.id, source_type="filedrop",
        account_ref="drop@in.osprey", status=ConnectionStatus.active,
    )
    session.add(conn_row)
    await session.flush()
    return org, project, conn_row


async def _ingest_email(session, conn_row, raw):
    connector = FileDropConnector()
    events = [ev async for ev in connector.handle_webhook({"kind": "email", "raw": raw})]
    return await ingest_events(session, connector, conn_row, events)


async def test_full_pipeline_ranks_notice_first(session):
    _, project, conn_row = await _setup(session)
    await _ingest_email(session, conn_row, NOTICE_EMAIL)
    await _ingest_email(session, conn_row, INVOICE_EMAIL)
    await session.flush()

    snapshot = await refresh_project(session, project.id)
    payload = snapshot.payload

    assert payload["item_count"] == 2
    top = payload["items"][0]
    assert "NOTICE OF DELAY" in top["what"].upper()
    assert top["notice_deadline"] is True
    assert top["bucket"] == Bucket.act_today.value
    assert top["dollar_exposure"] == 180000.0
    # Every item carries an explanation + factor breakdown + citations (explainable).
    assert top["why"]
    assert top["factors"]["citations"]
    assert payload["total_exposure"] >= 180000.0


async def test_ingest_is_idempotent(session):
    _, _, conn_row = await _setup(session)
    await _ingest_email(session, conn_row, NOTICE_EMAIL)
    await session.flush()
    # Re-ingest the same email -> no duplicate signal.
    created2 = await _ingest_email(session, conn_row, NOTICE_EMAIL)
    await session.flush()
    assert created2 == []
    count = (await session.execute(select(func.count()).select_from(Signal))).scalar_one()
    assert count == 1


async def test_clustering_merges_same_thread_across_ingests(session):
    _, project, conn_row = await _setup(session)
    # Two emails on the same thread (References points back to the first).
    first = parse_email(NOTICE_EMAIL)
    reply = (
        "From: owner@dev.com\nTo: pm@gc.com\n"
        "Subject: RE: NOTICE OF DELAY — differing site conditions at Tower B\n"
        "Date: Wed, 22 Jul 2026 12:00:00 -0500\n"
        "Message-ID: <notice-1-reply@dev.com>\n"
        f"References: {first.thread_key}\n\n"
        "Acknowledged; our engineer will respond within the notice period."
    )
    await _ingest_email(session, conn_row, NOTICE_EMAIL)
    await _ingest_email(session, conn_row, reply)
    await session.flush()

    snapshot = await refresh_project(session, project.id)
    # Both signals collapse into ONE item (same thread_key).
    assert snapshot.payload["item_count"] == 1
    assert len(snapshot.payload["items"][0]["sources"]) == 2
