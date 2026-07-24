"""Ingestion: connector RawEvents -> normalized, embedded, deduped Signals.

Idempotent by contract: re-ingesting the same event (same connection + external_id)
creates no duplicate. Every new Signal gets an embedding for later clustering.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.embeddings import get_embedder
from ..connectors.base import Connector, NormalizedSignal, RawEvent
from ..models import Connection, Signal

log = logging.getLogger("osprey.ingest")


async def _existing_external_ids(
    session: AsyncSession, connection_id: str, ids: list[str]
) -> set[str]:
    if not ids:
        return set()
    rows = (
        (
            await session.execute(
                select(Signal.external_id).where(
                    Signal.connection_id == connection_id, Signal.external_id.in_(ids)
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def ingest_events(
    session: AsyncSession,
    connector: Connector,
    connection: Connection,
    events: Iterable[RawEvent],
) -> list[Signal]:
    """Normalize + persist new Signals; skip already-seen external_ids."""
    events = list(events)
    if not events:
        return []

    seen = await _existing_external_ids(session, connection.id, [e.external_id for e in events])
    embedder = get_embedder()
    created: list[Signal] = []
    batch_ids: set[str] = set()

    for ev in events:
        if ev.external_id in seen or ev.external_id in batch_ids:
            continue  # idempotent dedupe (DB unique constraint is the backstop)
        batch_ids.add(ev.external_id)
        norm: NormalizedSignal = await connector.normalize(ev)
        embedding = embedder.embed(f"{norm.title}\n{norm.body}")
        signal = Signal(
            project_id=connection.project_id,
            connection_id=connection.id,
            source_type=connection.source_type,
            source_kind=norm.source_kind,
            external_id=norm.external_id,
            thread_key=norm.thread_key,
            title=norm.title,
            body=norm.body,
            participants=norm.participants,
            due_at=norm.due_at,
            amount=norm.amount,
            url=norm.url,
            raw=norm.raw,
            embedding=embedding,
            occurred_at=norm.occurred_at,
        )
        session.add(signal)
        created.append(signal)

    if created:
        await session.flush()
        log.info("ingested %d new signals for connection %s", len(created), connection.id)
    return created
