"""Script tasks: register, list, run-now, enable/disable."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Project, Role, ScriptStatus, ScriptTask
from ..schemas import ScriptCreate, ScriptOut, ScriptRunResult
from ..scripts.service import run_task
from ..security import audit
from ..security.auth import Principal
from .deps import current_principal, db_session, project_in_org, require_role

router = APIRouter(tags=["scripts"])


def _out(row: ScriptTask) -> ScriptOut:
    return ScriptOut(
        id=row.id,
        name=row.name,
        enabled=row.enabled,
        schedule_minutes=row.schedule_minutes,
        status=row.status.value,
        last_run=row.last_run.isoformat() if row.last_run else None,
        last_result=row.last_result,
    )


@router.post("/projects/{project_id}/scripts", response_model=ScriptOut, status_code=201)
async def create_script(
    body: ScriptCreate,
    project: Project = Depends(project_in_org),
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(require_role(Role.admin)),
) -> ScriptOut:
    if not settings.feature_scripts:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "script tasks are disabled")
    row = ScriptTask(
        org_id=principal.org_id,
        project_id=project.id,
        name=body.name,
        source_code=body.source_code,
        enabled=body.enabled,
        schedule_minutes=body.schedule_minutes,
        timeout_seconds=min(body.timeout_seconds, settings.scripts_max_timeout_seconds),
        status=ScriptStatus.idle,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        org_id=principal.org_id,
        actor=principal.email,
        action="script.created",
        target=row.id,
        meta={"name": row.name},
    )
    return _out(row)


@router.get("/projects/{project_id}/scripts", response_model=list[ScriptOut])
async def list_scripts(
    project: Project = Depends(project_in_org),
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[ScriptOut]:
    rows = (
        (await session.execute(select(ScriptTask).where(ScriptTask.project_id == project.id)))
        .scalars()
        .all()
    )
    return [_out(r) for r in rows]


async def _load(session: AsyncSession, script_id: str, org_id: str) -> ScriptTask:
    row = await session.get(ScriptTask, script_id)
    if row is None or row.org_id != org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "script not found")
    return row


@router.post("/scripts/{script_id}/run", response_model=ScriptRunResult)
async def run_script_now(
    script_id: str,
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(require_role(Role.admin)),
) -> ScriptRunResult:
    if not settings.feature_scripts:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "script tasks are disabled")
    row = await _load(session, script_id, principal.org_id)
    result = await run_task(session, row)
    await audit.record(
        session,
        org_id=principal.org_id,
        actor=principal.email,
        action="script.run",
        target=row.id,
        meta={"status": result["status"]},
    )
    return ScriptRunResult(**result)


@router.post("/scripts/{script_id}/toggle", response_model=ScriptOut)
async def toggle_script(
    script_id: str,
    enabled: bool,
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(require_role(Role.admin)),
) -> ScriptOut:
    row = await _load(session, script_id, principal.org_id)
    row.enabled = enabled
    row.status = ScriptStatus.disabled if not enabled else ScriptStatus.idle
    session.add(row)
    await session.flush()
    return _out(row)
