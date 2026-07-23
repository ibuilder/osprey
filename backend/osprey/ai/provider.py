"""Provider factory + resilient wrapper.

Selects the configured provider and always degrades to the deterministic provider
if a remote call fails — the engine must never be left without an extraction.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from ..config import settings
from .base import Extraction, ExtractionInput, LLMProvider
from .deterministic import DeterministicProvider

log = logging.getLogger("osprey.ai")


class ResilientProvider(LLMProvider):
    def __init__(self, primary: LLMProvider) -> None:
        self.name = primary.name
        self._primary = primary
        self._fallback = DeterministicProvider()

    async def extract(self, payload: ExtractionInput) -> Extraction:
        try:
            return await self._primary.extract(payload)
        except Exception as exc:  # noqa: BLE001 - deliberate: never fail the pipeline
            log.warning("AI provider '%s' failed (%s); using deterministic fallback", self.name, exc)
            return await self._fallback.extract(payload)

    async def sift(self, payload):
        try:
            return await self._primary.sift(payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("AI sift via '%s' failed (%s); using deterministic fallback", self.name, exc)
            return await self._fallback.sift(payload)


def _build() -> LLMProvider:
    provider = settings.ai_provider
    if provider == "claude":
        from .claude import ClaudeProvider

        return ResilientProvider(ClaudeProvider())
    if provider == "openai":
        from .openai_provider import OpenAIProvider

        return ResilientProvider(OpenAIProvider())
    if provider == "ollama":
        from .ollama import OllamaProvider

        return ResilientProvider(OllamaProvider())
    return DeterministicProvider()


@lru_cache
def get_provider() -> LLMProvider:
    return _build()


def provider_from_connection(provider: str, *, api_key: str, model: str, base_url: str | None = None) -> LLMProvider:
    """Build a provider from a user's AIConnection (bring-your-own key).

    Falls back to the deterministic provider if the requested backend can't be
    constructed (missing extra, bad key) — sift/extract still work offline.
    """
    try:
        if provider == "claude":
            from .claude import ClaudeProvider

            inst = ClaudeProvider.__new__(ClaudeProvider)
            from anthropic import AsyncAnthropic

            inst._client = AsyncAnthropic(api_key=api_key)  # type: ignore[attr-defined]
            inst._model = model or "claude-sonnet-5"  # type: ignore[attr-defined]
            inst.name = "claude"
            return ResilientProvider(inst)
        if provider == "openai":
            from .openai_provider import OpenAIProvider

            return ResilientProvider(OpenAIProvider(api_key=api_key, model=model or "gpt-4o", base_url=base_url))
        if provider == "ollama":
            from .ollama import OllamaProvider

            inst2 = OllamaProvider()
            if model:
                inst2._model = model  # type: ignore[attr-defined]
            if base_url:
                inst2._host = base_url.rstrip("/")  # type: ignore[attr-defined]
            return ResilientProvider(inst2)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not build provider '%s' from connection (%s); using default", provider, exc)
    return DeterministicProvider()
