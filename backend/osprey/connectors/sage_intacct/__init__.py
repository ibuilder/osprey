"""Sage Intacct: open receivables and payables through the REST API (SPEC §6, Tier 2).

Unpaid invoices and bills are the financial signals SPEC asks Sage for. This reads
both through the Sage Intacct REST API::

    POST https://api.intacct.com/ia/api/v1/oauth2/token        (client_credentials)
    GET  https://api.intacct.com/ia/api/v1/services/core/model?name=<object>&schema=true
    POST https://api.intacct.com/ia/api/v1/services/core/query
         {"object": "...", "fields": [...], "start": 1, "size": N}
      -> {"ia::result": [...], "ia::meta": {"totalCount", "start", "pageSize", "next", "previous"}}

``next`` is the ``start`` of the following page, or null on the last one. An error
comes back as an ``ia::error`` object inside ``ia::result``, which is raised rather
than read as an empty page.

**Receivables** (``accounts-receivable/invoice``) query a fixed field list, taken
from recorded production responses.

**Payables** (``accounts-payable/bill``) discover their fields at runtime. Their
field names could not be confirmed from any real traffic, so instead of guessing,
the connector asks the model service which fields this company's bill object has
and maps them by role. Asking a query for a field the object lacks fails the whole
request, so this is also what keeps a template difference in one company from
breaking the poll. A bill model without a due date or an amount due is not
rankable, so bills are then skipped with a logged reason; so are bills when the
credentials' role may not read payables. Receivables still sync either way.

**Credentials, not a desktop consent flow.** The connection holds a Sage Intacct
client id and secret (and optionally an entity id), sealed at rest like every
other token. That is the flow production integrations use against this API. Give
the client a read-only role: Osprey never writes.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable
from datetime import datetime

import httpx

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

BILL_OBJECT = "accounts-payable/bill"
#: Role -> field names to look for in the bill model, in order of preference. Only
#: roles whose field the company's model actually has are queried.
BILL_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "key": ("key", "id"),
    "number": ("billNumber", "recordId", "documentId", "referenceNumber"),
    "due": ("dueDate",),
    "amount_due": ("totalTxnAmountDue", "totalBaseAmountDue"),
    "total": ("totalTxnAmount", "totalBaseAmount"),
    "state": ("state",),
    "date": ("billDate", "postingDate", "createdDate"),
    "description": ("description",),
    "url": ("webURL",),
}
#: Without these a bill cannot be deduped or ranked.
REQUIRED_BILL_ROLES = ("key", "due", "amount_due")

PAGE_SIZE = 500
#: A backstop against a pagination bug looping forever.
MAX_PAGES = 200

#: The credentials' role may not read this object (or the company lacks the module).
_UNAVAILABLE = frozenset({403, 404})


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


def _still_owed(state: object, amount_due: object) -> bool:
    due = _num(amount_due)
    return str(state or "").lower() != "paid" and due is not None and due > 0


# --------------------------------------------------------------------------- #
# Receivables
# --------------------------------------------------------------------------- #
def is_open(invoice: dict) -> bool:
    """Still owed: not marked paid, and a positive amount due."""
    return _still_owed(invoice.get("state"), invoice.get("totalTxnAmountDue"))


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


# --------------------------------------------------------------------------- #
# Payables
# --------------------------------------------------------------------------- #
def choose_bill_fields(model_fields: Iterable[str]) -> dict[str, str] | None:
    """Map bill roles to the fields this company's model has, or None if unrankable."""
    available = set(model_fields)
    chosen: dict[str, str] = {}
    for role, candidates in BILL_FIELD_CANDIDATES.items():
        for name in candidates:
            if name in available:
                chosen[role] = name
                break
    if any(role not in chosen for role in REQUIRED_BILL_ROLES):
        return None
    return chosen


def is_open_bill(bill: dict, fields: dict[str, str]) -> bool:
    """Still to pay: not marked paid, and a positive amount due."""
    return _still_owed(bill.get(fields.get("state", "")), bill.get(fields["amount_due"]))


