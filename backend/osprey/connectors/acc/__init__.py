"""Autodesk Construction Cloud (ACC) Issues connector (SPEC §6, Tier 2).

Reads a project's issues through Autodesk Platform Services: OAuth 2.0
authorization code with PKCE (the desktop app is a public client), read-only
``data:read``, and the Issues API::

    GET https://developer.api.autodesk.com/construction/issues/v1/projects/{projectId}/issues
        ?limit=1..100&offset=N  ->  {"pagination": {...}, "results": [issue, ...]}

The connection's ``account_ref`` is the ACC project id. Data Management APIs spell
that id with a ``b.`` prefix and the Issues API without one, so either is accepted.

**Webhooks, only when an admin opts in.** Autodesk requires ``data:write`` to register
a webhook, and Osprey is read-only by default, so ``data:write`` is an *optional*
scope with its reason shown before it is granted. With it, the hourly renewal job
registers a signing secret (``POST /webhooks/v1/tokens``) and hooks for
``issue.created-1.0`` and ``issue.updated-1.0`` scoped to the project. Callbacks are
verified against ``x-adsk-signature`` (``sha1hash=`` + HMAC-SHA1 of the raw body) and
then trigger a poll of the project. The callback body is not parsed: its field
layout is not documented anywhere Osprey could verify, and a poll dedupes safely.
Without the opt-in, nothing changes: the connector polls every cycle as before.

What this deliberately does not do:

* **Webhook payload parsing** (above), or webhooks without the explicit opt-in.
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

import hashlib
import hmac
import logging
import secrets
from collections.abc import AsyncIterator, Mapping
from datetime import datetime

import httpx

from ...config import settings
from ...models import SourceKind, utcnow
from ...normalize import clean_text
from ..base import Connection as ConnView
from ..base import (
    Connector,
    Health,
    NormalizedSignal,
    RawEvent,
    SubscriptionState,
    registry,
)
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

#: The opt-in scope that lets Osprey register webhooks, and why it would be granted.
WEBHOOK_SCOPE = "data:write"
WEBHOOK_SCOPE_REASON = (
    "Lets Osprey register and renew ACC issue webhooks, so new and updated issues "
    "arrive within seconds instead of at the next poll. Autodesk requires data:write "
    "to create a webhook; Osprey uses it for nothing else and never edits project data. "
    "The person connecting must be a Project Admin."
)
WEBHOOK_SYSTEM = "autodesk.construction.issues"
WEBHOOK_EVENTS = ("issue.created-1.0", "issue.updated-1.0")
SIGNATURE_HEADER = "x-adsk-signature"


def sign_body(raw: bytes, secret: str) -> str:
    """The ``x-adsk-signature`` value APS sends: ``sha1hash=`` + hex HMAC-SHA1 of the body."""
    return "sha1hash=" + hmac.new(secret.encode(), raw, hashlib.sha1).hexdigest()


def _hook_id(location: str | None) -> str | None:
    """The hook id from a create response's ``Location`` (the body is empty)."""
    if not location:
        return None
    tail = location.rstrip("/").rsplit("/hooks/", 1)
    return tail[1].split("?", 1)[0] if len(tail) == 2 and tail[1] else None


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
    optional_scopes = {WEBHOOK_SCOPE: WEBHOOK_SCOPE_REASON}
    # Callbacks carry no issue data Osprey parses; they trigger a poll through the
    # lifecycle path, so the forward-to path stays off.
    supports_webhooks = False
    supports_subscriptions = True
    webhook_auth = "signature"

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

    # -- Webhooks (opt-in) --------------------------------------------------- #
    def verify_webhook_signature(self, raw: bytes, headers: Mapping[str, str], secret: str) -> bool:
        presented = headers.get(SIGNATURE_HEADER) or headers.get(SIGNATURE_HEADER.title()) or ""
        return bool(presented) and hmac.compare_digest(sign_body(raw, secret), presented)

    def lifecycle_events(self, payload: dict) -> list[str]:
        # Reached only after the signature check. Any verified callback means an
        # issue in the project changed; poll rather than parse an undocumented body.
        return ["poll"] if isinstance(payload, dict) and payload else []

    async def ensure_subscription(
        self, conn: ConnView, notify_url: str, lifecycle_url: str = ""
    ) -> SubscriptionState | None:
        """Register the signing secret and the project's issue hooks, or confirm them.

        Does nothing unless the admin opted into ``data:write`` for this connection.
        Idempotent: known hooks are checked and only missing ones are recreated, and a
        ``409`` (hook already exists) is resolved by finding it rather than failing.
        """
        project = project_id(conn.account_ref)
        if WEBHOOK_SCOPE not in conn.scopes or not project or not notify_url:
            return None

        headers = {"Authorization": f"Bearer {conn.tokens.get('access_token', '')}"}
        known = [h for h in str(conn.tokens.get("subscription_id") or "").split(",") if h]
        secret = conn.tokens.get("webhook_secret") or ""
        base = "/webhooks/v1"

        async with connector_client(
            SOURCE_TYPE, base_url=settings.acc_base_url, headers=headers, timeout=30
        ) as client:
            if not secret:
                # APS signs with a token of 32-64 characters; hex keeps it alphanumeric.
                secret = secrets.token_hex(24)
                created = await client.post(f"{base}/tokens", json={"token": secret})
                if created.status_code == 400:
                    # One secret per app and user already exists: replace it with ours.
                    replaced = await client.put(f"{base}/tokens/@me", json={"token": secret})
                    replaced.raise_for_status()
                else:
                    created.raise_for_status()

            hook_ids: list[str] = []
            for index, event in enumerate(WEBHOOK_EVENTS):
                path = f"{base}/systems/{WEBHOOK_SYSTEM}/events/{event}/hooks"
                existing = known[index] if index < len(known) else ""
                if existing:
                    check = await client.get(f"{path}/{existing}")
                    if check.status_code == 200:
                        hook_ids.append(existing)
                        continue
                    if check.status_code != 404:
                        check.raise_for_status()
                hook_ids.append(await self._create_hook(client, path, project, notify_url, conn.id))

        return SubscriptionState(subscription_id=",".join(hook_ids), client_state=secret)

    async def _create_hook(
        self, client: httpx.AsyncClient, path: str, project: str, notify_url: str, connection: str
    ) -> str:
        resp = await client.post(
            path,
            json={
                "callbackUrl": notify_url,
                "scope": {"project": project},
                "hookAttribute": {"osprey_connection": connection},
                "autoReactivateHook": True,
            },
        )
        if resp.status_code == 409:
            # Already registered (same callback, scope and event): find and reuse it.
            listed = await client.get(path, params={"scopeName": "project", "scopeValue": project})
            listed.raise_for_status()
            if listed.status_code != 204:
                for hook in (listed.json() or {}).get("data") or []:
                    if hook.get("callbackUrl") == notify_url and hook.get("hookId"):
                        return str(hook["hookId"])
            raise RuntimeError(f"ACC reports the hook exists but it was not found at {path}")
        resp.raise_for_status()
        hook_id = _hook_id(resp.headers.get("Location"))
        if not hook_id:
            raise RuntimeError("ACC created a hook but returned no hook id in Location")
        return hook_id

    async def healthcheck(self, conn: ConnView) -> Health:
        if not project_id(conn.account_ref):
            return Health(ok=False, detail="no ACC project id on the connection")
        ok = bool(conn.tokens.get("access_token"))
        return Health(ok=ok, detail="token present" if ok else "not connected")
