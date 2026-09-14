"""Shared helpers for Excel and PDF exports (same snapshot → agreeing reports)."""

from __future__ import annotations

import re
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


def format_due(value: Any) -> str:
    """Display a due/deadline value. ISO datetimes collapse to the calendar date."""
    if value is None or value == "":
        return "—"
    text = str(value).strip()
    if "T" in text:
        return text.split("T", 1)[0]
    return text


def score_parts(factors: dict[str, Any] | None) -> tuple[float | None, float | None, float | None]:
    """Return (urgency, impact, confidence) from an item's factors dict."""
    if not factors:
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
    first = sources[0]
    label = f"{first.get('source_type', '')}: {first.get('title', '')}".strip(": ").strip()
    return label[:80]


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
