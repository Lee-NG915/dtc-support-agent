"""Vector stores and dense retrieval over embedded chunks."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.config.policy_profiles import get_policy_profile
from app.core.enums import DocumentCategory
from app.core.exceptions import RetrievalError
from app.providers.base import EmbeddingProvider
from app.retrieval.policy import chunk_matches_scope
from app.schemas.retrieval import PolicyScope, RetrievedChunk

logger = logging.getLogger(__name__)


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


def stable_point_id(chunk_id: str) -> str:
    """Qdrant point IDs must be UUID/int. Derive from chunk_id so re-ingest is stable."""
    return str(uuid.UUID(hashlib.md5(chunk_id.encode(), usedforsecurity=False).hexdigest()))


@dataclass
class VectorRecord:
    chunk: RetrievedChunk
    embedding: list[float]


class InMemoryVectorStore:
    """Used in tests and when Qdrant is unavailable."""

    def __init__(self) -> None:
        self._records: list[VectorRecord] = []

    def upsert(self, records: list[VectorRecord]) -> None:
        existing = {record.chunk.chunk_id: index for index, record in enumerate(self._records)}
        for record in records:
            if record.chunk.chunk_id in existing:
                self._records[existing[record.chunk.chunk_id]] = record
            else:
                self._records.append(record)

    def search(
        self, query_vector: list[float], *, top_k: int, scope: PolicyScope | None = None
    ) -> list[RetrievedChunk]:
        resolved_scope = scope or PolicyScope()
        scored: list[tuple[float, VectorRecord]] = [
            (cosine(query_vector, record.embedding), record)
            for record in self._records
            if chunk_matches_scope(record.chunk, resolved_scope)
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        results: list[RetrievedChunk] = []
        for rank, (score, record) in enumerate(scored[:top_k], start=1):
            chunk = record.chunk.model_copy(deep=True)
            chunk.score = float(score)
            chunk.rank = rank
            chunk.source = "vector"
            results.append(chunk)
        return results

    def ping(self) -> bool:
        return True


def _date_ordinal(value: date | None) -> int | None:
    return value.toordinal() if value is not None else None


def _qdrant_scope_filter(scope: PolicyScope):
    from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue, Range

    must: list[Any] = [
        FieldCondition(key="brand", match=MatchValue(value=scope.brand)),
        FieldCondition(key="market", match=MatchValue(value=scope.market)),
    ]
    if scope.product_category:
        must.append(
            FieldCondition(key="product_categories", match=MatchAny(any=[scope.product_category]))
        )
    if get_policy_profile(scope.brand).filter_dates and scope.as_of is not None:
        as_of = scope.as_of.toordinal()
        must.extend(
            [
                FieldCondition(key="applicable_from_day", range=Range(lte=as_of)),
                FieldCondition(key="applicable_to_day", range=Range(gte=as_of)),
            ]
        )
    return Filter(must=must)


class QdrantVectorStore:
    def __init__(self, url: str, collection: str, dim: int) -> None:
        self._url = url
        self._collection = collection
        self._dim = dim
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            from qdrant_client import QdrantClient

            self._client = QdrantClient(url=self._url, timeout=5.0)
        return self._client

    def ensure_collection(self) -> None:
        from qdrant_client.models import Distance, VectorParams

        client = self._ensure_client()
        names = [item.name for item in client.get_collections().collections]
        if self._collection not in names:
            client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=self._dim, distance=Distance.COSINE),
            )

    def upsert(self, records: list[VectorRecord]) -> None:
        from qdrant_client.models import PointStruct

        client = self._ensure_client()
        self.ensure_collection()
        points = [
            PointStruct(
                id=stable_point_id(record.chunk.chunk_id),
                vector=record.embedding,
                payload={
                    "chunk_id": record.chunk.chunk_id,
                    "document_id": record.chunk.document_id,
                    "title": record.chunk.title,
                    "content": record.chunk.content,
                    "section": record.chunk.section,
                    "chunk_index": record.chunk.chunk_index,
                    "category": record.chunk.category.value,
                    "policy_version": record.chunk.policy_version,
                    "brand": record.chunk.brand,
                    "market": record.chunk.market,
                    "product_categories": record.chunk.product_categories,
                    "retrieved_at": (
                        record.chunk.retrieved_at.isoformat() if record.chunk.retrieved_at else None
                    ),
                    "effective_from": (
                        record.chunk.effective_from.isoformat()
                        if record.chunk.effective_from
                        else None
                    ),
                    "effective_to": (
                        record.chunk.effective_to.isoformat() if record.chunk.effective_to else None
                    ),
                    "applicable_from_day": _date_ordinal(
                        record.chunk.effective_from or record.chunk.retrieved_at
                    ),
                    "applicable_to_day": _date_ordinal(record.chunk.effective_to)
                    or date.max.toordinal(),
                    "policy_status": record.chunk.policy_status,
                    "requires_human_review": record.chunk.requires_human_review,
                    "review_query_rules": [
                        rule.model_dump() for rule in record.chunk.review_query_rules
                    ],
                    "source_type": record.chunk.source_type,
                    "source_label": record.chunk.source_label,
                    "source_path": record.chunk.source_path,
                    "source_urls": record.chunk.source_urls,
                    "source_updated": record.chunk.source_updated,
                    "source": record.chunk.metadata.get("source", "knowledge"),
                },
            )
            for record in records
        ]
        if points:
            client.upsert(collection_name=self._collection, points=points)

    def remove_stale(self, active_chunk_ids: set[str]) -> None:
        """Delete documents/chunks removed from the source bundle after upsert."""
        if not active_chunk_ids:
            raise ValueError("refusing to remove Qdrant points for an empty corpus")
        from qdrant_client.models import PointIdsList

        client = self._ensure_client()
        active_point_ids = {stable_point_id(chunk_id) for chunk_id in active_chunk_ids}
        stale: list[int | str] = []
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=self._collection,
                limit=256,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            stale.extend(str(point.id) for point in points if str(point.id) not in active_point_ids)
            if offset is None:
                break
        if stale:
            client.delete(
                collection_name=self._collection,
                points_selector=PointIdsList(points=stale),
            )

    def ping(self) -> bool:
        try:
            self._ensure_client().get_collections()
            return True
        except Exception:
            return False

    def search(
        self, query_vector: list[float], *, top_k: int, scope: PolicyScope | None = None
    ) -> list[RetrievedChunk]:
        resolved_scope = scope or PolicyScope()
        if (
            get_policy_profile(resolved_scope.brand).requires_category
            and not resolved_scope.product_category
        ):
            return []
        try:
            client = self._ensure_client()
            hits = client.search(
                collection_name=self._collection,
                query_vector=query_vector,
                limit=top_k,
                with_payload=True,
                query_filter=_qdrant_scope_filter(resolved_scope),
            )
        except Exception as exc:
            raise RetrievalError("qdrant search failed") from exc
        results: list[RetrievedChunk] = []
        for rank, hit in enumerate(hits, start=1):
            payload = hit.payload or {}
            results.append(
                RetrievedChunk(
                    chunk_id=str(payload.get("chunk_id", hit.id)),
                    document_id=str(payload.get("document_id", "")),
                    title=str(payload.get("title", "")),
                    content=str(payload.get("content", "")),
                    score=float(hit.score),
                    rank=rank,
                    source="vector",
                    category=DocumentCategory(str(payload.get("category") or "faq")),
                    section=payload.get("section"),
                    chunk_index=int(payload.get("chunk_index") or 0),
                    policy_version=str(payload.get("policy_version") or "v1"),
                    brand=payload.get("brand") or "upstream_demo",
                    market=payload.get("market") or "global",
                    product_categories=list(payload.get("product_categories") or []),
                    retrieved_at=payload.get("retrieved_at"),
                    effective_from=payload.get("effective_from"),
                    effective_to=payload.get("effective_to"),
                    policy_status=payload.get("policy_status") or "demo",
                    requires_human_review=bool(payload.get("requires_human_review")),
                    review_query_rules=list(payload.get("review_query_rules") or []),
                    source_type=payload.get("source_type") or "legacy",
                    source_label=str(payload.get("source_label") or ""),
                    source_path=str(payload.get("source_path") or ""),
                    source_urls=list(payload.get("source_urls") or []),
                    source_updated=str(payload.get("source_updated") or ""),
                    metadata={"source": str(payload.get("source", "qdrant"))},
                )
            )
        return results


class VectorRetriever:
    def __init__(
        self, store: InMemoryVectorStore | QdrantVectorStore, embedder: EmbeddingProvider
    ) -> None:
        self.store = store
        self.embedder = embedder

    async def search(
        self, query: str, *, top_k: int, scope: PolicyScope | None = None
    ) -> list[RetrievedChunk]:
        vectors = await self.embedder.embed([query])
        if not vectors:
            return []
        # Qdrant client is sync; do not block the event loop.
        return await asyncio.to_thread(self.store.search, vectors[0], top_k=top_k, scope=scope)

    async def ping(self) -> bool:
        return await asyncio.to_thread(self.store.ping)


def build_records(
    chunks: list[RetrievedChunk],
    embeddings: list[list[float]],
) -> list[VectorRecord]:
    if len(chunks) != len(embeddings):
        raise ValueError("chunk / embedding length mismatch")
    return [
        VectorRecord(chunk=chunk, embedding=embedding)
        for chunk, embedding in zip(chunks, embeddings, strict=False)
    ]
