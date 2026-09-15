"""PDF export (reportlab) — branded, bucketed, explainable hotlist report."""

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
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .common import (
    BUCKET_HEX,
    BUCKET_LABEL,
    BUCKET_ORDER,
    esc_xml,
    format_due,
    format_money,
    format_score,
    format_timestamp,
    global_ranks,
    is_overdue,
    item_key,
    items_by_bucket,
    score_breakdown_text,
    source_label,
)

INK = colors.HexColor("#0E1A2B")
EMBER = colors.HexColor("#FF6A2B")
MUTED = colors.HexColor("#667085")
LINE = colors.HexColor("#E4E7EC")
MIST = colors.HexColor("#F6F7F9")
PRIO_RED = colors.HexColor("#E5484D")
BUCKET_COLOR = {k: colors.HexColor(f"#{v}") for k, v in BUCKET_HEX.items()}


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
        "section": ParagraphStyle(
            "sec",
            parent=base["Normal"],
            textColor=colors.white,
            fontSize=10,
            fontName="Helvetica-Bold",
            leading=12,
        ),
        "cell": ParagraphStyle("c", parent=base["Normal"], fontSize=8, leading=10, textColor=INK),
        "cellsm": ParagraphStyle(
            "cs", parent=base["Normal"], fontSize=7, leading=9, textColor=MUTED
        ),
        "overdue": ParagraphStyle(
            "od",
            parent=base["Normal"],
            fontSize=7,
            leading=9,
            textColor=PRIO_RED,
            fontName="Helvetica-Bold",
        ),
        "empty": ParagraphStyle(
            "e", parent=base["Normal"], fontSize=10, textColor=MUTED, spaceBefore=12, spaceAfter=12
        ),
        "foot": ParagraphStyle("f", parent=base["Normal"], fontSize=7, textColor=MUTED),
    }


def _item_what_cell(item: dict[str, Any], st: dict[str, ParagraphStyle]) -> Paragraph:
    notice = ' <font color="#E5484D"><b>[NOTICE]</b></font>' if item.get("notice_deadline") else ""
    what = f"<b>{esc_xml(item.get('what', ''))}</b>{notice}"
    why = esc_xml(item.get("why", ""))
    breakdown = score_breakdown_text(item.get("factors"))
    src = esc_xml(source_label(item.get("sources")))
    detail_bits = [why]
    if breakdown:
        detail_bits.append(f"Score: {breakdown}")
    if src:
        detail_bits.append(f"Source: {src}")
    body = "<br/>".join(bit for bit in detail_bits if bit)
    return Paragraph(f"{what}<br/>{body}", st["cellsm"])


def _due_cell(item: dict[str, Any], st: dict[str, ParagraphStyle], *, as_of: Any) -> Paragraph:
    label = format_due(item.get("due"))
    if is_overdue(item.get("due"), as_of=as_of):
        return Paragraph(f"{esc_xml(label)} (overdue)", st["overdue"])
    return Paragraph(esc_xml(label), st["cellsm"])


def _section_table(
    items: list[dict[str, Any]],
    st: dict[str, ParagraphStyle],
    ranks: dict[Any, int],
    *,
    as_of: Any,
) -> Table:
    header = [
        Paragraph("<b>#</b>", st["cell"]),
        Paragraph("<b>What / Why</b>", st["cell"]),
        Paragraph("<b>Owner</b>", st["cell"]),
        Paragraph("<b>Due</b>", st["cell"]),
        Paragraph("<b>$ Exp.</b>", st["cell"]),
        Paragraph("<b>Action</b>", st["cell"]),
        Paragraph("<b>Score</b>", st["cell"]),
    ]
    data: list[list] = [header]
    style_cmds: list[tuple] = [
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, MIST]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for item in items:
        # Use the snapshot-global rank so Excel and PDF agree on numbering.
        rank = ranks.get(item_key(item), 0)
        data.append(
            [
                Paragraph(str(rank), st["cell"]),
                _item_what_cell(item, st),
                Paragraph(esc_xml(item.get("owner") or "—"), st["cellsm"]),
                _due_cell(item, st, as_of=as_of),
                Paragraph(format_money(item.get("dollar_exposure")), st["cellsm"]),
                Paragraph(esc_xml(item.get("recommended_action") or ""), st["cellsm"]),
                Paragraph(format_score(item.get("score")), st["cell"]),
            ]
        )

    table = Table(
        data,
        colWidths=[
            0.3 * inch,
            2.7 * inch,
            0.75 * inch,
            0.85 * inch,
            0.65 * inch,
            1.55 * inch,
            0.45 * inch,
        ],
        repeatRows=1,
    )
    table.setStyle(TableStyle(style_cmds))
    return table


