"""Tests for the example connector: pure mapping, the poll loop, webhooks, and the contract.

Everything runs offline. Recorded fixtures stand in for the provider, and respx
stands in for its HTTP API. Never point connector tests at live production data.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
import respx

import osprey_connector_example as example
from osprey.connectors.base import Connection
from osprey.connectors.contract import assert_connector_contract
from osprey.models import SourceKind

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def connection(**tokens: str) -> Connection:
    return Connection(id="conn-1", source_type=example.SOURCE_TYPE, tokens=tokens)


# --------------------------------------------------------------------------- #
# The mapping: pure, so the bulk of the testing lives here
# --------------------------------------------------------------------------- #
def test_normalize_issue_maps_the_fields_scoring_depends_on():
    event = example.normalize_issue(load("issue.json"))

    assert event.external_id == "exampletracker:1042"
    assert event.source_kind == SourceKind.rfi
    assert event.title.startswith("RFI 118")
    assert event.due_at is not None and event.due_at.date().isoformat() == "2026-09-20"
    assert event.amount == 18500.0
    assert "structural@ae.example.com" in event.participants


def test_normalize_issue_tolerates_a_sparse_issue():
    event = example.normalize_issue({"id": 7})

    assert event.external_id == "exampletracker:7"
    assert event.source_kind == SourceKind.general
    assert event.title == "Issue 7"
    assert event.due_at is None and event.amount is None


# --------------------------------------------------------------------------- #
# The poll loop, against a mocked API
# --------------------------------------------------------------------------- #
async def test_poll_walks_every_page_and_sends_the_token(monkeypatch):
    monkeypatch.setattr(example, "PAGE_SIZE", 2)
    issue = load("issue.json")
    pages = [
        [{**issue, "id": 1}, {**issue, "id": 2}],
        [{**issue, "id": 3}],  # a short page ends the walk
    ]
    with respx.mock() as mock:
        route = mock.get(f"{example.API_BASE}/issues").mock(
            side_effect=[httpx.Response(200, json={"issues": page}) for page in pages]
        )
        events = [
            e
            async for e in example.ExampleTrackerConnector().poll(
                connection(api_token="tok"), since=datetime(2026, 9, 1)
            )
        ]

    assert [e.external_id for e in events] == [f"exampletracker:{i}" for i in (1, 2, 3)]
    first = route.calls[0].request
    assert first.headers["authorization"] == "Bearer tok"
    assert "updated_since=2026-09-01" in str(first.url)


async def test_poll_raises_on_an_auth_failure_instead_of_returning_nothing():
    with respx.mock() as mock:
        mock.get(f"{example.API_BASE}/issues").respond(401, json={"error": "bad token"})
        try:
            async for _ in example.ExampleTrackerConnector().poll(connection(), since=None):
                pass
        except httpx.HTTPStatusError:
            return
    raise AssertionError("a 401 must surface, not look like an empty source")


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #
async def test_webhook_and_poll_agree_on_identity():
    connector = example.ExampleTrackerConnector()
    [pushed] = [e async for e in connector.handle_webhook(load("webhook.json"))]

    assert pushed.external_id == example.normalize_issue(load("issue.json")).external_id


async def test_deletion_webhooks_are_ignored():
    connector = example.ExampleTrackerConnector()
    payload = {"event": "issue.deleted", "issue": {"id": 1042}}

    assert [e async for e in connector.handle_webhook(payload)] == []


# --------------------------------------------------------------------------- #
# The contract Osprey holds every connector to
# --------------------------------------------------------------------------- #
async def test_honours_the_connector_contract():
    await assert_connector_contract(
        example.ExampleTrackerConnector,
        webhook_payloads=[load("webhook.json")],
        raw_events=[example.normalize_issue(load("issue.json"))],
    )
