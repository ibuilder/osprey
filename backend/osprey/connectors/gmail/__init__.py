"""Gmail / Google Workspace connector (SPEC §6, Tier 1).

OAuth2 (read-only ``gmail.readonly``). Real-time via Pub/Sub push + History API in
production; incremental History poll here. ``normalize_gmail_message`` is a pure
function tested against a fixture, so mapping is verifiable without a live account.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx

from ...config import settings
from ...models import SourceKind, utcnow
from ...normalize import clean_text
from ..base import Connection as ConnView
from ..base import Connector, Health, NormalizedSignal, RawEvent, registry

API = "https://gmail.googleapis.com/gmail/v1"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode_part(part: dict) -> str:
    body = part.get("body", {})
    data = body.get("data")
    if data:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
    for sub in part.get("parts", []) or []:
        if sub.get("mimeType") == "text/plain":
            return _decode_part(sub)
    for sub in part.get("parts", []) or []:
        text = _decode_part(sub)
        if text:
            return text
    return ""


def normalize_gmail_message(msg: dict) -> RawEvent:
    """Map a Gmail ``users.messages.get`` resource (full format) to a RawEvent."""
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    subject = _header(headers, "Subject") or "(no subject)"
    frm = _header(headers, "From")
    to = _header(headers, "To")
    cc = _header(headers, "Cc")
    participants = [p.strip() for p in f"{frm},{to},{cc}".split(",") if "@" in p]
    body = clean_text(_decode_part(payload) or msg.get("snippet", ""))
    ts = msg.get("internalDate")
    occurred = datetime.fromtimestamp(int(ts) / 1000, tz=UTC) if ts else utcnow()
    return RawEvent(
        external_id=msg["id"],
        source_kind=SourceKind.email,
        thread_key=msg.get("threadId"),
        title=subject,
        body=body,
        participants=participants,
        url=f"https://mail.google.com/mail/u/0/#inbox/{msg['id']}",
        raw={"labelIds": msg.get("labelIds", [])},
        occurred_at=occurred,
    )


@registry.register
class GmailConnector(Connector):
    source_type = "gmail"
    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    supports_webhooks = True

    def oauth_spec(self):
        from ...security.oauth import OAuthSpec

        return OAuthSpec(
            authorize_endpoint=GOOGLE_AUTH,
            token_endpoint=GOOGLE_TOKEN,
            scopes=self.scopes,
            use_pkce=True,
            extra_authorize_params={"access_type": "offline", "prompt": "consent"},
        )

    def client_credentials(self) -> tuple[str, str]:
        return settings.google_client_id, settings.google_client_secret

    async def _access_token(self, conn: ConnView) -> str:
        refresh = conn.tokens.get("refresh_token")
        if not refresh:
            return conn.tokens.get("access_token", "")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(GOOGLE_TOKEN, data=data)
            resp.raise_for_status()
            return resp.json()["access_token"]

    async def poll(self, conn: ConnView, since: datetime | None) -> AsyncIterator[RawEvent]:
        token = await self._access_token(conn)
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=60, headers=headers) as client:
            listing = await client.get(
                f"{API}/users/me/messages", params={"q": "newer_than:7d", "maxResults": 50}
            )
            listing.raise_for_status()
            for ref in listing.json().get("messages", []):
                full = await client.get(
                    f"{API}/users/me/messages/{ref['id']}", params={"format": "full"}
                )
                full.raise_for_status()
                yield normalize_gmail_message(full.json())

    async def normalize(self, raw: RawEvent) -> NormalizedSignal:
        return NormalizedSignal(**raw.model_dump())

    async def healthcheck(self, conn: ConnView) -> Health:
        try:
            await self._access_token(conn)
            return Health(ok=True, detail="google token acquired")
        except Exception as exc:  # noqa: BLE001
            return Health(ok=False, detail=str(exc))
