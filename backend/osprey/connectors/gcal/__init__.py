"""Google Calendar connector (SPEC §6, Tier 1).

Deadlines and meetings are high-signal for urgency scoring. OAuth2 read-only
(``calendar.readonly``). ``normalize_gcal_event`` is pure and fixture-tested.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

from ...config import settings
from ...models import SourceKind, utcnow
from ...normalize import clean_text
from ..base import Connection as ConnView
from ..base import Connector, Health, NormalizedSignal, RawEvent, registry
from ..http import connector_client

API = "https://www.googleapis.com/calendar/v3"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"


def _event_start(event: dict) -> datetime | None:
    start = event.get("start", {})
    raw = start.get("dateTime") or start.get("date")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_gcal_event(event: dict) -> RawEvent:
    """Map a Google Calendar ``events`` resource to a RawEvent (pure)."""
    title = event.get("summary") or "(untitled event)"
    when = _event_start(event)
    attendees = [a.get("email") for a in event.get("attendees", []) if a.get("email")]
    return RawEvent(
        external_id=f"gcal:{event['id']}",
        source_kind=SourceKind.event,
        thread_key=event.get("recurringEventId") or event.get("id"),
        title=title,
        body=clean_text(event.get("description", "") or ""),
        participants=attendees,
        due_at=when,
        url=event.get("htmlLink"),
        raw={"location": event.get("location"), "status": event.get("status")},
        occurred_at=when or utcnow(),
    )


@registry.register
class GoogleCalendarConnector(Connector):
    source_type = "gcal"
    scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
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

    async def poll(self, conn: ConnView, since: datetime | None) -> AsyncIterator[RawEvent]:
        token = conn.tokens.get("access_token", "")
        headers = {"Authorization": f"Bearer {token}"}
        async with connector_client("gcal", timeout=60, headers=headers) as client:
            resp = await client.get(
                f"{API}/calendars/primary/events",
                params={"maxResults": 50, "singleEvents": "true", "orderBy": "startTime"},
            )
            resp.raise_for_status()
            for event in resp.json().get("items", []):
                yield normalize_gcal_event(event)

    async def normalize(self, raw: RawEvent) -> NormalizedSignal:
        return NormalizedSignal(**raw.model_dump())

    async def healthcheck(self, conn: ConnView) -> Health:
        ok = bool(conn.tokens.get("access_token"))
        return Health(ok=ok, detail="token present" if ok else "not connected")
