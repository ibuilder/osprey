"""Procore connector (SPEC §6, Tier 1).

OAuth2 + webhooks. Subscribes only to high-signal resources (RFIs, submittals,
change orders, observations, invoices) to cut noise. ``normalize_procore_resource``
is pure and tested against fixtures; a free Procore dev account + sandbox is used
for integration testing — never live production data.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import httpx

from ...config import settings
from ...models import SourceKind, utcnow
from ...normalize import clean_text
from ..base import Connection as ConnView
from ..base import Connector, Health, NormalizedSignal, RawEvent, registry

AUTH = "https://login.procore.com/oauth/authorize"
TOKEN = "https://login.procore.com/oauth/token"

# Procore resource_name -> Osprey SourceKind
_KIND = {
    "rfis": SourceKind.rfi,
    "submittals": SourceKind.submittal,
    "change_orders": SourceKind.change_order,
    "observations": SourceKind.observation,
    "invoices": SourceKind.invoice,
}


def normalize_procore_resource(resource_name: str, obj: dict, *, company_url: str | None = None) -> RawEvent:
    """Map a Procore resource payload to a RawEvent (pure)."""
    kind = _KIND.get(resource_name, SourceKind.general)
    number = obj.get("number") or obj.get("formatted_number") or obj.get("id")
    subject = obj.get("subject") or obj.get("title") or obj.get("description") or f"{resource_name} {number}"
    body = clean_text(
        obj.get("body")
        or obj.get("description")
        or (obj.get("question", {}) or {}).get("body", "")
        or "",
        drop_quoted=False,
    )
    due = obj.get("due_date") or obj.get("due_at")
    due_at = _parse(due)
    amount = _num(obj.get("grand_total") or obj.get("amount") or obj.get("total"))
    assignee = ((obj.get("assignee") or {}) or {}).get("name") or obj.get("received_from")
    participants = [p for p in [assignee, ((obj.get("created_by") or {}) or {}).get("name")] if p]
    url = obj.get("html_url") or (f"{company_url}/{resource_name}/{obj.get('id')}" if company_url else None)
    return RawEvent(
        external_id=f"procore:{resource_name}:{obj.get('id')}",
        source_kind=kind,
        thread_key=f"procore:{resource_name}:{number}",
        title=str(subject),
        body=body,
        participants=participants,
        due_at=due_at,
        amount=amount,
        url=url,
        raw={"resource_name": resource_name, "status": obj.get("status")},
        occurred_at=_parse(obj.get("updated_at")) or utcnow(),
    )


def _parse(val) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(str(val), fmt)
            except ValueError:
                continue
    return None


def _num(val) -> float | None:
    if val in (None, ""):
        return None
    try:
        return float(str(val).replace("$", "").replace(",", ""))
    except ValueError:
        return None


@registry.register
class ProcoreConnector(Connector):
    source_type = "procore"
    scopes: list[str] = []          # Procore scopes are configured on the OAuth app
    supports_webhooks = True

    def oauth_spec(self):
        from ...security.oauth import OAuthSpec

        return OAuthSpec(
            authorize_endpoint=AUTH,
            token_endpoint=TOKEN,
            scopes=self.scopes,
            use_pkce=True,
        )

    def client_credentials(self) -> tuple[str, str]:
        return settings.procore_client_id, settings.procore_client_secret

    async def poll(self, conn: ConnView, since: datetime | None) -> AsyncIterator[RawEvent]:
        token = conn.tokens.get("access_token", "")
        company_id = conn.account_ref
        headers = {"Authorization": f"Bearer {token}", "Procore-Company-Id": company_id}
        async with httpx.AsyncClient(timeout=60, headers=headers, base_url=settings.procore_base_url) as client:
            for resource in _KIND:
                resp = await client.get(f"/rest/v1.1/{resource}", params={"per_page": 50})
                if resp.status_code != 200:
                    continue
                for obj in resp.json():
                    yield normalize_procore_resource(resource, obj)

    async def handle_webhook(self, payload: dict) -> AsyncIterator[RawEvent]:
        resource = payload.get("resource_name", "")
        obj = payload.get("resource") or payload.get("metadata") or {}
        if obj:
            yield normalize_procore_resource(resource, obj)

    async def normalize(self, raw: RawEvent) -> NormalizedSignal:
        return NormalizedSignal(**raw.model_dump())

    async def healthcheck(self, conn: ConnView) -> Health:
        ok = bool(conn.tokens.get("access_token"))
        return Health(ok=ok, detail="token present" if ok else "not connected")
