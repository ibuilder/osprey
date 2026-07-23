"""Microsoft 365 / Outlook connector via Microsoft Graph (SPEC §6, Tier 1).

Real-time: Graph change notifications (webhooks) + delta queries. Least-privilege,
read-only scopes (``Mail.Read``, ``Calendars.Read``). Network calls are isolated in
poll()/subscribe(); ``normalize`` is a pure function tested against a fixture Graph
message, so the connector's mapping logic is verifiable without a live tenant.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ...config import settings
from ...models import SourceKind, utcnow
from ...normalize import clean_text
from ..base import Connection as ConnView
from ..base import Connector, Health, NormalizedSignal, RawEvent, registry

GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


def normalize_graph_message(msg: dict) -> RawEvent:
    """Map a Graph ``message`` resource to a RawEvent (pure)."""
    subject = msg.get("subject") or "(no subject)"
    body = msg.get("body", {}) or {}
    body_text = clean_text(body.get("content", "") or msg.get("bodyPreview", ""))
    frm = ((msg.get("from") or {}).get("emailAddress") or {}).get("address")
    recips = [
        (r.get("emailAddress") or {}).get("address")
        for r in (msg.get("toRecipients", []) + msg.get("ccRecipients", []))
    ]
    participants = [a for a in ([frm] + recips) if a]
    occurred = _parse_iso(msg.get("receivedDateTime")) or utcnow()
    return RawEvent(
        external_id=msg["id"],
        source_kind=SourceKind.email,
        thread_key=msg.get("conversationId"),
        title=subject,
        body=body_text,
        participants=participants,
        url=msg.get("webLink"),
        raw={"internetMessageId": msg.get("internetMessageId")},
        occurred_at=occurred,
    )


def _parse_iso(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None


@registry.register
class OutlookConnector(Connector):
    source_type = "outlook"
    scopes = ["Mail.Read", "Calendars.Read", "offline_access"]
    supports_webhooks = True

    def oauth_spec(self):
        from ...security.oauth import OAuthSpec

        tenant = settings.msgraph_tenant_id or "common"
        return OAuthSpec(
            authorize_endpoint=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
            token_endpoint=TOKEN_URL.format(tenant=tenant),
            scopes=self.scopes,
            use_pkce=True,
            extra_authorize_params={"prompt": "select_account"},
        )

    def client_credentials(self) -> tuple[str, str]:
        return settings.msgraph_client_id, settings.msgraph_client_secret

    async def account_ref_from_tokens(self, tokens: dict) -> str:
        token = tokens.get("access_token")
        if not token:
            return ""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{GRAPH}/me", headers={"Authorization": f"Bearer {token}"}
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("userPrincipalName") or data.get("mail") or ""
        except Exception:  # noqa: BLE001
            return ""

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=20), reraise=True)
    async def _access_token(self, conn: ConnView) -> str:
        # Prefer the connection's own refresh token; fall back to app credentials.
        refresh = conn.tokens.get("refresh_token")
        data = {
            "client_id": settings.msgraph_client_id,
            "client_secret": settings.msgraph_client_secret,
            "scope": " ".join(self.scopes),
        }
        if refresh:
            data |= {"grant_type": "refresh_token", "refresh_token": refresh}
        else:
            data |= {"grant_type": "client_credentials", "scope": "https://graph.microsoft.com/.default"}
        url = TOKEN_URL.format(tenant=settings.msgraph_tenant_id or "common")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, data=data)
            resp.raise_for_status()
            return resp.json()["access_token"]

    async def poll(self, conn: ConnView, since: datetime | None) -> AsyncIterator[RawEvent]:
        token = await self._access_token(conn)
        headers = {"Authorization": f"Bearer {token}"}
        # Use the stored delta link if present, else start a delta enumeration.
        url = conn.cursor or f"{GRAPH}/me/mailFolders/inbox/messages/delta?$select=subject,from,toRecipients,ccRecipients,body,bodyPreview,conversationId,receivedDateTime,webLink,internetMessageId"
        async with httpx.AsyncClient(timeout=60, headers=headers) as client:
            while url:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                for msg in data.get("value", []):
                    if "@removed" in msg:
                        continue
                    yield normalize_graph_message(msg)
                url = data.get("@odata.nextLink")
                # @odata.deltaLink terminates the page; caller persists it as cursor.

    async def handle_webhook(self, payload: dict) -> AsyncIterator[RawEvent]:
        # Graph change notifications carry resource ids; a production impl fetches
        # each changed message. Here we accept already-expanded resourceData.
        for note in payload.get("value", []):
            data = note.get("resourceData")
            if data and data.get("id"):
                yield normalize_graph_message(data)

    async def normalize(self, raw: RawEvent) -> NormalizedSignal:
        return NormalizedSignal(**raw.model_dump())

    async def healthcheck(self, conn: ConnView) -> Health:
        try:
            await self._access_token(conn)
            return Health(ok=True, detail="graph token acquired")
        except Exception as exc:  # noqa: BLE001
            return Health(ok=False, detail=str(exc))

    supports_subscriptions = True

    async def ensure_subscription(self, conn: ConnView, notify_url: str) -> str | None:
        """Create/renew a Graph change-notification subscription (max ~3 days).

        If the connection already holds a subscription id in its tokens, PATCH to
        extend it; otherwise POST a new one. Renew well before ``expirationDateTime``.
        """
        from datetime import timedelta

        token = await self._access_token(conn)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        expiry = (utcnow() + timedelta(days=2, hours=23)).isoformat().replace("+00:00", "Z")
        sub_id = conn.tokens.get("subscription_id")
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            if sub_id:
                resp = await client.patch(
                    f"{GRAPH}/subscriptions/{sub_id}", json={"expirationDateTime": expiry}
                )
                if resp.status_code < 300:
                    return sub_id
            resp = await client.post(
                f"{GRAPH}/subscriptions",
                json={
                    "changeType": "created,updated",
                    "notificationUrl": notify_url,
                    "resource": "me/mailFolders('inbox')/messages",
                    "expirationDateTime": expiry,
                },
            )
            resp.raise_for_status()
            return resp.json().get("id")
