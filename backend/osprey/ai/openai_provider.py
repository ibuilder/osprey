"""OpenAI provider — structured extraction + sift via function calling.

Optional: requires the ``openai`` SDK (extras ``ai``) and an API key. Wrapped by
ResilientProvider, so any failure degrades to the deterministic path.
"""

from __future__ import annotations

import json

from ..config import settings
from .base import Extraction, ExtractionInput, LLMProvider, SiftFinding, SiftInput
from .claude import _SIFT_SYSTEM, _SYSTEM

_EXTRACT_FN = {
    "type": "function",
    "function": {
        "name": "record_item",
        "description": "Emit the structured extraction for one hotlist item.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [
                        "rfi",
                        "change_order",
                        "submittal",
                        "invoice",
                        "safety",
                        "schedule",
                        "contractual_notice",
                        "general",
                    ],
                },
                "summary": {"type": "string"},
                "deadline": {"type": ["string", "null"]},
                "dollar_exposure": {"type": ["number", "null"]},
                "notice_deadline": {"type": "boolean"},
                "blocking": {"type": "array", "items": {"type": "string"}},
                "recommended_action": {"type": "string"},
                "confidence": {"type": "number"},
                "citations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "signal_id": {"type": "string"},
                            "quote_span": {"type": "string"},
                        },
                        "required": ["signal_id", "quote_span"],
                    },
                },
            },
            "required": ["category", "summary", "recommended_action", "confidence"],
        },
    },
}

_SIFT_FN = {
    "type": "function",
    "function": {
        "name": "report_findings",
        "description": "Report the findings from sifting signals against the instruction.",
        "parameters": {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "body": {"type": "string"},
                            "matched_signal_ids": {"type": "array", "items": {"type": "string"}},
                            "confidence": {"type": "number"},
                        },
                        "required": ["title", "matched_signal_ids"],
                    },
                }
            },
            "required": ["findings"],
        },
    },
}


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(
        self, *, api_key: str | None = None, model: str | None = None, base_url: str | None = None
    ) -> None:
        from openai import AsyncOpenAI  # imported lazily

        key = api_key or settings.anthropic_api_key  # falls back only if explicitly set elsewhere
        if not key:
            raise RuntimeError("OpenAI API key is not set")
        self._client = AsyncOpenAI(api_key=key, base_url=base_url)
        self._model = model or "gpt-4o"

    async def _call(self, system: str, user: str, fn: dict) -> dict:
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            tools=[fn],
            tool_choice={"type": "function", "function": {"name": fn["function"]["name"]}},
        )
        call = resp.choices[0].message.tool_calls[0]
        return json.loads(call.function.arguments)

    async def extract(self, payload: ExtractionInput) -> Extraction:
        args = await self._call(_SYSTEM, json.dumps(payload.model_dump(), default=str), _EXTRACT_FN)
        return Extraction.model_validate(args)

    async def sift(self, payload: SiftInput) -> list[SiftFinding]:
        args = await self._call(
            _SIFT_SYSTEM, json.dumps(payload.model_dump(), default=str), _SIFT_FN
        )
        return [SiftFinding.model_validate(f) for f in args.get("findings", [])]