def normalize_ap_bill(bill: dict, fields: dict[str, str]) -> RawEvent:
    """Map one AP bill record, read through the discovered field roles. Pure."""

    def get(role: str) -> object:
        name = fields.get(role)
        return bill.get(name) if name else None

    key = str(get("key"))
    number = get("number") or key
    due_amount = _num(get("amount_due"))
    total = _num(get("total"))
    due_at = _date(get("due"))
    state = get("state") or "open"

    outstanding = f"${due_amount:,.2f}" if due_amount is not None else "an amount"
    title = f"Bill {number} to pay: {outstanding} outstanding (accounts payable)"
    if due_at is not None:
        title += f", due {due_at:%Y-%m-%d}"

    lines = [
        f"Payable {number} is {state}: {outstanding} outstanding"
        + (f" of ${total:,.2f}" if total is not None else "")
        + (f", due {due_at:%Y-%m-%d}." if due_at is not None else "."),
    ]
    if get("description"):
        lines.append(str(get("description")))

    return RawEvent(
        external_id=f"{SOURCE_TYPE}:ap-bill:{key}",
        source_kind=SourceKind.invoice,
        thread_key=f"{SOURCE_TYPE}:ap-bill:{key}",
        title=title,
        body=clean_text("\n".join(lines), drop_quoted=False),
        due_at=due_at,
        amount=due_amount,
        url=str(get("url")) if get("url") else None,
        raw={"state": state, "total": total, "fields": dict(fields)},
        occurred_at=_date(get("date")) or utcnow(),
    )


# --------------------------------------------------------------------------- #
# The connector
# --------------------------------------------------------------------------- #
async def _query(
    client: httpx.AsyncClient, headers: dict, obj: str, fields: list[str]
) -> AsyncIterator[dict]:
    """Every record of ``obj``, following ``ia::meta.next``."""
    start = 1
    for _ in range(MAX_PAGES):
        resp = await client.post(
            "/services/core/query",
            headers=headers,
            json={
                "object": obj,
                "fields": fields,
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
        for record in records:
            yield record
        next_start = (data.get("ia::meta") or {}).get("next")
        if not records or not isinstance(next_start, int):
            return
        start = next_start
    log.warning("sage intacct %s: stopped after %d pages", obj, MAX_PAGES)


async def discover_bill_fields(client: httpx.AsyncClient, headers: dict) -> dict[str, str] | None:
    """Ask the model service which bill fields exist; None if bills can't be read."""
    resp = await client.get(
        "/services/core/model",
        headers=headers,
        params={"name": BILL_OBJECT, "schema": "true"},
    )
    if resp.status_code in _UNAVAILABLE:
        log.info(
            "sage intacct: payables unavailable to these credentials (HTTP %s); skipping bills",
            resp.status_code,
        )
        return None
    resp.raise_for_status()
    result = resp.json().get("ia::result") or {}
    model_fields = [name for name in (result.get("fields") or {}) if not name.startswith("ia::")]
    chosen = choose_bill_fields(model_fields)
    if chosen is None:
        log.warning(
            "sage intacct: the bill model has no usable %s field; skipping bills",
            " / ".join(role for role in REQUIRED_BILL_ROLES),
        )
    return chosen


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

            async for invoice in _query(client, headers, INVOICE_OBJECT, INVOICE_FIELDS):
                if is_open(invoice):
                    yield normalize_ar_invoice(invoice)

            bill_fields = await discover_bill_fields(client, headers)
            if bill_fields is not None:
                async for bill in _query(
                    client, headers, BILL_OBJECT, sorted(set(bill_fields.values()))
                ):
                    if is_open_bill(bill, bill_fields):
                        yield normalize_ap_bill(bill, bill_fields)

    async def normalize(self, raw: RawEvent) -> NormalizedSignal:
        return NormalizedSignal(**raw.model_dump())

    async def healthcheck(self, conn: ConnView) -> Health:
        ok = bool(conn.tokens.get("client_id") and conn.tokens.get("client_secret"))
        return Health(ok=ok, detail="credentials present" if ok else "no client credentials")