def _page_chrome(canvas, doc) -> None:  # noqa: ANN001
    """Running footer with brand + page number (print-ready multi-page reports)."""
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.5)
    y = 0.4 * inch
    canvas.line(0.7 * inch, y + 10, letter[0] - 0.7 * inch, y + 10)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(0.7 * inch, y, "Osprey — The foreman that never sleeps.")
    canvas.drawRightString(letter[0] - 0.7 * inch, y, f"Page {doc.page}")
    canvas.restoreState()


def hotlist_to_pdf(
    payload: dict[str, Any],
    *,
    project_name: str = "Project",
    prepared_by: str | None = None,
) -> bytes:
    st = _styles()
    buf = io.BytesIO()
    # ~18mm margins — print/bind friendly without wasting page real estate.
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.7 * inch,
        rightMargin=0.7 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.65 * inch,
        title=f"Osprey Hotlist — {project_name}",
        author="Osprey",
    )
    flow: list = []
    flow.append(Paragraph("Osprey — Hotlist", st["title"]))
    flow.append(Paragraph("The foreman that never sleeps.", st["tag"]))

    items = list(payload.get("items") or [])
    as_of = payload.get("generated_at")
    overdue_n = sum(1 for it in items if is_overdue(it.get("due"), as_of=as_of))
    exposure = payload.get("total_exposure", 0) or 0
    prepared = esc_xml(prepared_by) if prepared_by else ""
    meta_bits = [
        f"Project: <b>{esc_xml(project_name)}</b>",
        f"Generated: {esc_xml(format_timestamp(payload.get('generated_at', '')))}",
    ]
    if prepared:
        meta_bits.append(f"Prepared by: {prepared}")
    meta_bits.extend(
        [
            f"Items: {payload.get('item_count', 0)}",
            f"Total exposure: {esc_xml(format_money(exposure))}",
        ]
    )
    if overdue_n:
        meta_bits.append(f'<font color="#E5484D"><b>Overdue: {overdue_n}</b></font>')
    flow.append(Paragraph(" &nbsp;·&nbsp; ".join(meta_bits), st["meta"]))
    flow.append(Spacer(1, 6))
    flow.append(HRFlowable(width="100%", thickness=1.2, color=EMBER))
    flow.append(Spacer(1, 8))

    if not items:
        flow.append(
            Paragraph(
                "No open items on this hotlist. Connect a source or refresh after ingesting signals.",
                st["empty"],
            )
        )
    else:
        ranks = global_ranks(items)
        grouped = items_by_bucket(items)
        buckets_meta = payload.get("buckets") or {}
        for bucket in BUCKET_ORDER:
            bucket_items = grouped.get(bucket) or []
            if not bucket_items:
                continue
            bmeta = buckets_meta.get(bucket) or {}
            count = bmeta.get("count", len(bucket_items))
            bexp = format_money(bmeta.get("exposure", 0))
            label = BUCKET_LABEL.get(bucket, bucket).upper()
            banner = Table(
                [
                    [
                        Paragraph(
                            f"{esc_xml(label)}  ·  {count} item(s)  ·  {esc_xml(bexp)}",
                            st["section"],
                        )
                    ]
                ],
                colWidths=[7.1 * inch],
            )
            banner.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), BUCKET_COLOR.get(bucket, colors.grey)),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ]
                )
            )
            table = _section_table(bucket_items, st, ranks, as_of=as_of)
            flow.append(KeepTogether([banner, Spacer(1, 4), table, Spacer(1, 10)]))

    flow.append(HRFlowable(width="100%", thickness=0.6, color=LINE))
    flow.append(Spacer(1, 4))
    flow.append(
        Paragraph(
            f"Generated by Osprey · {payload.get('item_count', 0)} items · "
            f"{esc_xml(format_timestamp(payload.get('generated_at', '')))} · "
            "Scores show Urgency · Impact · Confidence · Overdue dues are highlighted",
            st["foot"],
        )
    )
    doc.build(flow, onFirstPage=_page_chrome, onLaterPages=_page_chrome)
    return buf.getvalue()
