"""DeterministicProvider — rule-based structured extraction.

Zero network, zero keys, fully reproducible. It is both the offline default and
the guaranteed fallback when a remote provider errors, so the engine always
produces an explainable, citation-backed result.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from ..models import Category
from .base import Citation, Extraction, ExtractionInput, LLMProvider

# Keyword rules, most-specific first. Contractual-notice cues are checked before
# generic categories because they carry the highest downstream weight.
_NOTICE_CUES = re.compile(
    r"\b(notice of (delay|claim|change)|reservation of rights|time[- ]?bar(red)?|"
    r"pursuant to (section|article|clause)|within \d+ (calendar |working |business )?days|"
    r"failure to (notify|respond)|deemed (approved|waived)|cure period)\b",
    re.I,
)
_CATEGORY_RULES: list[tuple[Category, re.Pattern[str]]] = [
    (
        Category.contractual_notice,
        re.compile(r"\b(notice of|claim|reservation of rights|time[- ]?bar)\b", re.I),
    ),
    (
        Category.safety,
        re.compile(r"\b(safety|osha|incident|injur|near[- ]miss|hazard|fall protection)\b", re.I),
    ),
    (
        Category.change_order,
        re.compile(r"\b(change order|c\.?o\.?\s?#|cor\b|pco\b|scope change|extra work)\b", re.I),
    ),
    (Category.rfi, re.compile(r"\b(rfi|request for information|clarification)\b", re.I)),
    (
        Category.submittal,
        re.compile(r"\b(submittal|shop drawing|product data|sample|resubmit)\b", re.I),
    ),
    (
        Category.invoice,
        re.compile(
            r"\b(invoice|payment application|pay app|pay application|retention|billing)\b", re.I
        ),
    ),
    (
        Category.schedule,
        re.compile(r"\b(schedule|delay|critical path|milestone|look[- ]?ahead|float)\b", re.I),
    ),
]
_BLOCK_CUES = re.compile(
    r"\b(block(s|ing|ed)?|hold(s|ing)?|cannot proceed|awaiting|pending|depends on)\b", re.I
)
_MONEY_RE = re.compile(r"\$\s?([0-9][0-9,]*(?:\.\d{2})?)(\s?[kKmM])?")
_DATE_RES = [
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b"),
]
# "within 7 days", "within 14 calendar days", "no later than 10 working days"
_RELATIVE_RE = re.compile(r"\bwithin\s+(\d{1,3})\s+(?:calendar|working|business\s+)?days?\b", re.I)


class DeterministicProvider(LLMProvider):
    name = "deterministic"

    async def extract(self, payload: ExtractionInput) -> Extraction:
        text = (
            payload.item_title
            + "\n"
            + "\n".join(f"{s.get('title', '')}\n{s.get('body', '')}" for s in payload.signals)
        )

        category = self._categorize(text)
        notice = bool(_NOTICE_CUES.search(text)) or category == Category.contractual_notice
        deadline = self._deadline(payload)
        exposure = self._exposure(payload, text)
        blocking = self._blocking(text)
        confidence = self._confidence(payload, category, deadline, exposure)
        citations = self._citations(payload)

        summary = self._summary(payload, category)
        action = self._recommend(category, notice, deadline, blocking)

        return Extraction(
            category=category,
            summary=summary,
            deadline=deadline,
            dollar_exposure=exposure,
            notice_deadline=notice,
            blocking=blocking,
            recommended_action=action,
            confidence=confidence,
            citations=citations,
        )

    # -- rules --------------------------------------------------------------- #
    def _categorize(self, text: str) -> Category:
        for cat, rule in _CATEGORY_RULES:
            if rule.search(text):
                return cat
        return Category.general

    def _deadline(self, payload: ExtractionInput) -> str | None:
        dues: list[str] = [str(s["due_at"]) for s in payload.signals if s.get("due_at")]
        if dues:
            return min(dues)
        blob = " ".join(f"{s.get('title', '')} {s.get('body', '')}" for s in payload.signals)
        # Explicit dates first.
        for rx in _DATE_RES:
            m = rx.search(blob)
            if m:
                return self._norm_date(m.group(1))
        # Relative deadlines ("within 7 days") resolved against when the signal
        # occurred — critical for contractual-notice math, which is usually phrased
        # relatively ("respond within N days or the claim is waived").
        m = _RELATIVE_RE.search(blob)
        if m:
            base = self._earliest_occurred(payload)
            if base is not None:
                return (base + timedelta(days=int(m.group(1)))).date().isoformat()
        return None

    @staticmethod
    def _earliest_occurred(payload: ExtractionInput) -> datetime | None:
        stamps: list[datetime] = []
        for s in payload.signals:
            raw = s.get("occurred_at")
            if not raw:
                continue
            try:
                stamps.append(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
            except ValueError:
                continue
        if not stamps:
            return None
        earliest = min(stamps)
        return earliest.replace(tzinfo=None) if earliest.tzinfo else earliest

    @staticmethod
    def _norm_date(raw: str) -> str:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                continue
        return raw

    def _exposure(self, payload: ExtractionInput, text: str) -> float | None:
        amounts = [float(s["amount"]) for s in payload.signals if s.get("amount")]
        if amounts:
            return max(amounts)
        best: float | None = None
        for m in _MONEY_RE.finditer(text):
            val = float(m.group(1).replace(",", ""))
            suffix = (m.group(2) or "").strip().lower()
            if suffix == "k":
                val *= 1_000
            elif suffix == "m":
                val *= 1_000_000
            best = val if best is None else max(best, val)
        return best

    def _blocking(self, text: str) -> list[str]:
        out: list[str] = []
        for line in text.splitlines():
            if _BLOCK_CUES.search(line):
                clean = line.strip()
                if clean and clean not in out:
                    out.append(clean[:160])
            if len(out) >= 3:
                break
        return out

    def _confidence(self, payload, category, deadline, exposure) -> float:
        score = 0.4
        if category != Category.general:
            score += 0.2
        if deadline:
            score += 0.15
        if exposure:
            score += 0.15
        if any(s.get("body") for s in payload.signals):
            score += 0.1
        return round(min(score, 0.98), 2)

    def _citations(self, payload: ExtractionInput) -> list[Citation]:
        out: list[Citation] = []
        for s in payload.signals[:3]:
            span = (s.get("title") or s.get("body") or "")[:120]
            if s.get("id"):
                out.append(Citation(signal_id=str(s["id"]), quote_span=span))
        return out

    def _summary(self, payload: ExtractionInput, category: Category) -> str:
        title = payload.item_title.strip() or (
            payload.signals[0].get("title") if payload.signals else ""
        )
        n = len(payload.signals)
        src = f" across {n} sources" if n > 1 else ""
        return f"{category.value.replace('_', ' ').title()}: {title}{src}".strip()

    def _recommend(self, category, notice, deadline, blocking) -> str:
        if notice:
            return "Contractual notice implicated — confirm the notice deadline and issue a written response before it lapses (owner: PM)."
        base = {
            Category.rfi: "Draft and send the RFI response; assign the reviewing engineer.",
            Category.submittal: "Route the submittal for review and log the ball-in-court owner.",
            Category.change_order: "Price the change and issue/approve the change order.",
            Category.invoice: "Verify the payment application against the schedule of values and approve or reject.",
            Category.safety: "Escalate to the safety lead and document corrective action.",
            Category.schedule: "Assess critical-path impact and update the look-ahead.",
            Category.general: "Review and assign an owner.",
        }.get(category, "Review and assign an owner.")
        if deadline:
            base += f" Due {deadline}."
        if blocking:
            base += " Unblocks downstream work."
        return base
