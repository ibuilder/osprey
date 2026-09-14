"""Example Osprey connector: an issue tracker with a REST API and webhooks.

Copy this package and replace the tracker specifics with your source. The shape
is the one every Osprey connector follows:

* ``normalize_issue`` is a **pure function** from the provider's JSON to a
  ``RawEvent``. All the mapping lives here, so it is tested against recorded
  fixtures with no network and no credentials.
* ``poll`` does only I/O: page through the API with ``connector_client``, which
  paces requests and honours ``429``/``Retry-After`` for you.
* ``handle_webhook`` maps a pushed payload through the same pure function, so a
  webhook and a poll of the same issue produce the same ``external_id`` and
  Osprey ingests it once.

Nothing here edits Osprey. Installing this package is the whole integration: its
``osprey.connectors`` entry point makes Osprey import this module at startup.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

from osprey.connectors.base import (
    Connection,
    Connector,
    Health,
    NormalizedSignal,
    RawEvent,
    registry,
)
from osprey.connectors.http import connector_client
from osprey.models import SourceKind, utcnow
from osprey.normalize import clean_text

#: Registry key and webhook path (``POST /webhooks/exampletracker``). Keep it unique.
SOURCE_TYPE = "exampletracker"

API_BASE = "https://tracker.example.com/api/v1"
PAGE_SIZE = 100
#: A backstop against a pagination bug looping forever, not a real limit.
MAX_PAGES = 50

# Provider issue type -> Osprey SourceKind. Unknown types stay "general".
_KIND = {
    "rfi": SourceKind.rfi,
    "submittal": SourceKind.submittal,
    "change_order": SourceKind.change_order,
    "task": SourceKind.task,
}


def _parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _amount(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace("$", "").replace(",", ""))
    except ValueError:
        return None


def normalize_issue(issue: dict) -> RawEvent:
    """Map one tracker issue to a RawEvent. Pure: no I/O, no clock, no randomness."""
    issue_id = issue["id"]
    return RawEvent(
        # Namespaced and derived from the provider's own id, so every delivery of
        # this issue -- poll or webhook, today or after a restart -- dedupes.
        external_id=f"{SOURCE_TYPE}:{issue_id}",
        source_kind=_KIND.get(str(issue.get("type", "")), SourceKind.general),
        # Items that belong together cluster on thread_key.
        thread_key=f"{SOURCE_TYPE}:{issue.get('parent_id') or issue_id}",
        title=issue.get("title") or f"Issue {issue_id}",
        body=clean_text(issue.get("description") or "", drop_quoted=False),
        participants=[p for p in (issue.get("assignee"), issue.get("reporter")) if p],
        # Deadlines and dollar exposure drive the score; map them whenever the
        # source has them.
        due_at=_parse_dt(issue.get("due_date")),
        amount=_amount(issue.get("cost_impact")),
        url=issue.get("url"),
        raw={"status": issue.get("status")},
        occurred_at=_parse_dt(issue.get("updated_at")) or utcnow(),
    )


@registry.register
class ExampleTrackerConnector(Connector):
    source_type = SOURCE_TYPE
    # Least privilege: ask the provider for read access only. The contract test
    # fails on scopes that grant writes.
    scopes = ["issues:read"]
    supports_webhooks = True
    # Webhooks relayed to Osprey are verified with its X-Osprey-Signature HMAC.
    # Use "client_state" (and implement webhook_client_state) only for providers
    # that echo a shared secret instead of signing.
    webhook_auth = "hmac"

    async def poll(self, conn: Connection, since: datetime | None) -> AsyncIterator[RawEvent]:
        # Credentials arrive already decrypted in conn.tokens. Never log them.
        headers = {"Authorization": f"Bearer {conn.tokens.get('api_token', '')}"}
        params: dict[str, object] = {"per_page": PAGE_SIZE}
        if since is not None:
            params["updated_since"] = since.isoformat()
        async with connector_client(
            SOURCE_TYPE, base_url=API_BASE, headers=headers, timeout=30
        ) as client:
            for page in range(1, MAX_PAGES + 1):
                resp = await client.get("/issues", params={**params, "page": page})
                # Raise on 401/403/5xx: an error must surface, never read as "no data".
                resp.raise_for_status()
                issues = resp.json().get("issues", [])
                for issue in issues:
                    yield normalize_issue(issue)
                if len(issues) < PAGE_SIZE:
                    return

    async def handle_webhook(self, payload: dict) -> AsyncIterator[RawEvent]:
        # Deletions carry nothing to rank; ignore them rather than ingest a stub.
        if payload.get("event") in {"issue.created", "issue.updated"} and payload.get("issue"):
            yield normalize_issue(payload["issue"])

    async def normalize(self, raw: RawEvent) -> NormalizedSignal:
        # Source-specific cleaning beyond normalize_issue would go here. It must
        # keep raw.external_id unchanged.
        return NormalizedSignal(**raw.model_dump())

    async def healthcheck(self, conn: Connection) -> Health:
        ok = bool(conn.tokens.get("api_token"))
        return Health(ok=ok, detail="api token present" if ok else "not connected")
