"""OpenAI-compatible embedding provider.

Works with any OpenAI-compatible /embeddings endpoint (OpenAI, Ollama, etc.).
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from backend.core.errors import LLMProviderError
from backend.engines.embeddings.base import EmbeddingProvider


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "text-embedding-3-small",
        dimension: int = 1536,
        timeout: float = 30.0,
    ) -> None:
        self.name = f"openai_embed:{model}"
        self.dimension = dimension
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            resp = await self._client.post(
                "/embeddings",
                json={"input": list(texts), "model": self._model},
            )
            resp.raise_for_status()
            data = resp.json()
            embeddings = sorted(data["data"], key=lambda e: e["index"])
            return [e["embedding"] for e in embeddings]
        except httpx.HTTPStatusError as exc:
            raise LLMProviderError(
                f"Embedding request failed: {exc.response.status_code}"
            ) from exc
        except Exception as exc:
            raise LLMProviderError(f"Embedding request failed: {exc}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
