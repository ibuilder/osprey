"""Claude provider — structured extraction via tool-use / JSON.

Optional: requires ``anthropic`` (extras ``ai``) and ``OSPREY_ANTHROPIC_API_KEY``.
Wrapped by ResilientProvider, so any failure degrades to the deterministic path.
"""

from __future__ import annotations

import json

from ..config import settings
from .base import Extraction, ExtractionInput, LLMProvider, SiftFinding, SiftInput

_SYSTEM = (
    "You are Osprey's extraction engine for construction/real-estate project data. "
    "Return ONE structured record for the clustered item. Be precise, never invent "
    "facts, and cite the source signal id and a short verbatim quote for every "
    "non-trivial conclusion. Weight contractual NOTICE deadlines highest."
)

_SIFT_SYSTEM = (
    "You are Osprey's sift engine. Given a user instruction and a list of project "
    "signals (each with an id, title, and body), return findings: the groups of "
    "signals that match the instruction. For each finding, give a concise title, a "
    "short body summarizing the evidence, the exact matched signal ids, and a "
    "confidence 0..1. Only include signals that genuinely match; return an empty "
    "list if nothing matches. Never invent signal ids."
)

_SIFT_TOOL = {
    "name": "report_findings",
    "description": "Report the findings from sifting the signals against the instruction.",
    "input_schema": {
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
}

_TOOL = {
    "name": "record_item",
    "description": "Emit the structured extraction for one hotlist item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": [
                    "rfi", "change_order", "submittal", "invoice", "safety",
                    "schedule", "contractual_notice", "general",
                ],
            },
            "summary": {"type": "string"},
            "deadline": {"type": ["string", "null"], "description": "ISO8601 or null"},
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
}


class ClaudeProvider(LLMProvider):
    name = "claude"

    def __init__(self) -> None:
        from anthropic import AsyncAnthropic  # imported lazily

        if not settings.anthropic_api_key:
            raise RuntimeError("OSPREY_ANTHROPIC_API_KEY is not set")
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model

    async def extract(self, payload: ExtractionInput) -> Extraction:
        user = json.dumps(payload.model_dump(), default=str, indent=2)
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=_SYSTEM,
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "record_item"},
            messages=[{"role": "user", "content": f"Item and its signals:\n{user}"}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "record_item":
                return Extraction.model_validate(block.input)
        raise RuntimeError("Claude returned no tool_use block")

    async def sift(self, payload: SiftInput) -> list[SiftFinding]:
        user = json.dumps(payload.model_dump(), default=str)
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            system=_SIFT_SYSTEM,
            tools=[_SIFT_TOOL],
            tool_choice={"type": "tool", "name": "report_findings"},
            messages=[{"role": "user", "content": user}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "report_findings":
                findings = block.input.get("findings", [])
                return [SiftFinding.model_validate(f) for f in findings]
        raise RuntimeError("Claude returned no tool_use block")
