"""AI layer — pluggable extraction + embeddings.

The default provider is fully offline and deterministic so the entire engine runs
with no API key and tests are reproducible. Claude and Ollama are config opt-ins.
"""

from .base import Extraction, LLMProvider
from .embeddings import Embedder, get_embedder
from .provider import get_provider

__all__ = ["Extraction", "LLMProvider", "Embedder", "get_embedder", "get_provider"]
