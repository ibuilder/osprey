"""Shared helpers for Excel and PDF exports (same snapshot → agreeing reports)."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

BUCKET_ORDER = ("act_today", "this_week", "watch", "done")

BUCKET_LABEL = {
    "act_today": "Act today",
    "this_week": "This week",
    "watch": "Watch",
    "done": "Done",
}

# Brand priority colors (BRAND.md) — reserved for hotlist buckets only.
BUCKET_HEX = {
    "act_today": "E5484D",
    "this_week": "F5A623",
    "watch": "EAB308",
    "done": "30A46C",
}

_FILENAME_SAFE = re.compile(r"[^\w.\-]+", re.UNICODE)


def esc_xml(text: Any) -> str:
    """Escape text for ReportLab Paragraph markup (HTML/XML subset)."""
    return (
        str(text if text is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def format_money(value: Any) -> str:
    """Format dollar exposure. ``None`` → em dash; ``0`` → ``$0`` (not blank)."""
    if value is None or value == "":
        return "—"
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "—"


def format_score(value: Any) -> str:
    """Format a hotlist score for display; corrupt values become ``0``."""
    try:
        return f"{float(value or 0):.0f}"
    except (TypeError, ValueError):
        return "0"


def score_number(value: Any) -> float:
    """Numeric score for Excel cells; corrupt values become ``0``."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_due_date(value: Any) -> date | None:
    """Parse a due/deadline into a calendar ``date``, or ``None`` if unknown."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def format_due(value: Any) -> str:
    """Display a due/deadline value. ISO datetimes collapse to the calendar date."""
    parsed = parse_due_date(value)
    if parsed is not None:
        return parsed.isoformat()
    if value is None or value == "":
        return "—"
    return str(value).strip() or "—"


def is_overdue(due: Any, *, as_of: Any = None) -> bool:
    """True when the due date is strictly before the as-of (or today) date."""
    due_d = parse_due_date(due)
    if due_d is None:
        return False
    ref = parse_due_date(as_of) or date.today()
    return due_d < ref


def score_parts(factors: dict[str, Any] | None) -> tuple[float | None, float | None, float | None]:
    """Return (urgency, impact, confidence) from an item's factors dict."""
    if not isinstance(factors, dict):
        return None, None, None
    out: list[float | None] = []
    for key in ("urgency", "impact", "confidence"):
        raw = factors.get(key)
        if raw is None:
            out.append(None)
            continue
        try:
            out.append(round(float(raw), 2))
        except (TypeError, ValueError):
            out.append(None)
    return out[0], out[1], out[2]


def score_breakdown_text(factors: dict[str, Any] | None) -> str:
    """Human-readable score breakdown for cells / PDF detail lines."""
    u, i, c = score_parts(factors)
    parts = []
    if u is not None:
        parts.append(f"U {u:.2f}")
    if i is not None:
        parts.append(f"I {i:.2f}")
    if c is not None:
        parts.append(f"C {c:.2f}")
    return " · ".join(parts)


def source_label(sources: list[dict[str, Any]] | None) -> str:
    if not sources:
        return ""
    first = next((s for s in sources if isinstance(s, dict)), None)
    if first is None:
        return ""
    label = f"{first.get('source_type', '')}: {first.get('title', '')}".strip(": ").strip()
    return label[:80]


def first_source(sources: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """First usable source dict, or ``None`` when the list is empty/corrupt."""
    if not sources:
        return None
    return next((s for s in sources if isinstance(s, dict)), None)


def sanitize_export_filename(project_name: str, ext: str) -> str:
    """Safe Content-Disposition filename stem from a project name."""
    stem = _FILENAME_SAFE.sub("_", (project_name or "project").strip()).strip("._") or "project"
    return f"osprey-hotlist-{stem[:80]}.{ext}"


def items_by_bucket(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group snapshot items by bucket, preserving rank order within each bucket."""
    grouped: dict[str, list[dict[str, Any]]] = {b: [] for b in BUCKET_ORDER}
    for item in items:
        key = item.get("bucket", "watch")
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(item)
    return grouped


def critical_items(items: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    """Highest-impact open work: Act-today items first, capped for the Summary rollup."""
    act = [i for i in items if i.get("bucket") == "act_today"]
    return act[:limit]
