"""Explainable scoring rubric — the transparency + correctness guarantees."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from osprey.ai.base import Extraction
from osprey.engine.score import compute_impact, compute_urgency, score_item
from osprey.models import Bucket, Category

NOW = datetime(2026, 7, 23, tzinfo=UTC)


def test_urgency_overdue_is_max():
    assert compute_urgency(NOW - timedelta(days=1), None, NOW) == 1.0


def test_urgency_decreases_with_distance():
    near = compute_urgency(NOW + timedelta(days=1), None, NOW)
    mid = compute_urgency(NOW + timedelta(days=10), None, NOW)
    far = compute_urgency(NOW + timedelta(days=60), None, NOW)
    assert near > mid > far


def test_notice_deadline_floors_impact_high():
    impact, _ = compute_impact(Category.contractual_notice, None, True, 0)
    assert impact >= 0.90  # contractual notice weighted highest


def test_exposure_scales_impact():
    low, _ = compute_impact(Category.invoice, 1_000, False, 0)
    high, _ = compute_impact(Category.invoice, 1_000_000, False, 0)
    assert high > low


def test_notice_item_outranks_bigger_dollar_general_item():
    notice = Extraction(
        category=Category.contractual_notice,
        notice_deadline=True,
        deadline=(NOW + timedelta(days=1)).isoformat(),
        confidence=0.8,
    )
    big_money = Extraction(category=Category.general, dollar_exposure=250_000, confidence=0.8)
    s_notice = score_item(notice, last_activity=NOW, now=NOW)
    s_money = score_item(big_money, last_activity=NOW, now=NOW)
    assert s_notice.total > s_money.total
    assert s_notice.bucket == Bucket.act_today


def test_weight_tuning_reorders_predictably():
    urgent = Extraction(
        category=Category.rfi, deadline=(NOW + timedelta(days=1)).isoformat(), confidence=0.6
    )
    impactful = Extraction(category=Category.change_order, dollar_exposure=500_000, confidence=0.6)

    urgency_heavy = {"urgency": 0.8, "impact": 0.1, "confidence": 0.1}
    impact_heavy = {"urgency": 0.1, "impact": 0.8, "confidence": 0.1}

    su = score_item(urgent, last_activity=NOW, now=NOW, weights=urgency_heavy)
    si = score_item(impactful, last_activity=NOW, now=NOW, weights=urgency_heavy)
    assert su.total > si.total  # urgency-weighted -> the deadline item wins

    su2 = score_item(urgent, last_activity=NOW, now=NOW, weights=impact_heavy)
    si2 = score_item(impactful, last_activity=NOW, now=NOW, weights=impact_heavy)
    assert si2.total > su2.total  # impact-weighted -> the money item wins


def test_score_exposes_factor_breakdown():
    ex = Extraction(
        category=Category.safety, dollar_exposure=10_000, confidence=0.4, blocking=["crane"]
    )
    result = score_item(ex, last_activity=NOW, now=NOW)
    f = result.factors
    assert {"urgency", "impact", "confidence", "weights", "category_weight"}.issubset(f)
    assert f["low_confidence_flag"] is True  # flagged, not buried
    assert result.explanation
