"""Items: list, detail (with signals + latest score), and feedback actions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..engine.learn import record_action
from ..models import Item, Project, Role, Score, Signal
from ..schemas import ActionRequest, ItemOut
from ..security import audit
from ..security.auth import Principal
from .deps import current_principal, db_session, project_in_org, require_role

router = APIRouter(tags=["items"])


async def _latest_score(session: AsyncSession, item_id: str) -> Score | None:
    return (
        await session.execute(
            select(Score).where(Score.item_id == item_id).order_by(Score.version.desc()).limit(1)
        )
    ).scalar_one_or_none()


@router.get("/projects/{project_id}/items", response_model=list[ItemOut])
async def list_items(
    project: Project = Depends(project_in_org),
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[ItemOut]:
    items = (
        await session.execute(select(Item).where(Item.project_id == project.id))
    ).scalars().all()
    out: list[ItemOut] = []
    for item in items:
        score = await _latest_score(session, item.id)
        out.append(
            ItemOut(
                id=item.id, title=item.title, category=item.category.value, summary=item.summary,
                status=item.status.value, owner=item.owner,
                score=score.total if score else None,
                bucket=score.bucket.value if score else None,
            )
        )
    out.sort(key=lambda i: (i.score or -1), reverse=True)
    return out


async def _item_in_org(session: AsyncSession, item_id: str, org_id: str) -> Item:
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "item not found")
    project = await session.get(Project, item.project_id)
    if project is None or project.org_id != org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "item not found")
    return item


@router.get("/items/{item_id}")
async def get_item(
    item_id: str,
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> dict:
    item = await _item_in_org(session, item_id, principal.org_id)
    signals = (
        await session.execute(select(Signal).where(Signal.item_id == item.id))
    ).scalars().all()
    score = await _latest_score(session, item.id)
    return {
        "id": item.id,
        "title": item.title,
        "category": item.category.value,
        "summary": item.summary,
        "status": item.status.value,
        "owner": item.owner,
        "score": score.total if score else None,
        "bucket": score.bucket.value if score else None,
        "explanation": score.explanation if score else "",
        "factors": score.factors if score else {},
        "signals": [
            {
                "id": s.id, "source_type": s.source_type, "source_kind": s.source_kind.value,
                "title": s.title, "url": s.url,
                "occurred_at": s.occurred_at.isoformat() if s.occurred_at else None,
            }
            for s in signals
        ],
    }


@router.post("/items/{item_id}/actions", status_code=201)
async def act_on_item(
    item_id: str,
    body: ActionRequest,
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(require_role(Role.pm)),
) -> dict:
    item = await _item_in_org(session, item_id, principal.org_id)
    action = await record_action(
        session, item=item, action_type=body.type, user_id=principal.user_id, meta=body.meta
    )
    await audit.record(
        session, org_id=principal.org_id, actor=principal.email,
        action=f"item.{body.type.value}", target=item.id,
    )
    return {"action_id": action.id, "item_status": item.status.value}
