"""Universal File-Drop / IMAP / Forward-To fallback connector (SPEC §6).

Guarantees Osprey has *something* for every source on day one:
  * Forward-To  — a monitored address / API endpoint receives an RFC822 email.
  * IMAP        — poll any mailbox without a modern API.
  * CSV / file  — drop an Argus/Sage export in a watched folder; each row -> Signal.

Parsing is exposed as pure functions so the API endpoint and tests can drive it
directly without external services.
"""

from __future__ import annotations

import contextlib
import csv
import io
from collections.abc import AsyncIterator
from datetime import datetime
from email import message_from_bytes, message_from_string
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime

from ...models import SourceKind, utcnow
from ...normalize import clean_text
from ..base import Connection as ConnView
from ..base import Connector, Health, NormalizedSignal, RawEvent, registry


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return str(part.get_payload())
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _body_from_email(msg: Message) -> str:
    if msg.is_multipart():
        # Prefer text/plain; fall back to the first text/html part.
        plain: Message | None = None
        html: Message | None = None
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and plain is None:
                plain = part
            elif ctype == "text/html" and html is None:
                html = part
        chosen = plain or html
        return _decode_part(chosen) if chosen is not None else ""
    return _decode_part(msg)


def parse_email(raw: str | bytes, *, external_id: str | None = None) -> RawEvent:
    msg = message_from_bytes(raw) if isinstance(raw, bytes) else message_from_string(raw)
    subject = str(msg.get("Subject", "")).strip()
    message_id = str(msg.get("Message-ID", "")).strip()
    references = str(msg.get("References", "")) or str(msg.get("In-Reply-To", ""))
    thread_key = (references.split()[0].strip() if references.strip() else message_id) or None

    froms = getaddresses(msg.get_all("From", []))
    tos = getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []))
    participants = [addr for _, addr in (froms + tos) if addr]

    occurred = utcnow()
    date_hdr = msg.get("Date")
    if date_hdr:
        with contextlib.suppress(TypeError, ValueError):
            occurred = parsedate_to_datetime(date_hdr)

    body = clean_text(_body_from_email(msg))
    ext = external_id or message_id or f"filedrop:{hash((subject, body)) & 0xFFFFFFFF:08x}"
    return RawEvent(
        external_id=ext,
        source_kind=SourceKind.email,
        thread_key=thread_key,
        title=subject or "(no subject)",
        body=body,
        participants=participants,
        url=None,
        raw={"message_id": message_id, "headers": {"from": [a for _, a in froms]}},
        occurred_at=occurred,
    )


def parse_csv(text: str, *, source_kind: SourceKind = SourceKind.general) -> list[RawEvent]:
    """Each CSV row becomes a RawEvent. Recognizes common column names."""
    events: list[RawEvent] = []
    reader = csv.DictReader(io.StringIO(text))
    for i, row in enumerate(reader):
        low = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
        ext = low.get("id") or low.get("number") or low.get("ref") or f"row-{i+1}"
        title = low.get("title") or low.get("subject") or low.get("description") or ext
        due = _parse_dt(low.get("due") or low.get("due_date") or low.get("deadline"))
        amount = _parse_amount(low.get("amount") or low.get("value") or low.get("cost"))
        body = "; ".join(f"{k}={v}" for k, v in low.items() if v)
        events.append(
            RawEvent(
                external_id=str(ext),
                source_kind=source_kind,
                title=title,
                body=clean_text(body, drop_quoted=False),
                due_at=due,
                amount=amount,
                raw=dict(row),
            )
        )
    return events


def _parse_dt(val: str | None) -> datetime | None:
    if not val:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(val, fmt)
        except ValueError:
            continue
    return None


def _parse_amount(val: str | None) -> float | None:
    if not val:
        return None
    cleaned = val.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


@registry.register
class FileDropConnector(Connector):
    source_type = "filedrop"
    scopes: list[str] = []
    supports_webhooks = True

    async def poll(self, conn: ConnView, since: datetime | None) -> AsyncIterator[RawEvent]:
        # Push-based (Forward-To / API upload); nothing to poll here. IMAP polling
        # would live here in an IMAP-configured deployment.
        return
        yield  # pragma: no cover

    async def handle_webhook(self, payload: dict) -> AsyncIterator[RawEvent]:
        kind = payload.get("kind", "email")
        if kind == "email":
            yield parse_email(payload["raw"], external_id=payload.get("external_id"))
        elif kind == "csv":
            for ev in parse_csv(payload["raw"], source_kind=SourceKind(payload.get("source_kind", "general"))):
                yield ev
        else:
            yield RawEvent(
                external_id=payload.get("external_id", f"drop-{hash(str(payload)) & 0xFFFFFFFF:08x}"),
                source_kind=SourceKind(payload.get("source_kind", "general")),
                title=payload.get("title", ""),
                body=clean_text(payload.get("body", ""), drop_quoted=False),
                raw=payload,
            )

    async def normalize(self, raw: RawEvent) -> NormalizedSignal:
        return NormalizedSignal(**raw.model_dump())

    async def healthcheck(self, conn: ConnView) -> Health:
        return Health(ok=True, detail="file-drop endpoint accepts forwarded email/CSV")
