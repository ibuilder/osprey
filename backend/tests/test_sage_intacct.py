"""Sage Intacct receivables and payables: token, paged query, bill field discovery, errors.

Invoice record shapes follow real responses from the Sage Intacct REST query
service. Bill field names are deliberately *not* assumed: the model response here
is shaped like the model service's (``ia::result.fields``), and the tests cover a
company whose model has the fields, one whose model lacks them, and credentials
that may not read payables. Nothing here talks to a live Sage company.
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
MODEL_URL = f"{sage.API_BASE}/services/core/model"

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

#: A bill model as the model service describes it: field name -> definition.
BILL_MODEL = {
    "ia::result": {
        "fields": {
            "key": {"type": "string"},
            "id": {"type": "string"},
            "billNumber": {"type": "string"},
            "dueDate": {"type": "date"},
            "totalTxnAmountDue": {"type": "number"},
            "totalTxnAmount": {"type": "number"},
            "state": {"type": "string"},
            "createdDate": {"type": "date"},
            "webURL": {"type": "string"},
            "ia::meta": {"type": "object"},
        }
    }
}
OPEN_BILL = {
    "key": "9001",
    "billNumber": "B-3312",
    "dueDate": "2026-09-05",
    "totalTxnAmountDue": "48250.00",
    "totalTxnAmount": "48250.00",
    "state": "posted",
    "createdDate": "2026-08-06",
    "webURL": "https://www.intacct.com/ia/acct/ur.phtml?.r=bill",
}
PAID_BILL = {**OPEN_BILL, "key": "9002", "state": "paid", "totalTxnAmountDue": "0.00"}


def _conn(**tokens: str) -> Connection:
    return Connection(
        id="c1",
        source_type="sage-intacct",
        tokens=tokens or {"client_id": "cid", "client_secret": "secret", "entity": "WEST"},
    )


def _page(records, *, start: int = 1, next_start: int | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "ia::result": records,
            "ia::meta": {
                "totalCount": len(records),
                "start": start,
                "pageSize": sage.PAGE_SIZE,
                "next": next_start,
                "previous": None,
            },
        },
    )


def _by_object(pages: dict[str, list[httpx.Response]]):
    """A respx side effect that serves each queried object its own pages, in order."""
    served: dict[str, int] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        obj = json.loads(request.content)["object"]
        index = served.get(obj, 0)
        served[obj] = index + 1
        return pages[obj][index]

    return respond


async def _poll(**tokens: str):
    return [e async for e in sage.SageIntacctConnector().poll(_conn(**tokens), since=None)]


# --------------------------------------------------------------------------- #
# Receivables
# --------------------------------------------------------------------------- #
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


async def test_poll_reads_receivables_then_payables(monkeypatch):
    monkeypatch.setattr(sage, "PAGE_SIZE", 2)
    with respx.mock() as mock:
        token = mock.post(TOKEN_URL).respond(200, json={"access_token": "ia-token"})
        model = mock.get(MODEL_URL).respond(200, json=BILL_MODEL)
        query = mock.post(QUERY_URL).mock(
            side_effect=_by_object(
                {
                    sage.INVOICE_OBJECT: [
                        _page([OPEN_INVOICE, PAID_INVOICE], start=1, next_start=3),
                        _page([{**OPEN_INVOICE, "key": "300"}], start=3),
                    ],
                    sage.BILL_OBJECT: [_page([OPEN_BILL, PAID_BILL])],
                }
            )
        )
        events = await _poll()

    assert [e.external_id for e in events] == [
        "sage-intacct:ar-invoice:238",
        "sage-intacct:ar-invoice:300",
        "sage-intacct:ap-bill:9001",
    ]
    assert parse_qs(token.calls.last.request.content.decode()) == {
        "grant_type": ["client_credentials"],
        "client_id": ["cid"],
        "client_secret": ["secret"],
    }
    first = query.calls[0].request
    assert first.headers["authorization"] == "Bearer ia-token"
    assert first.headers["x-ia-api-param-entity"] == "WEST"
    assert json.loads(query.calls[1].request.content)["start"] == 3
    # Bill fields came from the model, not from a list compiled in advance.
    assert model.calls.last.request.url.params["name"] == sage.BILL_OBJECT
    bill_query = json.loads(query.calls[2].request.content)
    assert bill_query["object"] == sage.BILL_OBJECT
    assert set(bill_query["fields"]) == {
        "key",
        "billNumber",
        "dueDate",
        "totalTxnAmountDue",
        "totalTxnAmount",
        "state",
        "createdDate",
        "webURL",
    }


# --------------------------------------------------------------------------- #
# Payables
# --------------------------------------------------------------------------- #
def test_bill_fields_are_chosen_from_what_the_model_has():
    fields = sage.choose_bill_fields(BILL_MODEL["ia::result"]["fields"])

    assert fields == {
        "key": "key",
        "number": "billNumber",
        "due": "dueDate",
        "amount_due": "totalTxnAmountDue",
        "total": "totalTxnAmount",
        "state": "state",
        "date": "createdDate",
        "url": "webURL",
    }
    # A different template's spellings still map.
    other = sage.choose_bill_fields(["id", "recordId", "dueDate", "totalBaseAmountDue"])
    assert other == {
        "key": "id",
        "number": "recordId",
        "due": "dueDate",
        "amount_due": "totalBaseAmountDue",
    }


def test_a_bill_model_without_a_due_date_or_amount_is_unrankable():
    assert sage.choose_bill_fields(["key", "billNumber", "totalTxnAmountDue"]) is None
    assert sage.choose_bill_fields(["key", "dueDate"]) is None


def test_an_open_bill_becomes_a_ranked_payable():
    fields = sage.choose_bill_fields(BILL_MODEL["ia::result"]["fields"])
    event = sage.normalize_ap_bill(OPEN_BILL, fields)

    assert event.external_id == "sage-intacct:ap-bill:9001"
    assert (
        event.title
        == "Bill B-3312 to pay: $48,250.00 outstanding (accounts payable), due 2026-09-05"
    )
    assert event.amount == 48250.0
    assert event.due_at == datetime(2026, 9, 5)
    assert event.url == OPEN_BILL["webURL"]
    assert sage.is_open_bill(OPEN_BILL, fields)
    assert not sage.is_open_bill(PAID_BILL, fields)


async def test_receivables_still_sync_when_the_bill_model_is_unusable():
    unusable = {"ia::result": {"fields": {"key": {}, "billNumber": {}}}}
    with respx.mock() as mock:
        mock.post(TOKEN_URL).respond(200, json={"access_token": "t"})
        mock.get(MODEL_URL).respond(200, json=unusable)
        query = mock.post(QUERY_URL).mock(
            side_effect=_by_object({sage.INVOICE_OBJECT: [_page([OPEN_INVOICE])]})
        )
        events = await _poll()

    assert [e.external_id for e in events] == ["sage-intacct:ar-invoice:238"]
    assert query.call_count == 1  # no bill query built on guessed fields


async def test_receivables_still_sync_when_payables_are_off_limits():
    with respx.mock() as mock:
        mock.post(TOKEN_URL).respond(200, json={"access_token": "t"})
        mock.get(MODEL_URL).respond(403, json={"error": "permission denied"})
        mock.post(QUERY_URL).mock(
            side_effect=_by_object({sage.INVOICE_OBJECT: [_page([OPEN_INVOICE])]})
        )
        events = await _poll()

    assert [e.external_id for e in events] == ["sage-intacct:ar-invoice:238"]


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #
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
            await _poll()


async def test_rejected_credentials_raise():
    with respx.mock() as mock:
        mock.post(TOKEN_URL).respond(401, json={"error": "invalid_client"})
        with pytest.raises(httpx.HTTPStatusError):
            await _poll()


async def test_a_connection_without_credentials_is_refused():
    conn = Connection(id="c1", source_type="sage-intacct", tokens={})
    with pytest.raises(ValueError, match="client_id and client_secret"):
        async for _ in sage.SageIntacctConnector().poll(conn, since=None):
            pass
    assert (await sage.SageIntacctConnector().healthcheck(conn)).ok is False


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
async def _extract(event):
    return await DeterministicProvider().extract(
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


async def test_an_overdue_receivable_is_extracted_as_an_invoice_with_its_exposure():
    extraction = await _extract(sage.normalize_ar_invoice(OPEN_INVOICE))

    assert extraction.category == Category.invoice
    assert extraction.dollar_exposure == 25500.0
    assert extraction.deadline == "2026-08-31"


async def test_a_payable_is_extracted_as_an_invoice_with_its_exposure():
    fields = sage.choose_bill_fields(BILL_MODEL["ia::result"]["fields"])
    extraction = await _extract(sage.normalize_ap_bill(OPEN_BILL, fields))

    assert extraction.category == Category.invoice
    assert extraction.dollar_exposure == 48250.0
    assert extraction.deadline == "2026-09-05"
