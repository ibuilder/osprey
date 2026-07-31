"""Integration tests for connector poll() loops via mocked HTTP (respx).

Unlike the fixture-based unit tests (which cover only normalize), these exercise the
*actual* network code paths — token acquisition, Graph delta pagination, Gmail
list+get, Procore resource iteration — end-to-end into ingested Signals, without a
live account. They are the "prove the loop works" layer below real sandbox testing.
"""

from __future__ import annotations

import re

import httpx
import pytest
import respx

from osprey.config import settings
from osprey.connectors.base import Connection as ConnView
from osprey.connectors.gmail import GmailConnector
from osprey.connectors.outlook import OutlookConnector
from osprey.connectors.procore import ProcoreConnector
from osprey.engine.ingest import ingest_events
from osprey.models import Connection, ConnectionStatus, Org, Project


async def _project_and_conn(session, source_type: str):
    org = Org(name="Integ Co")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name="Tower B")
    session.add(project)
    await session.flush()
    conn = Connection(
        org_id=org.id,
        project_id=project.id,
        source_type=source_type,
        account_ref="acct",
        status=ConnectionStatus.active,
    )
    session.add(conn)
    await session.flush()
    return project, conn


@respx.mock
async def test_outlook_poll_delta_pagination(session):
    # Token endpoint.
    respx.post(re.compile(r"https://login\.microsoftonline\.com/.*/oauth2/v2\.0/token")).mock(
        return_value=httpx.Response(200, json={"access_token": "graph-token"})
    )

    page2_url = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$skiptoken=P2"

    def graph_router(request: httpx.Request) -> httpx.Response:
        def msg(i: str, subj: str):
            return {
                "id": i,
                "subject": subj,
                "conversationId": f"c-{i}",
                "from": {"emailAddress": {"address": "pm@gc.com"}},
                "toRecipients": [{"emailAddress": {"address": "sub@x.com"}}],
                "ccRecipients": [],
                "body": {"contentType": "text", "content": f"body {i}"},
                "receivedDateTime": "2026-07-22T09:00:00Z",
                "webLink": f"https://outlook/{i}",
            }

        if "skiptoken=P2" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "value": [msg("m3", "RFI followup")],
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta?$deltatoken=D",
                },
            )
        return httpx.Response(
            200,
            json={
                "value": [msg("m1", "Notice of delay"), msg("m2", "Pay app 07")],
                "@odata.nextLink": page2_url,
            },
        )

    respx.get(re.compile(r"https://graph\.microsoft\.com/.*")).mock(side_effect=graph_router)

    project, conn = await _project_and_conn(session, "outlook")
    connector = OutlookConnector()
    view = ConnView(id=conn.id, source_type="outlook", tokens={"refresh_token": "r"})

    events = [ev async for ev in connector.poll(view, None)]
    assert [e.external_id for e in events] == ["m1", "m2", "m3"]  # both pages followed

    created = await ingest_events(session, connector, conn, events)
    assert len(created) == 3


@respx.mock
async def test_gmail_poll_list_then_get(session):
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "g-token"})
    )
    respx.get(re.compile(r"https://gmail\.googleapis\.com/.*/messages\?")).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "a1"}, {"id": "a2"}]})
    )

    def message(i: str):
        return httpx.Response(
            200,
            json={
                "id": i,
                "threadId": f"t-{i}",
                "snippet": f"snippet {i}",
                "internalDate": "1753174800000",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": f"Submittal {i}"},
                        {"name": "From", "value": "arch@ae.com"},
                        {"name": "To", "value": "pm@gc.com"},
                    ],
                    "body": {"data": ""},
                },
            },
        )

    respx.get(re.compile(r"https://gmail\.googleapis\.com/.*/messages/a1")).mock(
        return_value=message("a1")
    )
    respx.get(re.compile(r"https://gmail\.googleapis\.com/.*/messages/a2")).mock(
        return_value=message("a2")
    )

    project, conn = await _project_and_conn(session, "gmail")
    connector = GmailConnector()
    view = ConnView(id=conn.id, source_type="gmail", tokens={"refresh_token": "r"})

    events = [ev async for ev in connector.poll(view, None)]
    assert {e.external_id for e in events} == {"a1", "a2"}
    assert all(e.title.startswith("Submittal") for e in events)

    created = await ingest_events(session, connector, conn, events)
    assert len(created) == 2


