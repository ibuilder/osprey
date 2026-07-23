"""Structured, PII-scrubbing logging setup."""

from __future__ import annotations

import logging
import re
import sys

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TOKEN_RE = re.compile(r"(?i)(bearer|token|secret|password|api[_-]?key)\s*[=:]\s*\S+")


class ScrubFilter(logging.Filter):
    """Redact obvious PII / secrets from log records before they are emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        scrubbed = _EMAIL_RE.sub("<email>", msg)
        scrubbed = _TOKEN_RE.sub(r"\1=<redacted>", scrubbed)
        if scrubbed != msg:
            record.msg = scrubbed
            record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s :: %(message)s")
    )
    handler.addFilter(ScrubFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Quiet noisy libraries.
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel("WARNING")
