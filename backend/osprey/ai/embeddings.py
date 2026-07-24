"""Embeddings — deterministic hashing embedder (offline default).

Maps text into a fixed-dimension unit vector using signed feature hashing with
term-frequency weighting. No model download, fully deterministic — so clustering
is reproducible in tests and works identically on SQLite and Postgres. A real
embedding-model provider can be slotted in behind the same ``Embedder`` interface.
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod
from functools import lru_cache

from ..config import settings

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "is",
        "are",
        "be",
        "was",
        "were",
        "this",
        "that",
        "it",
        "as",
        "at",
        "by",
        "with",
        "from",
        "re",
        "fwd",
        "fw",
        "please",
        "thanks",
        "thank",
        "you",
        "regards",
        "hi",
        "hello",
        "dear",
    ]
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP and len(t) > 1]


class Embedder(ABC):
    dim: int

    @abstractmethod
    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder(Embedder):
    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim or settings.embedding_dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = tokenize(text)
        if not tokens:
            return vec
        for tok in tokens:
            h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:8], "big")
            idx = h % self.dim
            sign = 1.0 if (h >> 63) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0:
            return vec
        return [v / norm for v in vec]


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    # Both inputs are already unit-normalized by HashingEmbedder; clamp for safety.
    return max(-1.0, min(1.0, dot))


@lru_cache
def get_embedder() -> Embedder:
    return HashingEmbedder()
