"""Projects: create, list, get, tune scoring weights."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Project, Role
from ..schemas import ProjectCreate, ProjectOut, WeightsUpdate
from ..security import audit
from ..security.auth import Principal
from .deps import current_principal, db_session, project_in_org, require_role

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(
    body: ProjectCreate,
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(require_role(Role.admin)),
) -> ProjectOut:
    project = Project(org_id=principal.org_id, name=body.name)
    session.add(project)
    await session.flush()
    await audit.record(
        session, org_id=principal.org_id, actor=principal.email, action="project.created", target=project.id
    )
    return ProjectOut(id=project.id, name=project.name, org_id=project.org_id, weights=project.weights)


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[ProjectOut]:
    rows = (
        await session.execute(select(Project).where(Project.org_id == principal.org_id))
    ).scalars().all()
    return [ProjectOut(id=p.id, name=p.name, org_id=p.org_id, weights=p.weights) for p in rows]


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project: Project = Depends(project_in_org)) -> ProjectOut:
    return ProjectOut(id=project.id, name=project.name, org_id=project.org_id, weights=project.weights)


@router.put("/{project_id}/weights", response_model=ProjectOut)
async def set_weights(
    body: WeightsUpdate,
    project: Project = Depends(project_in_org),
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(require_role(Role.pm)),
) -> ProjectOut:
    weights = dict(project.weights)
    for key in ("urgency", "impact", "confidence"):
        val = getattr(body, key)
        if val is not None:
            weights[key] = float(val)
    project.weights = weights
    session.add(project)
    await audit.record(
        session, org_id=principal.org_id, actor=principal.email,
        action="project.weights_updated", target=project.id, meta=weights,
    )
    await session.flush()
    return ProjectOut(id=project.id, name=project.name, org_id=project.org_id, weights=project.weights)
