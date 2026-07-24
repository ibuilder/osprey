"""Structured extraction over a clustered Item (SPEC §7.2)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.base import Extraction, ExtractionInput, LLMProvider
from ..ai.provider import get_provider
from ..models import Item, Signal, utcnow


async def _item_signals(session: AsyncSession, item_id: str) -> list[Signal]:
    return list(
        (
            await session.execute(
                select(Signal).where(Signal.item_id == item_id).order_by(Signal.occurred_at.asc())
            )
        )
        .scalars()
        .all()
    )


def _to_input(item: Item, signals: list[Signal]) -> ExtractionInput:
    return ExtractionInput(
        item_title=item.title or (signals[0].title if signals else ""),
        signals=[
            {
                "id": s.id,
                "title": s.title,
                "body": s.body,
                "due_at": s.due_at.isoformat() if s.due_at else None,
                "occurred_at": s.occurred_at.isoformat() if s.occurred_at else None,
                "amount": s.amount,
                "url": s.url,
                "participants": s.participants,
            }
            for s in signals
        ],
    )


async def extract_item(
    session: AsyncSession, item: Item, *, provider: LLMProvider | None = None
) -> Extraction:
    provider = provider or get_provider()
    signals = await _item_signals(session, item.id)
    extraction = await provider.extract(_to_input(item, signals))

    # Fold the extraction back onto the Item for display/filtering.
    item.category = extraction.category
    item.summary = extraction.summary or item.summary
    if signals:
        item.title = item.title or signals[0].title
    item.updated_at = utcnow()
    session.add(item)
    await session.flush()
    return extraction
