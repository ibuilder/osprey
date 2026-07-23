"""Ingestion/scoring task logic — plain async functions (no ARQ import).

Kept dependency-free so the polling + refresh cycle is unit-testable without a
running Redis/ARQ worker.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..connectors.service import get_connector, to_view
from ..engine.hotlist import build_hotlist, run_pipeline
from ..engine.ingest import ingest_events
from ..models import Connection, ConnectionStatus, utcnow

log = logging.getLogger("osprey.worker")


async def poll_connection(session: AsyncSession, connection_id: str) -> dict:
    """Poll one connection, ingest new signals, persist the delta cursor."""
    row = await session.get(Connection, connection_id)
    if row is None:
        return {"error": "connection not found"}
    connector = get_connector(row.source_type)
    view = to_view(row)
    created = []
    try:
        events = [ev async for ev in connector.poll(view, row.last_sync)]
        created = await ingest_events(session, connector, row, events)
        row.status = ConnectionStatus.active
        row.last_error = None
        row.last_sync = utcnow()
        session.add(row)
    except Exception as exc:  # noqa: BLE001 - one source down != system down
        row.status = ConnectionStatus.degraded
        row.last_error = str(exc)[:500]
        session.add(row)
        log.warning("poll failed for connection %s: %s", connection_id, exc)
    return {"connection_id": connection_id, "created": len(created)}


async def refresh_project_task(session: AsyncSession, project_id: str) -> dict:
    """Cluster/extract/score, snapshot, and push critical items. Returns counts."""
    from ..engine.notify import notify_critical
    from ..models import Project

    await run_pipeline(session, project_id)
    snapshot = await build_hotlist(session, project_id, generated_by="worker")
    project = await session.get(Project, project_id)
    pushed = 0
    if project is not None:
        pushed = await notify_critical(session, org_id=project.org_id, payload=snapshot.payload)
    act_today = snapshot.payload.get("buckets", {}).get("act_today", {}).get("count", 0)
    return {
        "project_id": project_id,
        "act_today": act_today,
        "items": snapshot.payload.get("item_count", 0),
        "pushed": pushed,
    }


async def poll_all_active(session: AsyncSession) -> dict:
    rows = (
        await session.execute(
            select(Connection).where(Connection.status != ConnectionStatus.revoked)
        )
    ).scalars().all()
    total = 0
    for row in rows:
        res = await poll_connection(session, row.id)
        total += res.get("created", 0)
    return {"connections": len(rows), "created": total}


async def run_scheduled_scripts(session: AsyncSession) -> dict:
    """Run user scripts whose schedule interval has elapsed."""
    from ..scripts.service import run_due_scripts

    return await run_due_scripts(session)


async def renew_subscriptions(session: AsyncSession, *, notify_base: str = "") -> dict:
    """Create/renew provider webhook subscriptions before they lapse."""
    rows = (
        await session.execute(
            select(Connection).where(Connection.status == ConnectionStatus.active)
        )
    ).scalars().all()
    renewed = 0
    for row in rows:
        connector = get_connector(row.source_type)
        if not getattr(connector, "supports_subscriptions", False):
            continue
        notify_url = f"{notify_base}/webhooks/{row.source_type}?connection_id={row.id}"
        try:
            sub_id = await connector.ensure_subscription(to_view(row), notify_url)
            if sub_id:
                renewed += 1
        except Exception as exc:  # noqa: BLE001 - one source failing != system down
            log.warning("subscription renewal failed for %s: %s", row.id, exc)
    return {"checked": len(rows), "renewed": renewed}
