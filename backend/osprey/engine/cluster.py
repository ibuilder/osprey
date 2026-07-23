"""Clustering & dedupe (SPEC §7.1).

Group Signals into Items so one real-world thing = one Item, even across sources
(an RFI that appears in email *and* Procore is a single Item). Matching, in order:

  1. ``thread_key`` equality (email thread / RFI number),
  2. embedding cosine similarity >= ``OSPREY_CLUSTER_SIMILARITY_THRESHOLD``.

Portability note: similarity runs in-process over the project's open-item signals,
so it is identical on SQLite and Postgres. With pgvector in production this becomes
an indexed ``<=>`` nearest-neighbour query — a drop-in optimization behind this
function; the clustering *semantics* do not change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from ..ai.embeddings import cosine
from ..config import settings
from ..models import Item, ItemStatus, Signal, utcnow

log = logging.getLogger("osprey.cluster")


@dataclass
class _Candidate:
    item_id: str
    thread_key: str | None
    embedding: list[float] | None


async def _load_candidates(session: AsyncSession, project_id: str) -> list[_Candidate]:
    """Signals already attached to open items — the pool new signals cluster into."""
    rows = (
        await session.execute(
            select(Signal.item_id, Signal.thread_key, Signal.embedding)
            .join(Item, Item.id == Signal.item_id)
            .where(
                Signal.project_id == project_id,
                col(Signal.item_id).is_not(None),
                Item.status == ItemStatus.open,
            )
        )
    ).all()
    return [_Candidate(item_id=r[0], thread_key=r[1], embedding=r[2]) for r in rows]


def _best_match(
    signal: Signal, candidates: list[_Candidate], threshold: float
) -> str | None:
    # 1) exact thread key
    if signal.thread_key:
        for c in candidates:
            if c.thread_key and c.thread_key == signal.thread_key:
                return c.item_id
    # 2) embedding similarity
    best_id, best_sim = None, threshold
    for c in candidates:
        sim = cosine(signal.embedding, c.embedding)
        if sim >= best_sim:
            best_id, best_sim = c.item_id, sim
    return best_id


async def cluster_project(session: AsyncSession, project_id: str) -> list[str]:
    """Attach all unclustered signals to Items. Returns affected item ids."""
    threshold = settings.cluster_similarity_threshold
    unclustered = (
        await session.execute(
            select(Signal)
            .where(Signal.project_id == project_id, col(Signal.item_id).is_(None))
            .order_by(col(Signal.occurred_at).asc())
        )
    ).scalars().all()
    if not unclustered:
        return []

    candidates = await _load_candidates(session, project_id)
    affected: set[str] = set()

    for signal in unclustered:
        match_id = _best_match(signal, candidates, threshold)
        if match_id is None:
            item = Item(
                project_id=project_id,
                title=signal.title,
                summary=signal.title,
            )
            session.add(item)
            await session.flush()
            match_id = item.id
        else:
            existing = await session.get(Item, match_id)
            if existing is not None:
                existing.updated_at = utcnow()
                session.add(existing)

        signal.item_id = match_id
        session.add(signal)
        candidates.append(
            _Candidate(item_id=match_id, thread_key=signal.thread_key, embedding=signal.embedding)
        )
        affected.add(match_id)

    await session.flush()
    log.info("clustered %d signals into %d items", len(unclustered), len(affected))
    return list(affected)
