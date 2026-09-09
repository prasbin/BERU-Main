"""Deterministic offline embedding provider.

Generates pseudo-embeddings from text using a hash-based approach. No network
access or API key needed. Useful for testing and offline development.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

from backend.engines.embeddings.base import EmbeddingProvider


class MockEmbeddingProvider(EmbeddingProvider):
    name = "mock"

    def __init__(self, dimension: int = 64) -> None:
        self.dimension = dimension

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._hash_embed(text) for text in texts]

    def _hash_embed(self, text: str) -> list[float]:
        """Generate a deterministic pseudo-embedding from text."""
        h = hashlib.sha256(text.lower().encode()).digest()
        vec: list[float] = []
        for i in range(self.dimension):
            byte_val = h[i % len(h)]
            seed = (i * 2654435761) & 0xFFFFFFFF
            val = math.sin(byte_val + seed) * 0.5
            vec.append(val)
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]
