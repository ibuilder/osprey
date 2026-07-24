"""AI connections (bring-your-own key) and sift-to-hotlist."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..engine.sift import seal_api_key, sift_to_hotlist
from ..models import AIConnection, ConnectionStatus, Project, Role
from ..schemas import (
    AIConnectionCreate,
    AIConnectionOut,
    SiftFindingOut,
    SiftRequest,
    SiftResponse,
)
from ..security import audit
from ..security.auth import Principal
from .deps import current_principal, db_session, project_in_org, require_role

router = APIRouter(prefix="/ai", tags=["ai"])


def _out(row: AIConnection) -> AIConnectionOut:
    return AIConnectionOut(
        id=row.id,
        provider=row.provider.value,
        label=row.label,
        model=row.model,
        status=row.status.value,
        project_id=row.project_id,
        has_key=bool(row.encrypted_key),
    )


@router.post("/connections", response_model=AIConnectionOut, status_code=201)
async def create_ai_connection(
    body: AIConnectionCreate,
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(require_role(Role.admin)),
) -> AIConnectionOut:
    if body.project_id:
        project = await session.get(Project, body.project_id)
        if project is None or project.org_id != principal.org_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    row = AIConnection(
        org_id=principal.org_id,
        project_id=body.project_id,
        provider=body.provider,
        label=body.label or body.provider.value,
        model=body.model,
        base_url=body.base_url,
        encrypted_key=seal_api_key(body.api_key),
        status=ConnectionStatus.active,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        org_id=principal.org_id,
        actor=principal.email,
        action="ai_connection.created",
        target=row.id,
        meta={"provider": body.provider.value},
    )
    return _out(row)


@router.get("/connections", response_model=list[AIConnectionOut])
async def list_ai_connections(
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[AIConnectionOut]:
    rows = (
        (await session.execute(select(AIConnection).where(AIConnection.org_id == principal.org_id)))
        .scalars()
        .all()
    )
    return [_out(r) for r in rows]


@router.post("/projects/{project_id}/sift", response_model=SiftResponse)
async def sift(
    body: SiftRequest,
    project: Project = Depends(project_in_org),
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(require_role(Role.pm)),
) -> SiftResponse:
    if not settings.feature_ai_sift:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "AI sift is disabled")
    if body.ai_connection_id:
        ai = await session.get(AIConnection, body.ai_connection_id)
        if ai is None or ai.org_id != principal.org_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "ai_connection not found")

    findings, scanned = await sift_to_hotlist(
        session,
        org_id=principal.org_id,
        project_id=project.id,
        instruction=body.instruction,
        ai_connection_id=body.ai_connection_id,
        lookback_days=body.lookback_days,
        max_signals=body.max_signals,
    )
    await audit.record(
        session,
        org_id=principal.org_id,
        actor=principal.email,
        action="ai.sift",
        target=project.id,
        meta={"findings": len(findings)},
    )
    return SiftResponse(findings=[SiftFindingOut(**f) for f in findings], scanned_signals=scanned)
