"""SQLite-backed vector store for semantic recall.

Stores text embeddings as JSON-serialised lists in SQLite and performs
cosine similarity search in pure Python (no numpy dependency). Suitable for
small-to-medium collections; swap in a proper vector database at scale.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from sqlalchemy import Column, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from backend.database.base import Base


class EmbeddingRow(Base):
    __tablename__ = "embeddings"
    __table_args__ = (
        # One embedding per (source entity). The vector store upserts on this key.
        UniqueConstraint("source_table", "source_id", name="uq_embeddings_source_entity"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_table = Column(String(64), nullable=False, index=True)
    source_id = Column(String(36), nullable=False, index=True)
    text = Column(Text, nullable=False)
    vector_json = Column(Text, nullable=False)
    dimension = Column(Integer, nullable=False)


@dataclass(frozen=True)
class SearchResult:
    text: str
    source_table: str
    source_id: str
    score: float


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
    norm_b = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (norm_a * norm_b)


class VectorStore:
    async def upsert(
        self,
        session: AsyncSession,
        *,
        source_table: str,
        source_id: str,
        text: str,
        vector: list[float],
    ) -> None:
        stmt = sqlite_insert(EmbeddingRow).values(
            source_table=source_table,
            source_id=source_id,
            text=text,
            vector_json=json.dumps(vector),
            dimension=len(vector),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["source_table", "source_id"],
            set_={
                "text": stmt.excluded.text,
                "vector_json": stmt.excluded.vector_json,
                "dimension": stmt.excluded.dimension,
            },
        )
        await session.execute(stmt)

    async def search(
        self,
        session: AsyncSession,
        query_vector: list[float],
        *,
        source_table: str,
        top_k: int = 5,
    ) -> list[SearchResult]:
        stmt = select(EmbeddingRow).where(EmbeddingRow.source_table == source_table)
        rows = list((await session.execute(stmt)).scalars().all())

        scored: list[SearchResult] = []
        for row in rows:
            vec = json.loads(row.vector_json)
            score = _cosine_similarity(query_vector, vec)
            scored.append(
                SearchResult(
                    text=row.text,
                    source_table=row.source_table,
                    source_id=row.source_id,
                    score=score,
                )
            )

        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]

    async def delete(
        self,
        session: AsyncSession,
        *,
        source_table: str,
        source_id: str,
    ) -> None:
        stmt = select(EmbeddingRow).where(
            EmbeddingRow.source_table == source_table,
            EmbeddingRow.source_id == source_id,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            await session.delete(row)

    async def count(
        self,
        session: AsyncSession,
        *,
        source_table: str,
    ) -> int:
        stmt = select(EmbeddingRow.id).where(EmbeddingRow.source_table == source_table)
        return len((await session.execute(stmt)).all())
