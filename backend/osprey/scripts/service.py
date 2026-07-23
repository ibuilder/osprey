"""Run a ScriptTask: execute in the sandbox, emit signals, refresh the hotlist."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..engine.emit import emit_events
from ..engine.hotlist import build_hotlist, run_pipeline
from ..models import Project, ScriptStatus, ScriptTask, utcnow
from .runner import run_source

log = logging.getLogger("osprey.scripts")


async def run_task(session: AsyncSession, task: ScriptTask) -> dict:
    project = await session.get(Project, task.project_id)
    ctx = {
        "project_id": task.project_id,
        "project_name": project.name if project else "",
        "script_id": task.id,
        "now": utcnow().isoformat(),
    }
    task.status = ScriptStatus.running
    session.add(task)
    await session.flush()

    out = run_source(task.source_code, ctx=ctx, timeout_seconds=task.timeout_seconds)

    created = []
    if out.events:
        created = await emit_events(
            session, org_id=task.org_id, project_id=task.project_id,
            source_type="pyscript", events=out.events, account_ref=task.name,
        )
    if created:
        await run_pipeline(session, task.project_id)
        await build_hotlist(session, task.project_id, generated_by=f"script:{task.name}")

    result = {
        "status": out.status,
        "emitted": len(out.events),
        "created": len(created),
        "logs": out.logs[-20:],
        "error": out.error,
    }
    task.status = ScriptStatus.ok if out.status == "ok" else ScriptStatus.error
    task.last_run = utcnow()
    task.last_result = result
    session.add(task)
    await session.flush()
    log.info("script %s ran: %s (emitted=%d)", task.name, out.status, len(out.events))
    return result


async def run_due_scripts(session: AsyncSession) -> dict:
    """Worker entry: run enabled, scheduled scripts whose interval has elapsed."""
    if not settings.feature_scripts:
        return {"ran": 0}
    now = utcnow()
    tasks = (
        await session.execute(
            select(ScriptTask).where(ScriptTask.enabled, ScriptTask.schedule_minutes > 0)
        )
    ).scalars().all()
    ran = 0
    for task in tasks:
        due = (
            task.last_run is None
            or (now - _as_utc(task.last_run)).total_seconds() >= task.schedule_minutes * 60
        )
        if due:
            await run_task(session, task)
            ran += 1
    return {"ran": ran}


def _as_utc(dt):
    from datetime import UTC

    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
