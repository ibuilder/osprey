"""Sage Intacct: open receivables through the REST API (SPEC §6, Tier 2).

Unpaid customer invoices are among the financial signals SPEC asks Sage for. This
reads Accounts Receivable invoices through the Sage Intacct REST API::

    POST https://api.intacct.com/ia/api/v1/oauth2/token        (client_credentials)
    POST https://api.intacct.com/ia/api/v1/services/core/query
         {"object": "accounts-receivable/invoice", "fields": [...], "start": 1, "size": N}
      -> {"ia::result": [...], "ia::meta": {"totalCount", "start", "pageSize", "next", "previous"}}

``next`` is the ``start`` of the following page, or null on the last one. An error
comes back as an ``ia::error`` object inside ``ia::result``, which is raised rather
than read as an empty page.

**Credentials, not a desktop consent flow.** The connection holds a Sage Intacct
client id and secret (and optionally an entity id), sealed at rest like every
other token. That is the flow production integrations use against this API. The
browser authorization-code parameters could not be confirmed against Sage's
documentation, so this module does not guess at them. Give the client a
read-only role in Sage Intacct: Osprey never writes.

Scope today is **AR invoices that still have money due**. Paid invoices are skipped.
AP bills are left out until their field names are confirmed, rather than queried
by guesswork.

Endpoint paths, the query body, pagination and the invoice fields used here come
from recorded responses of an open-source production extractor, not from a live
Sage company.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime

from ...models import SourceKind, utcnow
from ...normalize import clean_text
from ..base import Connection as ConnView
from ..base import Connector, Health, NormalizedSignal, RawEvent, registry
from ..http import connector_client

log = logging.getLogger(__name__)

SOURCE_TYPE = "sage-intacct"
API_BASE = "https://api.intacct.com/ia/api/v1"

INVOICE_OBJECT = "accounts-receivable/invoice"
#: Only fields seen in real responses. Asking for one the object does not allow in
#: a query fails the whole request.
INVOICE_FIELDS = [
    "id",
    "key",
    "invoiceNumber",
    "documentId",
    "referenceNumber",
    "description",
    "invoiceDate",
    "dueDate",
    "state",
    "totalTxnAmount",
    "totalTxnAmountDue",
    "webURL",
]

PAGE_SIZE = 500
#: A backstop against a pagination bug looping forever.
MAX_PAGES = 200


class SageIntacctError(RuntimeError):
    """Sage Intacct answered with an ``ia::error`` instead of results."""


def _num(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _date(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except ValueError:
        return None


def is_open(invoice: dict) -> bool:
    """Still owed: not marked paid, and a positive amount due."""
    due = _num(invoice.get("totalTxnAmountDue"))
    return str(invoice.get("state", "")).lower() != "paid" and due is not None and due > 0


def normalize_ar_invoice(invoice: dict) -> RawEvent:
    """Map one AR invoice record to a RawEvent. Pure."""
    key = str(invoice.get("key") or invoice.get("id"))
    number = invoice.get("invoiceNumber") or invoice.get("documentId") or key
    due_amount = _num(invoice.get("totalTxnAmountDue"))
    total = _num(invoice.get("totalTxnAmount"))
    due_at = _date(invoice.get("dueDate"))
    state = invoice.get("state") or "open"

    outstanding = f"${due_amount:,.2f}" if due_amount is not None else "an amount"
    title = f"Unpaid invoice {number}: {outstanding} outstanding"
    if due_at is not None:
        title += f", due {due_at:%Y-%m-%d}"

    lines = [
        f"Receivable {number} is {state}: {outstanding} outstanding"
        + (f" of ${total:,.2f}" if total is not None else "")
        + (f", due {due_at:%Y-%m-%d}." if due_at is not None else "."),
    ]
    if invoice.get("referenceNumber"):
        lines.append(f"Reference: {invoice['referenceNumber']}")
    if invoice.get("description"):
        lines.append(str(invoice["description"]))

    return RawEvent(
        external_id=f"{SOURCE_TYPE}:ar-invoice:{key}",
        source_kind=SourceKind.invoice,
        thread_key=f"{SOURCE_TYPE}:ar-invoice:{key}",
        title=title,
        body=clean_text("\n".join(lines), drop_quoted=False),
        due_at=due_at,
        amount=due_amount,
        url=invoice.get("webURL") or None,
        raw={
            "state": state,
            "invoice_date": invoice.get("invoiceDate"),
            "total": total,
            "document_id": invoice.get("documentId"),
        },
        occurred_at=_date(invoice.get("invoiceDate")) or utcnow(),
    )


@registry.register
class SageIntacctConnector(Connector):
    source_type = SOURCE_TYPE
    # Access is governed by the Sage Intacct role of the client credentials, not by
    # OAuth scopes; give that role read-only permissions.
    scopes: list[str] = []
    supports_webhooks = False

    async def poll(self, conn: ConnView, since: datetime | None) -> AsyncIterator[RawEvent]:
        client_id = conn.tokens.get("client_id", "")
        client_secret = conn.tokens.get("client_secret", "")
        if not (client_id and client_secret):
            raise ValueError("a Sage Intacct connection needs client_id and client_secret")

        async with connector_client(SOURCE_TYPE, base_url=API_BASE, timeout=60) as client:
            token = await client.post(
                "/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
            token.raise_for_status()
            headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
            if conn.tokens.get("entity"):
                # Multi-entity companies: which entity's books to read.
                headers["X-IA-API-Param-Entity"] = conn.tokens["entity"]

            start = 1
            for _ in range(MAX_PAGES):
                resp = await client.post(
                    "/services/core/query",
                    headers=headers,
                    json={
                        "object": INVOICE_OBJECT,
                        "fields": INVOICE_FIELDS,
                        "start": start,
                        "size": PAGE_SIZE,
                        "filterParameters": {"includePrivate": True},
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                result = data.get("ia::result")
                if isinstance(result, dict) and "ia::error" in result:
                    raise SageIntacctError(
                        result["ia::error"].get("message") or "Sage Intacct returned an error"
                    )
                records = result or []
                for invoice in records:
                    if is_open(invoice):
                        yield normalize_ar_invoice(invoice)
                next_start = (data.get("ia::meta") or {}).get("next")
                if not records or not isinstance(next_start, int):
                    return
                start = next_start
            log.warning("sage intacct: stopped after %d pages", MAX_PAGES)

    async def normalize(self, raw: RawEvent) -> NormalizedSignal:
        return NormalizedSignal(**raw.model_dump())

    async def healthcheck(self, conn: ConnView) -> Health:
        ok = bool(conn.tokens.get("client_id") and conn.tokens.get("client_secret"))
        return Health(ok=ok, detail="credentials present" if ok else "no client credentials")
