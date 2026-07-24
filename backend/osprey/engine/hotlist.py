"""Score persistence, ranking, and HotlistSnapshot construction (SPEC §7.4).

The snapshot payload is fully denormalized so Excel and PDF exports derive from the
*same* immutable object and can never disagree.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.base import Extraction
from ..config import settings
from ..models import (
    Bucket,
    HotlistSnapshot,
    Item,
    ItemStatus,
    Project,
    Score,
    Signal,
)
from .cluster import cluster_project
from .extract import extract_item
from .score import DEFAULT_WEIGHTS, score_item

log = logging.getLogger("osprey.hotlist")

_BUCKET_EMOJI = {
    Bucket.act_today: "🔴",
    Bucket.this_week: "🟠",
    Bucket.watch: "🟡",
    Bucket.done: "✅",
}
_BUCKET_LABEL = {
    Bucket.act_today: "Act today",
    Bucket.this_week: "This week",
    Bucket.watch: "Watch",
    Bucket.done: "Done",
}


def project_weights(project: Project | None) -> dict[str, float]:
    base = {
        "urgency": settings.weight_urgency,
        "impact": settings.weight_impact,
        "confidence": settings.weight_confidence,
    }
    if project and project.weights:
        base.update({k: float(v) for k, v in project.weights.items() if k in DEFAULT_WEIGHTS})
    return base


async def _latest_version(session: AsyncSession, item_id: str) -> int:
    row = (
        await session.execute(
            select(Score.version)
            .where(Score.item_id == item_id)
            .order_by(Score.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row or 0


async def score_and_persist(
    session: AsyncSession,
    item: Item,
    extraction: Extraction,
    *,
    weights: dict[str, float] | None = None,
    now: datetime | None = None,
) -> Score:
    signals = list(
        (await session.execute(select(Signal).where(Signal.item_id == item.id))).scalars().all()
    )
    last_activity = max((s.occurred_at for s in signals), default=item.updated_at)
    result = score_item(extraction, last_activity=last_activity, now=now, weights=weights)

    factors = dict(result.factors)
    factors["recommended_action"] = extraction.recommended_action
    factors["citations"] = [c.model_dump() for c in extraction.citations]

    score = Score(
        item_id=item.id,
        version=await _latest_version(session, item.id) + 1,
        urgency=result.urgency,
        impact=result.impact,
        confidence=result.confidence,
        total=result.total,
        bucket=result.bucket,
        factors=factors,
        explanation=result.explanation,
    )
    session.add(score)
    await session.flush()
    return score


async def run_pipeline(
    session: AsyncSession, project_id: str, *, now: datetime | None = None
) -> list[str]:
    """Cluster unclustered signals, then extract + score each affected item."""
    project = await session.get(Project, project_id)
    weights = project_weights(project)
    affected = await cluster_project(session, project_id)
    for item_id in affected:
        item = await session.get(Item, item_id)
        if item is None or item.status != ItemStatus.open:
            continue
        extraction = await extract_item(session, item)
        await score_and_persist(session, item, extraction, weights=weights, now=now)
    return affected


async def _latest_scores(session: AsyncSession, item_ids: list[str]) -> dict[str, Score]:
    if not item_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(Score).where(Score.item_id.in_(item_ids)).order_by(Score.version.asc())
            )
        )
        .scalars()
        .all()
    )
    latest: dict[str, Score] = {}
    for s in rows:  # ascending version -> last write wins = latest
        latest[s.item_id] = s
    return latest


async def _item_sources(session: AsyncSession, item_id: str) -> list[dict]:
    signals = (
        (await session.execute(select(Signal).where(Signal.item_id == item_id))).scalars().all()
    )
    return [
        {"source_type": s.source_type, "title": s.title, "url": s.url, "kind": s.source_kind.value}
        for s in signals
    ]


async def build_hotlist(
    session: AsyncSession,
    project_id: str,
    *,
    top_n: int | None = None,
    generated_by: str | None = None,
) -> HotlistSnapshot:
    top_n = top_n or settings.hotlist_top_n
    items = list(
        (
            await session.execute(
                select(Item).where(Item.project_id == project_id, Item.status == ItemStatus.open)
            )
        )
        .scalars()
        .all()
    )
    scores = await _latest_scores(session, [i.id for i in items])

    ranked: list[tuple[Item, Score]] = sorted(
        ((i, scores[i.id]) for i in items if i.id in scores),
        key=lambda pair: pair[1].total,
        reverse=True,
    )[:top_n]

    rows = []
    bucket_totals: dict[str, dict] = {b.value: {"count": 0, "exposure": 0.0} for b in Bucket}
    for item, score in ranked:
        sources = await _item_sources(session, item.id)
        exposure = score.factors.get("dollar_exposure")
        rows.append(
            {
                "item_id": item.id,
                "what": item.title,
                "category": item.category.value,
                "bucket": score.bucket.value,
                "bucket_label": _BUCKET_LABEL[score.bucket],
                "bucket_emoji": _BUCKET_EMOJI[score.bucket],
                "why": score.explanation,
                "summary": item.summary,
                "sources": sources,
                "owner": item.owner,
                "due": score.factors.get("deadline"),
                "dollar_exposure": exposure,
                "recommended_action": score.factors.get("recommended_action", ""),
                "notice_deadline": score.factors.get("notice_deadline", False),
                "score": score.total,
                "factors": score.factors,
            }
        )
        b = bucket_totals[score.bucket.value]
        b["count"] += 1
        b["exposure"] += float(exposure or 0.0)

    generated_at = (now_dt := (datetime.now(UTC))).isoformat()
    payload = {
        "project_id": project_id,
        "generated_at": generated_at,
        "top_n": top_n,
        "item_count": len(rows),
        "buckets": bucket_totals,
        "total_exposure": round(sum(float(r["dollar_exposure"] or 0) for r in rows), 2),
        "items": rows,
    }
    snapshot = HotlistSnapshot(
        project_id=project_id, top_n=top_n, generated_by=generated_by, payload=payload
    )
    snapshot.created_at = now_dt
    session.add(snapshot)
    await session.flush()
    log.info("built hotlist for project %s: %d items", project_id, len(rows))
    _publish(project_id, payload)
    return snapshot


def _publish(project_id: str, payload: dict) -> None:
    """Best-effort live broadcast to WebSocket subscribers (no hard dependency)."""
    try:
        from ..api.ws import hub

        hub.publish(project_id, payload)
    except Exception:  # noqa: BLE001
        pass


async def refresh_project(
    session: AsyncSession,
    project_id: str,
    *,
    top_n: int | None = None,
    generated_by: str | None = None,
    now: datetime | None = None,
) -> HotlistSnapshot:
    """Full cycle: pipeline (cluster/extract/score) then snapshot."""
    await run_pipeline(session, project_id, now=now)
    return await build_hotlist(session, project_id, top_n=top_n, generated_by=generated_by)
