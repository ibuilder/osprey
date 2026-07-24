"""Emit internally-produced signals (AI sift, user scripts) into the pipeline.

Anything Osprey generates about a project — an AI finding, a script's output —
enters through the same ingest → cluster → score → hotlist path as an external
connector, so it is deduped and becomes a first-class, explainable hotlist item.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..connectors.base import RawEvent
from ..connectors.service import get_connector
from ..models import Connection, ConnectionStatus, Signal
from .ingest import ingest_events


async def get_or_create_internal_connection(
    session: AsyncSession, *, org_id: str, project_id: str, source_type: str, account_ref: str = ""
) -> Connection:
    row = (
        (
            await session.execute(
                select(Connection).where(
                    Connection.project_id == project_id, Connection.source_type == source_type
                )
            )
        )
        .scalars()
        .first()
    )
    if row is not None:
        return row
    row = Connection(
        org_id=org_id,
        project_id=project_id,
        source_type=source_type,
        account_ref=account_ref or source_type,
        status=ConnectionStatus.active,
    )
    session.add(row)
    await session.flush()
    return row


async def emit_events(
    session: AsyncSession,
    *,
    org_id: str,
    project_id: str,
    source_type: str,
    events: list[RawEvent],
    account_ref: str = "",
) -> list[Signal]:
    """Persist internally-produced RawEvents as Signals (idempotent dedupe)."""
    if not events:
        return []
    connector = get_connector(source_type)
    connection = await get_or_create_internal_connection(
        session,
        org_id=org_id,
        project_id=project_id,
        source_type=source_type,
        account_ref=account_ref,
    )
    return await ingest_events(session, connector, connection, events)
