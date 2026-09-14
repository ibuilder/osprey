"""Autodesk Construction Cloud (ACC) Issues connector (SPEC §6, Tier 2).

Reads a project's issues through Autodesk Platform Services: OAuth 2.0
authorization code with PKCE (the desktop app is a public client), read-only
``data:read``, and the Issues API::

    GET https://developer.api.autodesk.com/construction/issues/v1/projects/{projectId}/issues
        ?limit=1..100&offset=N  ->  {"pagination": {...}, "results": [issue, ...]}

The connection's ``account_ref`` is the ACC project id. Data Management APIs spell
that id with a ``b.`` prefix and the Issues API without one, so either is accepted.

What this deliberately does not do:

* **Webhooks.** APS offers them, but they are not implemented here, so the
  connector polls and says ``supports_webhooks = False``.
* **Server-side "updated since" filtering.** The API documents ``filter[updatedAt]``
  as matching issues updated *at* a timestamp, not after one, so each poll pages
  the project and relies on stable ids for dedupe rather than on a filter whose
  semantics are unclear.
* **Names and links it does not have.** ``assignedTo`` is an Autodesk user id, not
  a name, and the API returns no web URL for an issue, so neither is invented.
  Both ids stay on the event's raw payload.

Mapping lives in the pure :func:`normalize_acc_issue`, tested against fixtures.
Verified against Autodesk's published API reference, not against a live ACC
project.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime

from ...config import settings
from ...models import SourceKind, utcnow
from ...normalize import clean_text
from ..base import Connection as ConnView
from ..base import Connector, Health, NormalizedSignal, RawEvent, registry
from ..http import connector_client

log = logging.getLogger(__name__)

SOURCE_TYPE = "acc"
AUTHORIZE = "https://developer.api.autodesk.com/authentication/v2/authorize"
TOKEN = "https://developer.api.autodesk.com/authentication/v2/token"

#: Issues API maximum page size.
PAGE_SIZE = 100
#: A backstop against a pagination bug looping forever: 100 pages is 10,000 issues.
MAX_PAGES = 100

#: Issues that are not live work: unpublished drafts and finished or voided ones.
SKIP_STATUSES = frozenset({"draft", "closed", "void"})


def project_id(account_ref: str) -> str:
    """The Issues API project id: the ``b.`` prefix Data Management uses is removed."""
    ref = account_ref.strip()
    return ref[2:] if ref.startswith("b.") else ref


def _parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return None


def normalize_acc_issue(issue: dict, *, project: str = "") -> RawEvent:
    """Map one ACC issue to a RawEvent. Pure."""
    issue_id = str(issue["id"])
    display = issue.get("displayId")
    title = issue.get("title") or "(untitled issue)"
    return RawEvent(
        external_id=f"{SOURCE_TYPE}:issue:{issue_id}",
        # ACC issues are field observations: quality, safety, punch.
        source_kind=SourceKind.observation,
        thread_key=f"{SOURCE_TYPE}:issue:{issue_id}",
        title=f"Issue #{display}: {title}" if display is not None else title,
        body=clean_text(issue.get("description") or "", drop_quoted=False),
        due_at=_parse_dt(issue.get("dueDate")),
        raw={
            "project_id": project,
            "display_id": display,
            "status": issue.get("status"),
            "issue_type_id": issue.get("issueTypeId"),
            "issue_subtype_id": issue.get("issueSubtypeId"),
            "assigned_to": issue.get("assignedTo"),
            "assigned_to_type": issue.get("assignedToType"),
            "created_by": issue.get("createdBy"),
        },
        occurred_at=_parse_dt(issue.get("updatedAt"))
        or _parse_dt(issue.get("createdAt"))
        or utcnow(),
    )


@registry.register
class AccConnector(Connector):
    source_type = SOURCE_TYPE
    scopes = ["data:read"]
    supports_webhooks = False

    def oauth_spec(self):
        from ...security.oauth import OAuthSpec

        return OAuthSpec(
            authorize_endpoint=AUTHORIZE,
            token_endpoint=TOKEN,
            scopes=self.scopes,
            use_pkce=True,
        )

    def client_credentials(self) -> tuple[str, str]:
        return settings.acc_client_id, settings.acc_client_secret

    async def poll(self, conn: ConnView, since: datetime | None) -> AsyncIterator[RawEvent]:
        project = project_id(conn.account_ref)
        if not project:
            raise ValueError("an ACC connection needs its project id as account_ref")
        headers = {"Authorization": f"Bearer {conn.tokens.get('access_token', '')}"}
        path = f"/construction/issues/v1/projects/{project}/issues"
        async with connector_client(
            SOURCE_TYPE, base_url=settings.acc_base_url, headers=headers, timeout=60
        ) as client:
            offset = 0
            for _ in range(MAX_PAGES):
                resp = await client.get(path, params={"limit": PAGE_SIZE, "offset": offset})
                # A 401/403/404 means the token or the project is wrong: surface it.
                resp.raise_for_status()
                data = resp.json()
                issues = data.get("results") or []
                for issue in issues:
                    if str(issue.get("status", "")).lower() in SKIP_STATUSES:
                        continue
                    yield normalize_acc_issue(issue, project=project)
                offset += len(issues)
                total = (data.get("pagination") or {}).get("totalResults")
                if not issues or (isinstance(total, int) and offset >= total):
                    return
            log.warning("acc project %s: stopped after %d pages", project, MAX_PAGES)

    async def normalize(self, raw: RawEvent) -> NormalizedSignal:
        return NormalizedSignal(**raw.model_dump())

    async def healthcheck(self, conn: ConnView) -> Health:
        if not project_id(conn.account_ref):
            return Health(ok=False, detail="no ACC project id on the connection")
        ok = bool(conn.tokens.get("access_token"))
        return Health(ok=ok, detail="token present" if ok else "not connected")
