"""Sage Intacct open receivables: token exchange, paged query, filtering, errors.

Record shapes follow real responses from the Sage Intacct REST query service
(``accounts-receivable/invoice``). Nothing here talks to a live Sage company.
"""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from osprey.ai.base import ExtractionInput
from osprey.ai.deterministic import DeterministicProvider
from osprey.connectors import sage_intacct as sage
from osprey.connectors.base import Connection
from osprey.models import Category, SourceKind

TOKEN_URL = f"{sage.API_BASE}/oauth2/token"
QUERY_URL = f"{sage.API_BASE}/services/core/query"

OPEN_INVOICE = {
    "id": "238",
    "key": "238",
    "invoiceNumber": "SI0184",
    "documentId": "Sales Invoice-Inventory-SI0184",
    "referenceNumber": "PO-7781",
    "description": "Level 2 fit-out, progress billing 3",
    "invoiceDate": "2026-08-01",
    "dueDate": "2026-08-31",
    "state": "partiallyPaid",
    "totalTxnAmount": "70000.00",
    "totalTxnAmountDue": "25500.00",
    "webURL": "https://www.intacct.com/ia/acct/ur.phtml?.r=abc",
}
PAID_INVOICE = {
    **OPEN_INVOICE,
    "key": "211",
    "invoiceNumber": "SI0157",
    "state": "paid",
    "totalTxnAmountDue": "0.00",
}


def _conn(**tokens: str) -> Connection:
    return Connection(
        id="c1",
        source_type="sage-intacct",
        tokens=tokens or {"client_id": "cid", "client_secret": "secret", "entity": "WEST"},
    )


def _page(records: list[dict], *, start: int, next_start: int | None, total: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "ia::result": records,
            "ia::meta": {
                "totalCount": total,
                "start": start,
                "pageSize": sage.PAGE_SIZE,
                "next": next_start,
                "previous": None,
            },
        },
    )


def test_an_open_invoice_becomes_a_ranked_receivable():
    event = sage.normalize_ar_invoice(OPEN_INVOICE)

    assert event.external_id == "sage-intacct:ar-invoice:238"
    assert event.source_kind == SourceKind.invoice
    assert event.title == "Unpaid invoice SI0184: $25,500.00 outstanding, due 2026-08-31"
    assert event.amount == 25500.0
    assert event.due_at == datetime(2026, 8, 31)
    assert event.url == OPEN_INVOICE["webURL"]
    assert "partiallyPaid" in event.body and "PO-7781" in event.body


def test_only_invoices_with_money_due_are_open():
    assert sage.is_open(OPEN_INVOICE)
    assert not sage.is_open(PAID_INVOICE)
    assert not sage.is_open({**OPEN_INVOICE, "totalTxnAmountDue": "0.00"})


async def test_poll_exchanges_credentials_then_pages_the_query(monkeypatch):
    monkeypatch.setattr(sage, "PAGE_SIZE", 2)
    with respx.mock() as mock:
        token = mock.post(TOKEN_URL).respond(200, json={"access_token": "ia-token"})
        query = mock.post(QUERY_URL).mock(
            side_effect=[
                _page([OPEN_INVOICE, PAID_INVOICE], start=1, next_start=3, total=3),
                _page([{**OPEN_INVOICE, "key": "300"}], start=3, next_start=None, total=3),
            ]
        )
        events = [e async for e in sage.SageIntacctConnector().poll(_conn(), since=None)]

    assert [e.external_id for e in events] == [
        "sage-intacct:ar-invoice:238",
        "sage-intacct:ar-invoice:300",
    ]
    assert parse_qs(token.calls.last.request.content.decode()) == {
        "grant_type": ["client_credentials"],
        "client_id": ["cid"],
        "client_secret": ["secret"],
    }
    first, second = (c.request for c in query.calls)
    assert first.headers["authorization"] == "Bearer ia-token"
    assert first.headers["x-ia-api-param-entity"] == "WEST"
    body = json.loads(first.content)
    assert body["object"] == "accounts-receivable/invoice"
    assert body["start"] == 1 and body["size"] == 2
    assert json.loads(second.content)["start"] == 3


async def test_an_ia_error_is_raised_not_read_as_no_invoices():
    error = {
        "ia::result": {
            "ia::error": {
                "code": "invalidRequest",
                "message": "The action field in accounts-receivable/invoice object cannot be used in query",
            }
        },
        "ia::meta": {"totalCount": 1, "totalSuccess": 0, "totalError": 1},
    }
    with respx.mock() as mock:
        mock.post(TOKEN_URL).respond(200, json={"access_token": "t"})
        mock.post(QUERY_URL).respond(200, json=error)
        with pytest.raises(sage.SageIntacctError, match="cannot be used in query"):
            async for _ in sage.SageIntacctConnector().poll(_conn(), since=None):
                pass


async def test_rejected_credentials_raise():
    with respx.mock() as mock:
        mock.post(TOKEN_URL).respond(401, json={"error": "invalid_client"})
        with pytest.raises(httpx.HTTPStatusError):
            async for _ in sage.SageIntacctConnector().poll(_conn(), since=None):
                pass


async def test_a_connection_without_credentials_is_refused():
    conn = Connection(id="c1", source_type="sage-intacct", tokens={})
    with pytest.raises(ValueError, match="client_id and client_secret"):
        async for _ in sage.SageIntacctConnector().poll(conn, since=None):
            pass
    assert (await sage.SageIntacctConnector().healthcheck(conn)).ok is False


async def test_an_overdue_receivable_is_extracted_as_an_invoice_with_its_exposure():
    event = sage.normalize_ar_invoice(OPEN_INVOICE)
    extraction = await DeterministicProvider().extract(
        ExtractionInput(
            item_title=event.title,
            signals=[
                {
                    "id": "s1",
                    "title": event.title,
                    "body": event.body,
                    "due_at": event.due_at.date().isoformat(),
                    "amount": event.amount,
                }
            ],
        )
    )

    assert extraction.category == Category.invoice
    assert extraction.dollar_exposure == 25500.0
    assert extraction.deadline == "2026-08-31"
