"""The SDK template is held to the connector contract on every backend run.

The template is what an outside developer copies. If a core change breaks it, the
break should land here, not in someone else's plugin. CI also installs the template
as a real package and runs its own suite (see the backend job), which is what
proves entry-point discovery end to end; this test keeps the contract check fast
and local.
"""

from __future__ import annotations

import importlib
import json
import sys
import tomllib
from pathlib import Path

import pytest

from osprey.connectors import PLUGIN_GROUP
from osprey.connectors.base import registry
from osprey.connectors.contract import assert_connector_contract

TEMPLATE = Path(__file__).resolve().parents[2] / "connectors-sdk" / "template"
MODULE = "osprey_connector_example"


def _fixture(name: str) -> dict:
    return json.loads((TEMPLATE / "tests" / "fixtures" / name).read_text(encoding="utf-8"))


@pytest.fixture
def template_module(monkeypatch):
    monkeypatch.syspath_prepend(str(TEMPLATE))
    already = sys.modules.pop(MODULE, None)
    if already is not None:  # pragma: no cover - only when the template is installed
        registry.unregister(already.SOURCE_TYPE)
    module = importlib.import_module(MODULE)
    try:
        yield module
    finally:
        registry.unregister(module.SOURCE_TYPE)
        sys.modules.pop(MODULE, None)


async def test_template_connector_honours_the_contract(template_module):
    await assert_connector_contract(
        template_module.ExampleTrackerConnector,
        webhook_payloads=[_fixture("webhook.json")],
        raw_events=[template_module.normalize_issue(_fixture("issue.json"))],
    )


def test_template_declares_its_entry_point_in_the_group_osprey_loads():
    project = tomllib.loads((TEMPLATE / "pyproject.toml").read_text(encoding="utf-8"))
    entry_points = project["project"]["entry-points"][PLUGIN_GROUP]

    assert entry_points == {"exampletracker": MODULE}
