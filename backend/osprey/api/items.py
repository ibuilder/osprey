"""Items: list, detail (with signals + latest score), and feedback actions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..engine.learn import record_action
from ..models import Item, ItemStatus, Project, Role, Score, Signal
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


async def _latest_scores(session: AsyncSession, item_ids: list[str]) -> dict[str, Score]:
    """Newest score per item, in one query instead of one query per item.

    A correlated ``MAX(version)`` subquery would be the tidier SQL, but it does
    not translate identically across SQLite and Postgres for the tie case (two
    rows sharing a version, which the scorer can produce on a same-second
    rescore). Fetching the candidate rows and folding them in Python keeps the
    two backends byte-identical, which the test suite depends on, and the row
    count is bounded by the page size.
    """
    if not item_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(Score)
                .where(Score.item_id.in_(item_ids))
                .order_by(Score.item_id, Score.version.asc())
            )
        )
        .scalars()
        .all()
    )
    # Ascending order means the last write per item wins, i.e. the highest version.
    return {row.item_id: row for row in rows}


@router.get("/projects/{project_id}/items", response_model=list[ItemOut])
async def list_items(
    project: Project = Depends(project_in_org),
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status_filter: ItemStatus | None = Query(default=None, alias="status"),
) -> list[ItemOut]:
    """List a project's items, highest-scoring first.

    Paged and capped: an unbounded list plus a per-item score lookup was fine at
    demo scale and a timeout on a real project with tens of thousands of items.
    Ordering happens in SQL against the score table so that paging is stable --
    sorting a page in Python would give a different page-2 depending on what
    landed in page 1.
    """
    ranked = (
        select(Item.id, Score.total)
        .join(Score, Score.item_id == Item.id, isouter=True)
        .where(Item.project_id == project.id)
    )
    if status_filter is not None:
        ranked = ranked.where(Item.status == status_filter)
    # NULL scores sort last; unscored items are not more urgent than scored ones.
    ranked = (
        ranked.order_by(func.coalesce(Score.total, -1).desc(), Item.id.asc())
        .limit(limit)
        .offset(offset)
    )

    rows = (await session.execute(ranked)).all()
    ordered_ids = []
    seen: set[str] = set()
    for item_id, _total in rows:
        # An item with several score versions appears once per version in the
        # join; keep the first (highest total) occurrence only.
        if item_id not in seen:
            seen.add(item_id)
            ordered_ids.append(item_id)
    if not ordered_ids:
        return []

    items = {
        item.id: item
        for item in (await session.execute(select(Item).where(Item.id.in_(ordered_ids))))
        .scalars()
        .all()
    }
    scores = await _latest_scores(session, ordered_ids)
    out: list[ItemOut] = []
    for item_id in ordered_ids:
        item = items.get(item_id)
        if item is None:  # pragma: no cover - deleted between the two queries
            continue
        score = scores.get(item_id)
        out.append(
            ItemOut(
                id=item.id,
                title=item.title,
                category=item.category.value,
                summary=item.summary,
                status=item.status.value,
                owner=item.owner,
                score=score.total if score else None,
                bucket=score.bucket.value if score else None,
            )
        )
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
        (await session.execute(select(Signal).where(Signal.item_id == item.id))).scalars().all()
    )
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
                "id": s.id,
                "source_type": s.source_type,
                "source_kind": s.source_kind.value,
                "title": s.title,
                "url": s.url,
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
        session,
        org_id=principal.org_id,
        actor=principal.email,
        action=f"item.{body.type.value}",
        target=item.id,
    )
    return {"action_id": action.id, "item_status": item.status.value}
