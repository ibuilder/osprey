"""AI sift-to-hotlist (SPEC extension).

A user connects their own AI account and asks Osprey, in natural language, to sift
recent project data ("flag anything about liquidated damages", "find unanswered RFIs
older than a week"). Matching signals are synthesized into findings that flow through
the normal extraction + scoring pipeline and land on the hotlist — with citations
back to the source signals, so the result is explainable, not a black box.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.base import SiftInput
from ..ai.provider import get_provider, provider_from_connection
from ..connectors.base import RawEvent
from ..models import AIConnection, ConnectionStatus, Score, Signal, SourceKind, utcnow
from ..security import crypto
from .emit import emit_events
from .hotlist import build_hotlist, run_pipeline

log = logging.getLogger("osprey.sift")


async def _build_provider(session: AsyncSession, ai_connection_id: str | None):
    if not ai_connection_id:
        return get_provider()
    ai = await session.get(AIConnection, ai_connection_id)
    if ai is None:
        return get_provider()
    api_key = crypto.open_sealed(ai.encrypted_key)["k"] if ai.encrypted_key else ""
    ai.last_used = utcnow()
    session.add(ai)
    return provider_from_connection(
        ai.provider.value, api_key=api_key, model=ai.model, base_url=ai.base_url
    )


async def sift_to_hotlist(
    session: AsyncSession,
    *,
    org_id: str,
    project_id: str,
    instruction: str,
    ai_connection_id: str | None = None,
    lookback_days: int = 30,
    max_signals: int = 200,
    generated_by: str = "ai-sift",
) -> tuple[list[dict], int]:
    """Run a sift and push findings to the hotlist. Returns (findings, scanned_count)."""
    since = datetime.now(UTC) - timedelta(days=lookback_days)
    signals = list(
        (
            await session.execute(
                select(Signal)
                .where(Signal.project_id == project_id, Signal.occurred_at >= since)
                .order_by(Signal.occurred_at.desc())
                .limit(max_signals)
            )
        ).scalars().all()
    )
    provider = await _build_provider(session, ai_connection_id)
    payload = SiftInput(
        instruction=instruction,
        signals=[{"id": s.id, "title": s.title, "body": s.body} for s in signals],
    )
    findings = await provider.sift(payload)
    if not findings:
        return [], len(signals)

    events: list[RawEvent] = []
    for idx, f in enumerate(findings):
        digest = hashlib.sha256(f"{instruction}|{idx}|{sorted(f.matched_signal_ids)}".encode()).hexdigest()[:16]
        events.append(
            RawEvent(
                external_id=f"ai:{digest}",
                source_kind=SourceKind.general,
                title=f.title,
                body=f.body,
                raw={
                    "instruction": instruction,
                    "matched_signal_ids": f.matched_signal_ids,
                    "generated_by": generated_by,
                    "sift_confidence": f.confidence,
                },
            )
        )
    await emit_events(
        session, org_id=org_id, project_id=project_id, source_type="ai", events=events, account_ref="ai-sift"
    )
    await run_pipeline(session, project_id)
    await build_hotlist(session, project_id, generated_by=generated_by)

    # Map the fresh AI items back for the response.
    ai_signals = (
        await session.execute(
            select(Signal).where(
                Signal.project_id == project_id,
                Signal.external_id.in_([e.external_id for e in events]),
            )
        )
    ).scalars().all()
    by_item = {s.item_id: s for s in ai_signals if s.item_id}
    results: list[dict] = []
    for item_id, sig in by_item.items():
        score = (
            await session.execute(
                select(Score).where(Score.item_id == item_id).order_by(Score.version.desc()).limit(1)
            )
        ).scalar_one_or_none()
        results.append(
            {
                "item_id": item_id,
                "title": sig.title,
                "category": score.factors.get("category", "general") if score else "general",
                "score": score.total if score else 0.0,
                "bucket": score.bucket.value if score else "watch",
                "matched_signal_ids": sig.raw.get("matched_signal_ids", []),
            }
        )
    log.info("sift produced %d findings for project %s", len(results), project_id)
    return results, len(signals)


def seal_api_key(key: str) -> str:
    return crypto.seal({"k": key}) if key else ""


def connection_status_ok() -> ConnectionStatus:
    return ConnectionStatus.active
