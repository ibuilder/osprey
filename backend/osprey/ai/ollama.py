"""Ollama provider — local LLM extraction (privacy mode).

Optional: requires a running Ollama host. Uses JSON-format output; wrapped by
ResilientProvider so failures degrade to the deterministic path.
"""

from __future__ import annotations

import json

import httpx

from ..config import settings
from .base import Extraction, ExtractionInput, LLMProvider

_PROMPT = (
    "You are Osprey's extraction engine for construction/RE project data. Given the "
    "clustered item and its signals, respond with ONLY a JSON object matching this "
    "schema: {category, summary, deadline, dollar_exposure, notice_deadline, "
    "blocking[], recommended_action, confidence, citations[{signal_id, quote_span}]}. "
    "category is one of rfi|change_order|submittal|invoice|safety|schedule|"
    "contractual_notice|general. Weight contractual notice deadlines highest."
)


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self) -> None:
        self._host = settings.ollama_host.rstrip("/")
        self._model = settings.ollama_model

    async def extract(self, payload: ExtractionInput) -> Extraction:
        body = {
            "model": self._model,
            "format": "json",
            "stream": False,
            "prompt": f"{_PROMPT}\n\nITEM:\n{json.dumps(payload.model_dump(), default=str)}",
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{self._host}/api/generate", json=body)
            resp.raise_for_status()
            raw = resp.json().get("response", "{}")
        return Extraction.model_validate(json.loads(raw))
