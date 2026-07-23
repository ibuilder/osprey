"""Explainable scoring rubric (SPEC §7.3).

Transparent first, ML later. Every Item shows its factor breakdown, so a user can
always see *why* something ranked where it did. Contractual notice deadlines are
weighted highest — a missed notice can waive a claim worth more than the fee.

    total(0..100) = 100 * (w_u·urgency + w_i·impact + w_c·confidence)

All component functions are pure so they are trivially unit-tested.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..ai.base import Extraction
from ..models import Bucket, Category

# Relative impact weight per category (0..1).
CATEGORY_WEIGHT: dict[Category, float] = {
    Category.contractual_notice: 1.00,
    Category.safety: 0.90,
    Category.change_order: 0.70,
    Category.invoice: 0.65,
    Category.schedule: 0.60,
    Category.rfi: 0.50,
    Category.submittal: 0.45,
    Category.general: 0.30,
}

DEFAULT_WEIGHTS = {"urgency": 0.40, "impact": 0.50, "confidence": 0.10}


@dataclass
class ScoreResult:
    urgency: float
    impact: float
    confidence: float
    total: float          # 0..100
    bucket: Bucket
    explanation: str
    factors: dict = field(default_factory=dict)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _parse_deadline(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return _as_utc(value)
    try:
        return _as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def compute_urgency(deadline: datetime | None, last_activity: datetime | None, now: datetime) -> float:
    """Time-to-deadline dominates; aging provides a floor when there's no date."""
    deadline_component = 0.0
    if deadline is not None:
        days_left = (deadline - now).total_seconds() / 86400.0
        if days_left < 0:
            deadline_component = 1.0
        elif days_left <= 1:
            deadline_component = 0.95
        elif days_left <= 3:
            deadline_component = 0.85
        elif days_left <= 7:
            deadline_component = 0.65
        elif days_left <= 14:
            deadline_component = 0.45
        elif days_left <= 30:
            deadline_component = 0.25
        else:
            deadline_component = 0.10

    aging_component = 0.10
    if last_activity is not None:
        days_since = max(0.0, (now - last_activity).total_seconds() / 86400.0)
        aging_component = min(1.0, 0.10 + (days_since / 14.0) * 0.5)

    return round(max(deadline_component, aging_component), 4)


def compute_impact(
    category: Category, exposure: float | None, notice_deadline: bool, blocking_count: int
) -> tuple[float, float]:
    """Return (impact, exposure_factor)."""
    cat_weight = CATEGORY_WEIGHT.get(category, 0.3)
    exposure_factor = 0.0
    if exposure and exposure > 0:
        exposure_factor = min(1.0, math.log10(1.0 + exposure) / 6.0)  # $1M -> ~1.0
    blocking_bonus = 0.10 if blocking_count > 0 else 0.0
    impact = 0.6 * cat_weight + 0.4 * exposure_factor + blocking_bonus
    impact = min(1.0, impact)
    if notice_deadline:
        impact = max(impact, 0.90)  # notice deadlines weighted highest
    return round(impact, 4), round(exposure_factor, 4)


def _bucket(total: float, urgency: float, notice: bool) -> Bucket:
    if notice and urgency >= 0.6:
        return Bucket.act_today
    if total >= 70:
        return Bucket.act_today
    if total >= 45:
        return Bucket.this_week
    return Bucket.watch


def score_item(
    extraction: Extraction,
    *,
    last_activity: datetime | None,
    now: datetime | None = None,
    weights: dict[str, float] | None = None,
    blocking_count: int | None = None,
) -> ScoreResult:
    now = _as_utc(now) or datetime.now(UTC)
    last_activity = _as_utc(last_activity)
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    wsum = w["urgency"] + w["impact"] + w["confidence"]
    if wsum <= 0:
        w, wsum = DEFAULT_WEIGHTS, 1.0

    deadline = _parse_deadline(extraction.deadline)
    urgency = compute_urgency(deadline, last_activity, now)
    n_block = blocking_count if blocking_count is not None else len(extraction.blocking)
    impact, exposure_factor = compute_impact(
        extraction.category, extraction.dollar_exposure, extraction.notice_deadline, n_block
    )
    confidence = round(max(0.0, min(1.0, extraction.confidence)), 4)

    blended = (w["urgency"] * urgency + w["impact"] * impact + w["confidence"] * confidence) / wsum
    total = round(100.0 * blended, 2)
    bucket = _bucket(total, urgency, extraction.notice_deadline)

    factors = {
        "urgency": urgency,
        "impact": impact,
        "confidence": confidence,
        "exposure_factor": exposure_factor,
        "category": extraction.category.value,
        "category_weight": CATEGORY_WEIGHT.get(extraction.category, 0.3),
        "dollar_exposure": extraction.dollar_exposure,
        "deadline": deadline.isoformat() if deadline else None,
        "notice_deadline": extraction.notice_deadline,
        "blocking_count": n_block,
        "weights": {k: round(v / wsum, 4) for k, v in w.items()},
        "low_confidence_flag": confidence < 0.5,
    }
    explanation = _explain(bucket, urgency, impact, extraction, deadline)
    return ScoreResult(urgency, impact, confidence, total, bucket, explanation, factors)


def _explain(bucket, urgency, impact, extraction: Extraction, deadline) -> str:
    parts: list[str] = []
    if extraction.notice_deadline:
        parts.append("contractual notice deadline (highest weight)")
    if deadline:
        parts.append(f"deadline {deadline.date().isoformat()}")
    if extraction.dollar_exposure:
        parts.append(f"${extraction.dollar_exposure:,.0f} exposure")
    if extraction.blocking:
        parts.append(f"blocks {len(extraction.blocking)} item(s)")
    parts.append(f"category={extraction.category.value}")
    lead = {
        Bucket.act_today: "Act today",
        Bucket.this_week: "This week",
        Bucket.watch: "Watch",
        Bucket.done: "Done",
    }[bucket]
    return f"{lead}: " + "; ".join(parts) + f" (urgency {urgency:.2f}, impact {impact:.2f})."
