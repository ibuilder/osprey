"""Excel export (openpyxl) — Summary + Hotlist + Raw sheets, brand-styled."""

from __future__ import annotations

import io
import json
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .common import (
    BUCKET_HEX,
    BUCKET_LABEL,
    BUCKET_ORDER,
    format_money,
    score_parts,
    source_label,
)

# Brand tokens (BRAND.md)
INK = "0E1A2B"
EMBER = "FF6A2B"
MIST = "F6F7F9"
WHITE = "FFFFFF"
MUTED = "667085"
_THIN = Side(style="thin", color="E4E7EC")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _header(cell, text: str) -> None:
    cell.value = text
    cell.font = Font(bold=True, color=WHITE, name="Calibri", size=11)
    cell.fill = PatternFill("solid", fgColor=INK)
    cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    cell.border = _BORDER


def _money_cell(cell, value: Any) -> None:
    """Write a currency cell. ``None`` stays blank; ``0`` is a real zero."""
    if value is None or value == "":
        cell.value = None
        return
    try:
        cell.value = round(float(value), 2)
        cell.number_format = "$#,##0"
    except (TypeError, ValueError):
        cell.value = None


def hotlist_to_xlsx(
    payload: dict[str, Any],
    *,
    project_name: str = "Project",
    prepared_by: str | None = None,
) -> bytes:
    wb = Workbook()

    # ---- Summary ----------------------------------------------------------- #
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "Osprey — Hotlist"
    ws["A1"].font = Font(bold=True, size=18, color=INK)
    ws["A2"] = "The foreman that never sleeps."
    ws["A2"].font = Font(italic=True, size=10, color=MUTED)
    ws["A4"] = "Project"
    ws["B4"] = project_name
    ws["A5"] = "Generated"
    ws["B5"] = payload.get("generated_at", "")
    ws["A6"] = "Prepared by"
    ws["B6"] = prepared_by or ""
    ws["A7"] = "Items"
    ws["B7"] = payload.get("item_count", 0)
    ws["A8"] = "Total $ exposure"
    _money_cell(ws["B8"], payload.get("total_exposure", 0))
    for r in range(4, 9):
        ws[f"A{r}"].font = Font(bold=True, color=INK)

    ws["A10"] = "Bucket"
    ws["B10"] = "Count"
    ws["C10"] = "$ Exposure"
    for col in ("A10", "B10", "C10"):
        _header(ws[col], ws[col].value)
    buckets = payload.get("buckets", {})
    row = 11
    for key in BUCKET_ORDER:
        b = buckets.get(key, {"count": 0, "exposure": 0.0})
        ws.cell(row=row, column=1, value=BUCKET_LABEL[key])
        ws.cell(row=row, column=2, value=b.get("count", 0))
        _money_cell(ws.cell(row=row, column=3), b.get("exposure", 0.0))
        fill = PatternFill("solid", fgColor=BUCKET_HEX[key])
        ws.cell(row=row, column=1).fill = fill
        ws.cell(row=row, column=1).font = Font(bold=True, color=WHITE)
        row += 1
    for col, width in {"A": 22, "B": 12, "C": 16}.items():
        ws.column_dimensions[col].width = width

    # ---- Hotlist ----------------------------------------------------------- #
    hs = wb.create_sheet("Hotlist")
    cols = [
        ("Rank", 6),
        ("Bucket", 12),
        ("Notice", 8),
        ("What", 40),
        ("Category", 16),
        ("Why", 46),
        ("Owner", 14),
        ("Due", 12),
        ("$ Exposure", 12),
        ("Recommended action", 40),
        ("Score", 8),
        ("Urgency", 10),
        ("Impact", 10),
        ("Confidence", 11),
        ("Source", 36),
    ]
    for idx, (name, width) in enumerate(cols, start=1):
        _header(hs.cell(row=1, column=idx), name)
        hs.column_dimensions[get_column_letter(idx)].width = width
    hs.freeze_panes = "A2"

    for i, item in enumerate(payload.get("items", []), start=1):
        r = i + 1
        bucket = item.get("bucket", "watch")
        urgency, impact, confidence = score_parts(item.get("factors"))
        vals: list[Any] = [
            i,
            item.get("bucket_label") or BUCKET_LABEL.get(bucket, bucket),
            "NOTICE" if item.get("notice_deadline") else "",
            item.get("what", ""),
            item.get("category", ""),
            item.get("why", ""),
            item.get("owner") or "",
            item.get("due") or "",
            None,  # $ Exposure filled below so 0 ≠ blank
            item.get("recommended_action", ""),
            item.get("score", 0),
            urgency,
            impact,
            confidence,
            source_label(item.get("sources")),
        ]
        for c_idx, v in enumerate(vals, start=1):
            cell = hs.cell(row=r, column=c_idx, value=v)
            cell.alignment = Alignment(vertical="top", wrap_text=c_idx in (4, 6, 10))
            cell.border = _BORDER
        _money_cell(hs.cell(row=r, column=9), item.get("dollar_exposure"))
        # Bucket cell fill + first source hyperlink.
        bcell = hs.cell(row=r, column=2)
        bcell.fill = PatternFill("solid", fgColor=BUCKET_HEX.get(bucket, "EAB308"))
        bcell.font = Font(bold=True, color=WHITE)
        if item.get("notice_deadline"):
            ncell = hs.cell(row=r, column=3)
            ncell.font = Font(bold=True, color="E5484D")
        sources = item.get("sources") or []
        src_cell = hs.cell(row=r, column=15)
        if sources and sources[0].get("url"):
            src_cell.hyperlink = sources[0]["url"]
            src_cell.font = Font(color=EMBER, underline="single")
    if payload.get("items"):
        hs.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{len(payload['items']) + 1}"

    # ---- Raw (audit) ------------------------------------------------------- #
    raw = wb.create_sheet("Raw")
    _header(raw.cell(row=1, column=1), "item_id")
    _header(raw.cell(row=1, column=2), "money_display")
    _header(raw.cell(row=1, column=3), "factors (json)")
    raw.column_dimensions["A"].width = 36
    raw.column_dimensions["B"].width = 14
    raw.column_dimensions["C"].width = 120
    for i, item in enumerate(payload.get("items", []), start=2):
        raw.cell(row=i, column=1, value=item.get("item_id", ""))
        raw.cell(row=i, column=2, value=format_money(item.get("dollar_exposure")))
        raw.cell(row=i, column=3, value=json.dumps(item.get("factors", {}), default=str))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
