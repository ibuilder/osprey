"""AI provider: Claude sift tool schema + ResilientProvider fallback behavior."""

from __future__ import annotations

from osprey.ai.base import LLMProvider, SiftFinding, SiftInput
from osprey.ai.deterministic import DeterministicProvider
from osprey.ai.provider import ResilientProvider


def test_claude_sift_tool_schema_wellformed():
    from osprey.ai import claude

    tool = claude._SIFT_TOOL
    assert tool["name"] == "report_findings"
    props = tool["input_schema"]["properties"]["findings"]["items"]["properties"]
    assert {"title", "body", "matched_signal_ids", "confidence"} <= set(props)
    assert "report_findings" in claude._SIFT_SYSTEM or "sift" in claude._SIFT_SYSTEM.lower()
    # ClaudeProvider exposes an overridden sift.
    assert "sift" in claude.ClaudeProvider.__dict__


class _FailingPrimary(LLMProvider):
    name = "failing"

    async def extract(self, payload):
        raise RuntimeError("boom")

    async def sift(self, payload):
        raise RuntimeError("boom")


async def test_resilient_sift_falls_back_to_deterministic():
    resilient = ResilientProvider(_FailingPrimary())
    payload = SiftInput(
        instruction="liquidated damages",
        signals=[
            {"id": "1", "title": "Liquidated damages clause", "body": "damages of $5000/day"},
            {"id": "2", "title": "Weekly coordination", "body": "site walk"},
        ],
    )
    findings = await resilient.sift(payload)
    # Deterministic keyword sift still returns a finding citing signal 1.
    assert findings and "1" in findings[0].matched_signal_ids


async def test_deterministic_sift_no_match_is_empty():
    findings = await DeterministicProvider().sift(
        SiftInput(instruction="zzz nonexistent", signals=[{"id": "1", "title": "hi", "body": "there"}])
    )
    assert findings == []


def test_sift_finding_model():
    f = SiftFinding(title="t", matched_signal_ids=["a"], confidence=0.9)
    assert f.body == ""
    assert f.confidence == 0.9


def test_openai_function_schemas_wellformed():
    from osprey.ai import openai_provider as op

    assert op._EXTRACT_FN["function"]["name"] == "record_item"
    assert op._SIFT_FN["function"]["name"] == "report_findings"
    sift_props = op._SIFT_FN["function"]["parameters"]["properties"]["findings"]["items"]["properties"]
    assert {"title", "matched_signal_ids", "confidence"} <= set(sift_props)


async def test_provider_from_connection_openai_falls_back_offline():
    # openai SDK isn't installed in the test env -> build must degrade gracefully.
    from osprey.ai.provider import provider_from_connection

    provider = provider_from_connection("openai", api_key="sk-x", model="gpt-4o")
    findings = await provider.sift(
        SiftInput(instruction="damages", signals=[{"id": "1", "title": "damages clause", "body": "x"}])
    )
    assert findings and "1" in findings[0].matched_signal_ids   # deterministic fallback works
