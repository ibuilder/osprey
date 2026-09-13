"""Claude and OpenAI providers, driven through fake SDK clients.

The SDKs are optional extras the test suite does not install, so these tests
substitute the client objects (or the imported module) and assert on what the
provider sends and how it reads the reply. No network, no keys.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from osprey.ai import claude, openai_provider
from osprey.ai.base import Extraction, ExtractionInput, SiftInput
from osprey.ai.deterministic import DeterministicProvider
from osprey.ai.provider import ResilientProvider, _build, provider_from_connection
from osprey.config import settings

EXTRACT_IN = ExtractionInput(
    item_title="Notice of delay",
    signals=[
        {
            "id": "s1",
            "title": "NOTICE OF DELAY",
            "body": "A written response is required within 7 days.",
        }
    ],
)
SIFT_IN = SiftInput(
    instruction="liquidated damages",
    signals=[{"id": "s1", "title": "LDs", "body": "Liquidated damages of $5,000/day"}],
)
EXTRACTION = {
    "category": "contractual_notice",
    "summary": "Owner-issued notice of delay",
    "notice_deadline": True,
    "recommended_action": "Respond in writing within 7 days",
    "confidence": 0.9,
    "citations": [{"signal_id": "s1", "quote_span": "required within 7 days"}],
}
FINDINGS = {"findings": [{"title": "LDs", "matched_signal_ids": ["s1"], "confidence": 0.8}]}


class _Create:
    """Stands in for `client.messages` / `client.chat.completions`."""

    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.response


class _FakeSdkClient:
    """Records constructor arguments, like AsyncAnthropic / AsyncOpenAI."""

    instances: list[_FakeSdkClient] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        _FakeSdkClient.instances.append(self)


@pytest.fixture
def fake_sdks(monkeypatch):
    """Make `from anthropic import AsyncAnthropic` and the OpenAI equivalent work."""
    _FakeSdkClient.instances = []
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(AsyncAnthropic=_FakeSdkClient))
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=_FakeSdkClient))
    return _FakeSdkClient


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #
def _claude_with(content: list) -> tuple[claude.ClaudeProvider, _Create]:
    provider = claude.ClaudeProvider.__new__(claude.ClaudeProvider)
    create = _Create(SimpleNamespace(content=content))
    provider._client = SimpleNamespace(messages=create)
    provider._model = "claude-test"
    return provider, create


def _tool_use(name: str, data: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=data)


async def test_claude_extract_forces_the_tool_and_reads_its_input():
    provider, create = _claude_with(
        [SimpleNamespace(type="text", text="preamble"), _tool_use("record_item", EXTRACTION)]
    )

    result = await provider.extract(EXTRACT_IN)

    assert result.notice_deadline is True
    assert result.citations[0].signal_id == "s1"
    call = create.calls[0]
    assert call["model"] == "claude-test"
    assert call["tool_choice"] == {"type": "tool", "name": "record_item"}
    assert "s1" in call["messages"][0]["content"]  # the signals were sent


async def test_claude_extract_without_a_tool_block_raises_and_resilience_recovers():
    provider, _ = _claude_with([SimpleNamespace(type="text", text="I'd rather not")])

    with pytest.raises(RuntimeError, match="no tool_use"):
        await provider.extract(EXTRACT_IN)
    # The engine must never be left without an extraction.
    assert isinstance(await ResilientProvider(provider).extract(EXTRACT_IN), Extraction)


async def test_claude_sift_returns_findings_and_ignores_other_tools():
    provider, create = _claude_with(
        [_tool_use("something_else", {}), _tool_use("report_findings", FINDINGS)]
    )

    findings = await provider.sift(SIFT_IN)

    assert [f.matched_signal_ids for f in findings] == [["s1"]]
    assert create.calls[0]["tool_choice"] == {"type": "tool", "name": "report_findings"}


async def test_claude_sift_without_a_tool_block_raises():
    provider, _ = _claude_with([])
    with pytest.raises(RuntimeError):
        await provider.sift(SIFT_IN)


def test_claude_needs_a_key_before_it_touches_the_sdk(monkeypatch):
    # Checked before the import, so the message is right even without the extra.
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    with pytest.raises(RuntimeError, match="OSPREY_ANTHROPIC_API_KEY"):
        claude.ClaudeProvider()


def test_claude_builds_its_client_from_settings(monkeypatch, fake_sdks):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr(settings, "anthropic_model", "claude-configured")

    provider = claude.ClaudeProvider()

    assert fake_sdks.instances[-1].kwargs == {"api_key": "sk-ant-test"}
    assert provider._model == "claude-configured"


# --------------------------------------------------------------------------- #
# OpenAI
# --------------------------------------------------------------------------- #
def _openai_with(arguments: dict) -> tuple[openai_provider.OpenAIProvider, _Create]:
    provider = openai_provider.OpenAIProvider.__new__(openai_provider.OpenAIProvider)
    call = SimpleNamespace(function=SimpleNamespace(arguments=json.dumps(arguments)))
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))])
    create = _Create(reply)
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=create))
    provider._model = "gpt-test"
    return provider, create


async def test_openai_extract_forces_the_function_and_parses_arguments():
    provider, create = _openai_with(EXTRACTION)

    result = await provider.extract(EXTRACT_IN)

    assert result.recommended_action == "Respond in writing within 7 days"
    call = create.calls[0]
    assert call["model"] == "gpt-test"
    assert call["tool_choice"] == {"type": "function", "function": {"name": "record_item"}}
    assert [m["role"] for m in call["messages"]] == ["system", "user"]


async def test_openai_sift_parses_findings():
    provider, create = _openai_with(FINDINGS)

    findings = await provider.sift(SIFT_IN)

    assert findings[0].title == "LDs"
    assert create.calls[0]["tool_choice"]["function"]["name"] == "report_findings"


def test_openai_never_borrows_the_anthropic_key(monkeypatch, fake_sdks):
    """Regression: the server-wide OpenAI provider used settings.anthropic_api_key.

    With OSPREY_AI_PROVIDER=openai and only an Anthropic key configured, that sent
    the Anthropic secret to OpenAI as a bearer token.
    """
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-must-not-leave")
    monkeypatch.setattr(settings, "openai_api_key", "")

    with pytest.raises(RuntimeError, match="OSPREY_OPENAI_API_KEY"):
        openai_provider.OpenAIProvider()
    assert fake_sdks.instances == []  # no client was ever built


def test_openai_server_provider_uses_its_own_settings(monkeypatch, fake_sdks):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-must-not-leave")
    monkeypatch.setattr(settings, "openai_api_key", "sk-openai")
    monkeypatch.setattr(settings, "openai_model", "gpt-configured")
    monkeypatch.setattr(settings, "openai_base_url", "https://gateway.internal/v1")

    provider = openai_provider.OpenAIProvider()

    assert fake_sdks.instances[-1].kwargs == {
        "api_key": "sk-openai",
        "base_url": "https://gateway.internal/v1",
    }
    assert provider._model == "gpt-configured"


def test_a_users_connection_ignores_the_servers_openai_endpoint(monkeypatch, fake_sdks):
    # A bring-your-own key must not be sent to whatever gateway the operator set.
    monkeypatch.setattr(settings, "openai_base_url", "https://operator-gateway/v1")

    provider = openai_provider.OpenAIProvider(api_key="sk-user", model="gpt-user")

    assert fake_sdks.instances[-1].kwargs == {"api_key": "sk-user", "base_url": None}
    assert provider._model == "gpt-user"


# --------------------------------------------------------------------------- #
# Factories
# --------------------------------------------------------------------------- #
def test_build_selects_the_configured_provider(monkeypatch, fake_sdks):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant")
    monkeypatch.setattr(settings, "openai_api_key", "sk-openai")

    monkeypatch.setattr(settings, "ai_provider", "deterministic")
    assert isinstance(_build(), DeterministicProvider)

    monkeypatch.setattr(settings, "ai_provider", "claude")
    built = _build()
    assert isinstance(built, ResilientProvider)
    assert built.name == "claude"

    monkeypatch.setattr(settings, "ai_provider", "openai")
    built = _build()
    assert isinstance(built, ResilientProvider)
    assert built.name == "openai"


def test_provider_from_connection_builds_each_backend(fake_sdks):
    claude_built = provider_from_connection("claude", api_key="sk-ant-user", model="")
    assert isinstance(claude_built, ResilientProvider)
    assert claude_built._primary._model == "claude-sonnet-5"  # default when unset
    assert fake_sdks.instances[-1].kwargs == {"api_key": "sk-ant-user"}

    openai_built = provider_from_connection(
        "openai", api_key="sk-user", model="", base_url="https://byo/v1"
    )
    assert isinstance(openai_built, ResilientProvider)
    assert fake_sdks.instances[-1].kwargs == {"api_key": "sk-user", "base_url": "https://byo/v1"}


def test_provider_from_connection_unknown_backend_is_deterministic():
    assert isinstance(
        provider_from_connection("mystery", api_key="k", model=""), DeterministicProvider
    )