@respx.mock
async def test_procore_poll_iterates_resources(session):
    def resource_router(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/rfis") or "/rfis?" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 5001,
                        "number": "RFI-0500",
                        "subject": "Anchor spacing",
                        "question": {"body": "confirm spacing"},
                        "due_date": "2026-07-30",
                        "status": "open",
                        "html_url": "https://app.procore.com/rfis/5001",
                    }
                ],
            )
        if "/change_orders" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 88,
                        "number": "PCO-088",
                        "title": "Slab thickening",
                        "description": "extra work",
                        "grand_total": "45000",
                        "status": "open",
                    }
                ],
            )
        return httpx.Response(200, json=[])  # other resources empty

    respx.get(re.compile(r"https://api\.procore\.com/rest/v1\.1/.*")).mock(
        side_effect=resource_router
    )

    project, conn = await _project_and_conn(session, "procore")
    connector = ProcoreConnector()
    view = ConnView(
        id=conn.id, source_type="procore", account_ref="company-1", tokens={"access_token": "t"}
    )

    events = [ev async for ev in connector.poll(view, None)]
    ids = {e.external_id for e in events}
    assert "procore:rfis:5001" in ids
    assert "procore:change_orders:88" in ids

    created = await ingest_events(session, connector, conn, events)
    assert len(created) == 2
    assert any(e.amount == 45000 for e in events)


@respx.mock
async def test_procore_poll_raises_when_throttled(session, monkeypatch):
    """A sustained 429 must fail the poll, not quietly report an empty source.

    This is the regression that motivated the shared HTTP layer: the poller used
    to `continue` on any non-200, so being rate-limited was indistinguishable
    from a project with no RFIs — the connection stayed green and the hotlist
    silently went stale.
    """
    # One attempt, so the test does not sit through the retry backoff.
    monkeypatch.setattr(settings, "connector_max_attempts", 1)
    respx.get(re.compile(r"https://api\.procore\.com/rest/v1\.1/.*")).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "60"})
    )

    connector = ProcoreConnector()
    view = ConnView(
        id="c1", source_type="procore", account_ref="company-1", tokens={"access_token": "t"}
    )

    with pytest.raises(httpx.HTTPStatusError):
        [ev async for ev in connector.poll(view, None)]


@respx.mock
async def test_procore_poll_skips_a_forbidden_resource(session):
    """A company that doesn't licence one module must not lose the others."""

    def router(request: httpx.Request) -> httpx.Response:
        if "/invoices" in str(request.url):
            return httpx.Response(403, json={"errors": "not licensed"})
        if "/rfis" in str(request.url):
            return httpx.Response(200, json=[{"id": 1, "number": "RFI-1", "subject": "Spacing"}])
        return httpx.Response(200, json=[])

    respx.get(re.compile(r"https://api\.procore\.com/rest/v1\.1/.*")).mock(side_effect=router)

    connector = ProcoreConnector()
    view = ConnView(
        id="c1", source_type="procore", account_ref="company-1", tokens={"access_token": "t"}
    )

    events = [ev async for ev in connector.poll(view, None)]
    assert {e.external_id for e in events} == {"procore:rfis:1"}


@respx.mock
async def test_procore_poll_follows_pagination(session):
    """A full page means there is more to fetch; a short page ends the walk."""
    pages: dict[int, list[dict]] = {
        1: [{"id": i, "number": f"RFI-{i}", "subject": f"Item {i}"} for i in range(50)],
        2: [{"id": 100, "number": "RFI-100", "subject": "Last one"}],
    }

    def router(request: httpx.Request) -> httpx.Response:
        if "/rfis" not in str(request.url):
            return httpx.Response(200, json=[])
        page = int(request.url.params.get("page", 1))
        return httpx.Response(200, json=pages.get(page, []))

    respx.get(re.compile(r"https://api\.procore\.com/rest/v1\.1/.*")).mock(side_effect=router)

    connector = ProcoreConnector()
    view = ConnView(
        id="c1", source_type="procore", account_ref="company-1", tokens={"access_token": "t"}
    )

    events = [ev async for ev in connector.poll(view, None)]
    assert len(events) == 51
    assert "procore:rfis:100" in {e.external_id for e in events}
