"""PDF export (reportlab) — a branded, deterministic, print-ready hotlist one-pager."""

from __future__ import annotations

import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

INK = colors.HexColor("#0E1A2B")
EMBER = colors.HexColor("#FF6A2B")
MUTED = colors.HexColor("#667085")
LINE = colors.HexColor("#E4E7EC")
BUCKET_COLOR = {
    "act_today": colors.HexColor("#E5484D"),
    "this_week": colors.HexColor("#F5A623"),
    "watch": colors.HexColor("#EAB308"),
    "done": colors.HexColor("#30A46C"),
}
BUCKET_LABEL = {
    "act_today": "ACT TODAY",
    "this_week": "THIS WEEK",
    "watch": "WATCH",
    "done": "DONE",
}


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "t", parent=base["Title"], textColor=INK, fontSize=22, spaceAfter=2, alignment=TA_LEFT
        ),
        "tag": ParagraphStyle(
            "tag", parent=base["Normal"], textColor=MUTED, fontSize=9, italic=True, spaceAfter=8
        ),
        "meta": ParagraphStyle("m", parent=base["Normal"], textColor=MUTED, fontSize=8),
        "cell": ParagraphStyle("c", parent=base["Normal"], fontSize=8, leading=10, textColor=INK),
        "cellsm": ParagraphStyle(
            "cs", parent=base["Normal"], fontSize=7, leading=9, textColor=MUTED
        ),
        "foot": ParagraphStyle("f", parent=base["Normal"], fontSize=7, textColor=MUTED),
    }


def hotlist_to_pdf(payload: dict[str, Any], *, project_name: str = "Project") -> bytes:
    st = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        title=f"Osprey Hotlist — {project_name}",
        author="Osprey",
    )
    flow: list = []
    flow.append(Paragraph("Osprey — Hotlist", st["title"]))
    flow.append(Paragraph("The foreman that never sleeps.", st["tag"]))
    exposure = payload.get("total_exposure", 0) or 0
    meta = (
        f"Project: <b>{project_name}</b> &nbsp;·&nbsp; Prepared: {payload.get('generated_at', '')} "
        f"&nbsp;·&nbsp; Items: {payload.get('item_count', 0)} &nbsp;·&nbsp; "
        f"Total exposure: ${exposure:,.0f}"
    )
    flow.append(Paragraph(meta, st["meta"]))
    flow.append(Spacer(1, 6))
    flow.append(HRFlowable(width="100%", thickness=1.2, color=INK))
    flow.append(Spacer(1, 8))

    header = [
        Paragraph("<b>#</b>", st["cell"]),
        Paragraph("<b>Bucket</b>", st["cell"]),
        Paragraph("<b>What / Why</b>", st["cell"]),
        Paragraph("<b>Due</b>", st["cell"]),
        Paragraph("<b>$ Exp.</b>", st["cell"]),
        Paragraph("<b>Recommended action</b>", st["cell"]),
        Paragraph("<b>Score</b>", st["cell"]),
    ]
    data = [header]
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F7F9")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]

    for i, item in enumerate(payload.get("items", []), start=1):
        bucket = item.get("bucket", "watch")
        what = f"<b>{_esc(item.get('what', ''))}</b><br/>{_esc(item.get('why', ''))}"
        exp = item.get("dollar_exposure")
        row = [
            Paragraph(str(i), st["cell"]),
            Paragraph(BUCKET_LABEL.get(bucket, bucket), st["cell"]),
            Paragraph(what, st["cellsm"]),
            Paragraph(_esc(str(item.get("due") or "—")), st["cellsm"]),
            Paragraph(f"${exp:,.0f}" if exp else "—", st["cellsm"]),
            Paragraph(_esc(item.get("recommended_action", "")), st["cellsm"]),
            Paragraph(f"{item.get('score', 0):.0f}", st["cell"]),
        ]
        data.append(row)
        r = len(data) - 1
        style_cmds.append(("BACKGROUND", (1, r), (1, r), BUCKET_COLOR.get(bucket, colors.grey)))
        style_cmds.append(("TEXTCOLOR", (1, r), (1, r), colors.white))

    table = Table(
        data,
        colWidths=[
            0.3 * inch,
            0.75 * inch,
            2.9 * inch,
            0.75 * inch,
            0.7 * inch,
            2.0 * inch,
            0.5 * inch,
        ],
        repeatRows=1,
    )
    table.setStyle(TableStyle(style_cmds))
    flow.append(table)
    flow.append(Spacer(1, 10))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=LINE))
    flow.append(Spacer(1, 4))
    flow.append(
        Paragraph(
            f"Generated by Osprey · {payload.get('item_count', 0)} items · "
            f"{payload.get('generated_at', '')} · Ink #0E1A2B · Ember #FF6A2B",
            st["foot"],
        )
    )
    doc.build(flow)
    return buf.getvalue()


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
