"""Connector parsing/normalization against fixtures (no live services)."""

from __future__ import annotations

from osprey.connectors.base import registry
from osprey.connectors.filedrop import parse_csv, parse_email
from osprey.connectors.outlook import normalize_graph_message
from osprey.models import SourceKind

RAW_EMAIL = """\
From: Jane PM <jane@gc.com>
To: super@sub.com
Cc: owner@dev.com
Subject: RFI-0412 — beam penetration at grid C-4
Date: Wed, 22 Jul 2026 09:15:00 -0500
Message-ID: <rfi-0412@gc.com>

Please clarify the beam penetration detail at grid C-4. This blocks MEP rough-in.
Response needed by 2026-07-28 or the sequence slips.

On Tue, 21 Jul 2026, super@sub.com wrote:
> quoted history that should be stripped
"""


def test_registry_has_builtin_connectors():
    types = registry.types()
    assert "filedrop" in types
    assert "outlook" in types


def test_parse_email_extracts_thread_and_participants():
    ev = parse_email(RAW_EMAIL)
    assert ev.source_kind == SourceKind.email
    assert "RFI-0412" in ev.title
    assert ev.thread_key == "<rfi-0412@gc.com>"
    assert "jane@gc.com" in ev.participants
    assert "owner@dev.com" in ev.participants
    assert "quoted history" not in ev.body  # reply quote stripped
    assert ev.external_id == "<rfi-0412@gc.com>"


def test_parse_email_is_stable_external_id_when_no_message_id():
    raw = "Subject: no id\n\nbody text"
    a = parse_email(raw)
    b = parse_email(raw)
    assert a.external_id == b.external_id  # deterministic dedupe key


def test_parse_csv_rows_to_events():
    csv_text = "id,title,due,amount\nCO-12,Change order slab,2026-08-01,45000\nCO-13,Change order stair,,12000\n"
    events = parse_csv(csv_text)
    assert len(events) == 2
    assert events[0].external_id == "CO-12"
    assert events[0].amount == 45000.0
    assert events[0].due_at is not None


def test_outlook_graph_normalize():
    msg = {
        "id": "AAMk-123",
        "subject": "Submittal 03 30 00 concrete mix",
        "conversationId": "conv-9",
        "from": {"emailAddress": {"address": "arch@ae.com"}},
        "toRecipients": [{"emailAddress": {"address": "pm@gc.com"}}],
        "ccRecipients": [],
        "body": {"contentType": "text", "content": "Please review the concrete mix submittal."},
        "receivedDateTime": "2026-07-22T14:00:00Z",
        "webLink": "https://outlook.office.com/mail/id/AAMk-123",
    }
    ev = normalize_graph_message(msg)
    assert ev.external_id == "AAMk-123"
    assert ev.thread_key == "conv-9"
    assert "arch@ae.com" in ev.participants
    assert ev.url.endswith("AAMk-123")
