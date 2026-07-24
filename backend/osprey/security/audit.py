"""Append-only, tamper-evident audit log (hash-chained).

Each record's ``hash = sha256(prev_hash + canonical_record)``. Any modification
or deletion of a prior record breaks every subsequent hash, so the chain is
verifiable end-to-end (see :func:`verify_chain`).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditLog


def _ts(created_at: datetime) -> str:
    # Normalize to naive-UTC ISO so the hash is stable across DB round-trips
    # (SQLite drops tzinfo on reload; Postgres preserves it).
    return created_at.replace(tzinfo=None).isoformat()


def _canonical(
    actor: str, action: str, target: str, meta: dict[str, Any], created_at: datetime
) -> str:
    return json.dumps(
        {
            "actor": actor,
            "action": action,
            "target": target,
            "meta": meta,
            "created_at": _ts(created_at),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(prev_hash: str, canonical: str) -> str:
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


async def record(
    session: AsyncSession,
    *,
    org_id: str,
    actor: str,
    action: str,
    target: str = "",
    meta: dict[str, Any] | None = None,
) -> AuditLog:
    meta = meta or {}
    prev = (
        await session.execute(
            select(AuditLog)
            .where(AuditLog.org_id == org_id)
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    prev_hash = prev.hash if prev else ""
    entry = AuditLog(
        org_id=org_id, actor=actor, action=action, target=target, meta=meta, prev_hash=prev_hash
    )
    canonical = _canonical(actor, action, target, meta, entry.created_at)
    entry.hash = _digest(prev_hash, canonical)
    session.add(entry)
    await session.flush()
    return entry


async def verify_chain(session: AsyncSession, org_id: str) -> bool:
    rows = (
        (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.org_id == org_id)
                .order_by(AuditLog.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    prev_hash = ""
    for row in rows:
        canonical = _canonical(row.actor, row.action, row.target, row.meta, row.created_at)
        if row.prev_hash != prev_hash or row.hash != _digest(prev_hash, canonical):
            return False
        prev_hash = row.hash
    return True
