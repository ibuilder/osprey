"""Excel + PDF exports derive from the same snapshot payload and are valid files."""

from __future__ import annotations

import io
from datetime import date

from openpyxl import load_workbook

from osprey.exports import format_money, hotlist_to_pdf, hotlist_to_xlsx, sanitize_export_filename
from osprey.exports.common import (
    critical_items,
    esc_xml,
    format_due,
    is_overdue,
    score_breakdown_text,
)

PAYLOAD = {
    "project_id": "p1",
    "generated_at": "2026-07-23T00:00:00+00:00",
    "top_n": 25,
    "item_count": 3,
    "total_exposure": 180000.0,
    "buckets": {
        "act_today": {"count": 1, "exposure": 180000.0},
        "this_week": {"count": 1, "exposure": 0.0},
        "watch": {"count": 1, "exposure": 0.0},
        "done": {"count": 0, "exposure": 0.0},
    },
    "items": [
        {
            "item_id": "i1",
            "what": "NOTICE OF DELAY — Tower B",
            "category": "contractual_notice",
            "bucket": "act_today",
            "bucket_label": "Act today",
            "bucket_emoji": "🔴",
            "why": "Act today: contractual notice deadline; deadline 2026-07-20.",
            "summary": "s",
            "sources": [{"source_type": "filedrop", "title": "Notice", "url": "https://x/1"}],
            "owner": "PM",
            "due": "2026-07-20",  # overdue vs generated_at
            "dollar_exposure": 180000.0,
            "recommended_action": "Respond in writing before the notice lapses.",
            "notice_deadline": True,
            "score": 88.0,
            "factors": {"urgency": 0.95, "impact": 0.9, "confidence": 0.8},
        },
        {
            "item_id": "i2",
            "what": "Pay Application 07",
            "category": "invoice",
            "bucket": "this_week",
            "bucket_label": "This week",
            "bucket_emoji": "🟠",
            "why": "This week: zero-dollar invoice still needs review.",
            "summary": "s2",
            "sources": [],
            "owner": None,
            "due": None,
            "dollar_exposure": 0.0,
            "recommended_action": "Verify against SOV and approve.",
            "notice_deadline": False,
            "score": 52.0,
            "factors": {"urgency": 0.4, "impact": 0.2, "confidence": 0.7},
        },
        {
            "item_id": "i3",
            "what": "Watch item with no $",
            "category": "other",
            "bucket": "watch",
            "bucket_label": "Watch",
            "bucket_emoji": "🟡",
            "why": "No exposure recorded.",
            "summary": "s3",
            "sources": [{"source_type": "email", "title": "FYI", "url": None}],
            "owner": "Superintendent",
            "due": "2026-08-01T00:00:00+00:00",
            "dollar_exposure": None,
            "recommended_action": "Keep an eye on it.",
            "notice_deadline": False,
            "score": 31.0,
            "factors": {"urgency": 0.2, "impact": 0.1, "confidence": 0.6},
        },
    ],
}


def test_format_money_distinguishes_zero_from_missing():
    assert format_money(0) == "$0"
    assert format_money(0.0) == "$0"
    assert format_money(None) == "—"
    assert format_money(180000) == "$180,000"


def test_format_due_collapses_iso_datetimes():
    assert format_due(None) == "—"
    assert format_due("2026-07-29") == "2026-07-29"
    assert format_due("2026-09-20T00:00:00+00:00") == "2026-09-20"


def test_is_overdue_against_as_of():
    assert is_overdue("2026-07-20", as_of="2026-07-23T00:00:00+00:00")
    assert not is_overdue("2026-08-01", as_of="2026-07-23")
    assert not is_overdue(None, as_of="2026-07-23")


def test_critical_items_are_act_today_capped():
    crit = critical_items(PAYLOAD["items"], limit=5)
    assert len(crit) == 1
    assert crit[0]["item_id"] == "i1"


def test_sanitize_export_filename_strips_unsafe_chars():
    assert (
        sanitize_export_filename('Tower B / "Phase 2"', "pdf")
        == "osprey-hotlist-Tower_B_Phase_2.pdf"
    )
    assert sanitize_export_filename("A & B <C>", "xlsx").endswith(".xlsx")
    assert " " not in sanitize_export_filename("x y", "pdf")


def test_esc_xml_and_score_breakdown():
    assert "&amp;" in esc_xml("A & B")
    assert "&lt;" in esc_xml("<x>")
    assert score_breakdown_text({"urgency": 0.95, "impact": 0.9, "confidence": 0.8}) == (
        "U 0.95 · I 0.90 · C 0.80"
    )


