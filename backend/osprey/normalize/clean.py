"""Cleaning raw source text into concise signal bodies."""

from __future__ import annotations

import re

_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")
_MANY_NL = re.compile(r"\n{3,}")
# Common quoted-reply / forwarded boundaries.
_QUOTE_BOUNDARY = re.compile(
    r"(?im)^(on .+ wrote:|-{2,}\s*original message\s*-{2,}|from:.*\n(sent|date):|_{5,})"
)
_SIGNATURE = re.compile(r"(?im)^--\s*$")


def _strip_html(text: str) -> str:
    if "<" in text and ">" in text:
        text = _HTML_TAG.sub(" ", text)
        text = (
            text.replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
        )
    return text


def strip_quoted_reply(text: str) -> str:
    """Keep only the top (newest) portion of an email body."""
    m = _QUOTE_BOUNDARY.search(text)
    if m:
        text = text[: m.start()]
    m = _SIGNATURE.search(text)
    if m:
        text = text[: m.start()]
    return text


def clean_text(text: str, *, drop_quoted: bool = True, max_chars: int = 8000) -> str:
    if not text:
        return ""
    text = _strip_html(text)
    if drop_quoted:
        text = strip_quoted_reply(text)
    text = _WS.sub(" ", text)
    text = _MANY_NL.sub("\n\n", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return text.strip()[:max_chars]
