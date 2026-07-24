"""LLM provider interface and the structured extraction schema."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from ..models import Category


class Citation(BaseModel):
    signal_id: str
    quote_span: str


class Extraction(BaseModel):
    """Structured result the AI layer returns for a clustered Item.

    Every conclusion is backed by citations into source text so the hotlist is
    verifiable, not a black box.
    """

    category: Category = Category.general
    summary: str = ""
    deadline: str | None = None  # ISO8601 or null
    dollar_exposure: float | None = None
    notice_deadline: bool = False  # contractual notice — weighted highest
    blocking: list[str] = Field(default_factory=list)
    recommended_action: str = ""
    confidence: float = 0.5  # 0..1 extraction certainty
    citations: list[Citation] = Field(default_factory=list)


class ExtractionInput(BaseModel):
    """What the provider is asked to extract over (one Item's signals)."""

    item_title: str
    signals: list[dict]  # [{id, title, body, due_at, amount, url, participants}]


class SiftFinding(BaseModel):
    """One result of sifting signals against a user instruction.

    Sift is *retrieval*: it selects the signals that match the instruction and
    synthesizes a title/body. The normal extraction + scoring pipeline then turns
    each finding into an explainable, ranked hotlist item.
    """

    title: str
    body: str = ""
    matched_signal_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.5


class SiftInput(BaseModel):
    instruction: str
    signals: list[dict]  # [{id, title, body, ...}]


def _keyword_sift(payload: SiftInput) -> list[SiftFinding]:
    """Deterministic keyword sift — the offline default every provider inherits."""
    import re

    terms = set(re.findall(r"[a-z0-9]{3,}", payload.instruction.lower()))
    stop = {
        "the",
        "and",
        "any",
        "all",
        "for",
        "with",
        "that",
        "find",
        "show",
        "list",
        "get",
        "flag",
    }
    terms -= stop
    matched: list[dict] = []
    for s in payload.signals:
        blob = f"{s.get('title', '')} {s.get('body', '')}".lower()
        hits = sum(1 for t in terms if t in blob)
        if hits:
            matched.append({**s, "_hits": hits})
    if not matched:
        return []
    matched.sort(key=lambda s: s["_hits"], reverse=True)
    ids = [str(s["id"]) for s in matched if s.get("id")]
    body = "\n".join(f"- {s.get('title', '')}: {s.get('body', '')[:200]}" for s in matched[:8])
    conf = min(0.95, 0.5 + 0.05 * len(matched))
    return [
        SiftFinding(
            title=f"AI sift: {payload.instruction.strip()[:80]} ({len(matched)} matches)",
            body=body,
            matched_signal_ids=ids,
            confidence=round(conf, 2),
        )
    ]


class LLMProvider(ABC):
    name: str

    @abstractmethod
    async def extract(self, payload: ExtractionInput) -> Extraction:
        """Return a structured Extraction for the given clustered item."""
        raise NotImplementedError

    async def sift(self, payload: SiftInput) -> list[SiftFinding]:
        """Select signals matching an instruction. Default: deterministic keyword sift."""
        return _keyword_sift(payload)
