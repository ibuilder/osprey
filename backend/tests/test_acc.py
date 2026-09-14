"""Autodesk Construction Cloud issues: mapping, pagination and failure handling.

Fixtures follow the shape in Autodesk's published Issues API reference. Nothing
here talks to a live ACC project.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
import respx

from osprey.config import settings
from osprey.connectors import acc
from osprey.connectors.base import Connection
from osprey.models import SourceKind

ISSUE = {
    "id": "5c8f3a2e-0d1b-4c6e-9f7a-1b2c3d4e5f60",
    "containerId": "0f9e8d7c-6b5a-4938-2716-0a1b2c3d4e5f",
    "displayId": 42,
    "title": "Fire damper missing at grid D-7",
    "description": "Level 3 corridor. Blocks ceiling close-in; inspector on site Thursday.",
    "status": "open",
    "issueTypeId": "type-1",
    "issueSubtypeId": "subtype-9",
    "dueDate": "2026-09-18",
    "assignedTo": "AUTODESK-USER-123",
    "assignedToType": "user",
    "createdBy": "AUTODESK-USER-456",
    "createdAt": "2026-09-10T14:02:11.000Z",
    "updatedAt": "2026-09-12T09:30:00.000Z",
}

PROJECT = "b.0f9e8d7c-6b5a-4938-2716-0a1b2c3d4e5f"
ISSUES_URL = (
    "https://developer.api.autodesk.com/construction/issues/v1/projects/"
    "0f9e8d7c-6b5a-4938-2716-0a1b2c3d4e5f/issues"
)


def _conn(**tokens: str) -> Connection:
    return Connection(
        id="c1",
        source_type="acc",
        account_ref=PROJECT,
        tokens=tokens or {"access_token": "tok"},
    )


def _page(issues: list[dict], *, offset: int, total: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "pagination": {"limit": 100, "offset": offset, "totalResults": total},
            "results": issues,
        },
    )


def test_normalize_maps_the_fields_it_has_and_invents_none():
    event = acc.normalize_acc_issue(ISSUE, project="proj")

    assert event.external_id == "acc:issue:5c8f3a2e-0d1b-4c6e-9f7a-1b2c3d4e5f60"
    assert event.source_kind == SourceKind.observation
    assert event.title == "Issue #42: Fire damper missing at grid D-7"
    assert event.due_at == datetime(2026, 9, 18)
    assert "Blocks ceiling close-in" in event.body
    # assignedTo is an Autodesk user id, not a person's name, and the API gives no
    # issue URL; neither is dressed up as something it is not.
    assert event.participants == []
    assert event.url is None
    assert event.raw["assigned_to"] == "AUTODESK-USER-123"


def test_the_data_management_project_prefix_is_accepted():
    assert acc.project_id("b.abc") == "abc"
    assert acc.project_id("abc") == "abc"


async def test_poll_pages_until_total_results(monkeypatch):
    monkeypatch.setattr(acc, "PAGE_SIZE", 2)
    pages = [
        _page([{**ISSUE, "id": "1"}, {**ISSUE, "id": "2"}], offset=0, total=3),
        _page([{**ISSUE, "id": "3"}], offset=2, total=3),
    ]
    with respx.mock() as mock:
        route = mock.get(ISSUES_URL).mock(side_effect=pages)
        events = [e async for e in acc.AccConnector().poll(_conn(), since=None)]

    assert [e.external_id for e in events] == ["acc:issue:1", "acc:issue:2", "acc:issue:3"]
    assert route.call_count == 2
    assert route.calls[0].request.headers["authorization"] == "Bearer tok"
    assert "offset=2" in str(route.calls[1].request.url)


async def test_drafts_and_closed_issues_are_not_live_work():
    issues = [
        {**ISSUE, "id": "open-1", "status": "open"},
        {**ISSUE, "id": "draft-1", "status": "draft"},
        {**ISSUE, "id": "closed-1", "status": "closed"},
        {**ISSUE, "id": "review-1", "status": "in_review"},
    ]
    with respx.mock() as mock:
        mock.get(ISSUES_URL).mock(return_value=_page(issues, offset=0, total=4))
        events = [e async for e in acc.AccConnector().poll(_conn(), since=None)]

    assert [e.external_id for e in events] == ["acc:issue:open-1", "acc:issue:review-1"]


async def test_an_unauthorized_token_raises_rather_than_reading_as_empty():
    with respx.mock() as mock:
        mock.get(ISSUES_URL).respond(401, json={"developerMessage": "token expired"})
        with pytest.raises(httpx.HTTPStatusError):
            async for _ in acc.AccConnector().poll(_conn(), since=None):
                pass


async def test_a_connection_without_a_project_is_refused():
    conn = Connection(id="c1", source_type="acc", account_ref="", tokens={"access_token": "t"})
    with pytest.raises(ValueError, match="project id"):
        async for _ in acc.AccConnector().poll(conn, since=None):
            pass
    assert (await acc.AccConnector().healthcheck(conn)).ok is False


def test_oauth_uses_aps_v2_with_pkce_and_the_configured_app(monkeypatch):
    monkeypatch.setattr(settings, "acc_client_id", "aps-client")
    connector = acc.AccConnector()
    spec = connector.oauth_spec()

    assert (
        spec.authorize_endpoint == "https://developer.api.autodesk.com/authentication/v2/authorize"
    )
    assert spec.token_endpoint == "https://developer.api.autodesk.com/authentication/v2/token"
    assert spec.use_pkce is True
    assert spec.scopes == ["data:read"]
    assert connector.client_credentials()[0] == "aps-client"
