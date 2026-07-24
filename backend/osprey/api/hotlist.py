"""Hotlist: build/refresh, fetch, and export (Excel / PDF) — all from one snapshot."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..engine.hotlist import refresh_project
from ..exports import hotlist_to_pdf, hotlist_to_xlsx
from ..models import HotlistSnapshot, Project, Role
from ..security import audit
from ..security.auth import Principal
from .deps import current_principal, db_session, project_in_org, require_role

router = APIRouter(prefix="/projects", tags=["hotlist"])


async def _latest_snapshot(session: AsyncSession, project_id: str) -> HotlistSnapshot | None:
    return (
        await session.execute(
            select(HotlistSnapshot)
            .where(HotlistSnapshot.project_id == project_id)
            .order_by(HotlistSnapshot.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@router.post("/{project_id}/hotlist/refresh")
async def refresh(
    project: Project = Depends(project_in_org),
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(require_role(Role.pm)),
) -> dict:
    snapshot = await refresh_project(session, project.id, generated_by=principal.email)
    await audit.record(
        session,
        org_id=principal.org_id,
        actor=principal.email,
        action="hotlist.refreshed",
        target=project.id,
        meta={"items": snapshot.payload.get("item_count", 0)},
    )
    return snapshot.payload


@router.get("/{project_id}/hotlist")
async def get_hotlist(
    project: Project = Depends(project_in_org),
    refresh_now: bool = Query(default=False, alias="refresh"),
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> dict:
    if refresh_now:
        snapshot = await refresh_project(session, project.id, generated_by=principal.email)
        return snapshot.payload
    existing = await _latest_snapshot(session, project.id)
    snapshot = existing or await refresh_project(session, project.id, generated_by=principal.email)
    return snapshot.payload


@router.get("/{project_id}/hotlist/export")
async def export_hotlist(
    project: Project = Depends(project_in_org),
    format: str = Query(default="xlsx", pattern="^(xlsx|pdf)$"),
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> Response:
    existing = await _latest_snapshot(session, project.id)
    snapshot = existing or await refresh_project(session, project.id, generated_by=principal.email)
    payload = snapshot.payload

    await audit.record(
        session,
        org_id=principal.org_id,
        actor=principal.email,
        action="hotlist.exported",
        target=project.id,
        meta={"format": format},
    )

    if format == "pdf":
        data = hotlist_to_pdf(payload, project_name=project.name)
        media, ext = "application/pdf", "pdf"
    else:
        data = hotlist_to_xlsx(payload, project_name=project.name)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ext = "xlsx"
    filename = f"osprey-hotlist-{project.name.replace(' ', '_')}.{ext}"
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