def test_xlsx_export_is_valid_workbook():
    data = hotlist_to_xlsx(PAYLOAD, project_name="Tower B", prepared_by="pm@gc.com")
    assert data[:2] == b"PK"  # xlsx is a zip
    wb = load_workbook(io.BytesIO(data))
    assert {"Summary", "Hotlist", "Raw"}.issubset(set(wb.sheetnames))

    summary = wb["Summary"]
    assert summary["B4"].value == "Tower B"
    assert summary["B6"].value == "pm@gc.com"
    assert summary["B7"].value == 3
    assert summary["B8"].value == 180000
    assert summary["B9"].value == 1  # one overdue item
    assert summary["A18"].value == "Critical action items (Act today)"
    assert summary["A20"].value == "NOTICE OF DELAY — Tower B"

    hs = wb["Hotlist"]
    # Header + 3 data rows.
    assert hs.max_row == 4
    assert hs.page_setup.orientation == "landscape"
    assert hs.page_setup.fitToWidth == 1
    assert hs.print_title_rows in ("1:1", "$1:$1")
    assert hs.cell(row=1, column=12).value == "Urgency"
    assert hs.cell(row=2, column=4).value == "NOTICE OF DELAY — Tower B"
    assert hs.cell(row=2, column=3).value == "NOTICE"
    assert hs.cell(row=2, column=12).value == 0.95
    assert hs.cell(row=2, column=15).value == "filedrop: Notice"
    assert hs.cell(row=2, column=15).hyperlink.target == "https://x/1"
    # $0 must remain a real zero, not blank.
    assert hs.cell(row=3, column=9).value == 0
    # Missing exposure stays blank in the currency column.
    assert hs.cell(row=4, column=9).value is None
    # Due dates are real Excel dates (sortable), not text.
    due_overdue = hs.cell(row=2, column=8).value
    due_future = hs.cell(row=4, column=8).value
    assert (due_overdue.date() if hasattr(due_overdue, "date") else due_overdue) == date(
        2026, 7, 20
    )
    assert (due_future.date() if hasattr(due_future, "date") else due_future) == date(2026, 8, 1)
    assert hs.cell(row=2, column=8).font.color.rgb.endswith("E5484D")

    raw = wb["Raw"]
    assert raw.cell(row=3, column=2).value == "$0"
    assert raw.cell(row=4, column=2).value == "—"


def test_pdf_export_is_valid_pdf():
    data = hotlist_to_pdf(PAYLOAD, project_name="Tower B", prepared_by="seed")
    assert data[:5] == b"%PDF-"
    assert len(data) > 800


def test_pdf_survives_markup_in_project_name_and_item_text():
    nasty = {
        **PAYLOAD,
        "items": [
            {
                **PAYLOAD["items"][0],
                "what": "Delay <Phase 2> & punch",
                "why": "Owner said A < B & C",
                "recommended_action": "Reply with <ack> & schedule",
            }
        ],
        "item_count": 1,
        "buckets": {
            "act_today": {"count": 1, "exposure": 180000.0},
            "this_week": {"count": 0, "exposure": 0.0},
            "watch": {"count": 0, "exposure": 0.0},
            "done": {"count": 0, "exposure": 0.0},
        },
    }
    data = hotlist_to_pdf(nasty, project_name="A & B <Tower>")
    assert data[:5] == b"%PDF-"


def test_pdf_empty_hotlist_still_renders():
    empty = {
        "generated_at": "2026-07-23T00:00:00+00:00",
        "item_count": 0,
        "total_exposure": 0,
        "buckets": {
            k: {"count": 0, "exposure": 0.0} for k in ("act_today", "this_week", "watch", "done")
        },
        "items": [],
    }
    data = hotlist_to_pdf(empty, project_name="Empty")
    assert data[:5] == b"%PDF-"


def test_exports_agree_on_item_count_and_money():
    xlsx = hotlist_to_xlsx(PAYLOAD, project_name="P")
    wb = load_workbook(io.BytesIO(xlsx))
    xlsx_rows = wb["Hotlist"].max_row - 1
    assert xlsx_rows == PAYLOAD["item_count"]
    assert wb["Hotlist"].cell(row=3, column=9).value == 0
    # PDF built from the same payload -> same source of truth.
    assert hotlist_to_pdf(PAYLOAD, project_name="P")[:5] == b"%PDF-"
    assert format_money(PAYLOAD["items"][1]["dollar_exposure"]) == "$0"
    assert format_money(PAYLOAD["items"][2]["dollar_exposure"]) == "—"
