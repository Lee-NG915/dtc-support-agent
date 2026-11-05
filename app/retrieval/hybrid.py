"""Hybrid retriever fusing BM25 and dense vectors with RRF."""

from __future__ import annotations

import logging
import time

from app.core.exceptions import RetrievalError
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.cache import RetrievalCache
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.vector import VectorRetriever
from app.schemas.retrieval import PolicyScope, RetrievedChunk

logger = logging.getLogger(__name__)


class HybridRetriever:
    """BM25 + dense search, fused with RRF.

    Each retriever is allowed to fail independently. If both fail, raise.
    """

    def __init__(
        self,
        bm25: BM25Retriever,
        vector: VectorRetriever,
        *,
        rrf_k: int,
        cache: RetrievalCache | None = None,
        corpus_revision: str = "",
    ) -> None:
        self._bm25 = bm25
        self._vector = vector
        self._rrf_k = rrf_k
        self._cache = cache
        self._corpus_revision = corpus_revision

    def matching_review_chunks(
        self, query: str, *, scope: PolicyScope | None = None
    ) -> list[RetrievedChunk]:
        """Evaluate structured exception rules before result-size truncation."""
        return self._bm25.matching_review_chunks(query, scope=scope)

    async def retrieve(
        self, query: str, *, top_k: int, scope: PolicyScope | None = None
    ) -> list[RetrievedChunk]:
        resolved_scope = scope or PolicyScope()
        if self._cache is not None:
            cached = await self._cache.get(
                query, top_k, scope=resolved_scope, revision=self._corpus_revision
            )
            if cached is not None:
                return cached
        started = time.perf_counter()
        lexical: list[RetrievedChunk] = []
        dense: list[RetrievedChunk] = []
        lexical_error: Exception | None = None
        dense_error: Exception | None = None
        try:
            lexical = self._bm25.search(query, top_k=top_k, scope=resolved_scope)
        except Exception as exc:  # BM25 is in-process; unexpected errors still isolated
            lexical_error = exc
            logger.exception("bm25_failed")
        try:
            dense = await self._vector.search(query, top_k=top_k, scope=resolved_scope)
        except Exception as exc:
            dense_error = exc
            logger.exception("vector_search_failed")
        if lexical_error is not None and dense_error is not None:
            raise RetrievalError("both lexical and dense retrievers failed") from dense_error
        degraded = lexical_error is not None or dense_error is not None
        if degraded and not lexical and not dense:
            raise RetrievalError("degraded retrieval produced no evidence") from (
                dense_error or lexical_error
            )
        fused = reciprocal_rank_fusion([lexical, dense], k=self._rrf_k)
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "hybrid_retrieval_completed",
            extra={
                "top_k": top_k,
                "lexical": len(lexical),
                "dense": len(dense),
                "fused": len(fused),
                "latency_ms": round(elapsed_ms, 2),
            },
        )
        trimmed = fused[:top_k]
        if self._cache is not None and not degraded:
            await self._cache.set(
                query,
                top_k,
                trimmed,
                scope=resolved_scope,
                revision=self._corpus_revision,
            )
        return trimmed
