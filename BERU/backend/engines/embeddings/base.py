"""Provider-agnostic embedding interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence


class EmbeddingProvider(ABC):
    """Abstract base for embedding providers.

    Implementations convert text into fixed-dimensional float vectors.
    """

    name: str = "base"
    dimension: int = 0

    @abstractmethod
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts and return their vector representations."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release any held resources."""
        return None
