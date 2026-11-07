"""BM25 lexical retriever over a jieba-tokenised inverted index."""

from __future__ import annotations

from collections import defaultdict

from rank_bm25 import BM25Okapi

from app.retrieval.policy import (
    chunk_document_topic,
    chunk_matches_scope,
    review_chunk_matches_query,
)
from app.retrieval.tokenizer import tokenize
from app.schemas.retrieval import PolicyScope, RetrievedChunk


class BM25Retriever:
    """Lexical retriever. Complements dense search on exact policy numbers / SKUs."""

    def __init__(self, corpus: list[RetrievedChunk] | None = None) -> None:
        self._chunks: list[RetrievedChunk] = []
        self._bm25: BM25Okapi | None = None
        if corpus:
            self.build(corpus)

    @property
    def corpus(self) -> list[RetrievedChunk]:
        """Read-only view of the ingested corpus used for evidence binding."""
        return list(self._chunks)

    def build(self, corpus: list[RetrievedChunk]) -> None:
        self._chunks = list(corpus)
        tokenized = [tokenize(chunk.content) for chunk in self._chunks]
        # BM25Okapi requires a non-empty corpus; keep None if ingest has not run.
        self._bm25 = BM25Okapi(tokenized) if tokenized else None

    def search(
        self, query: str, *, top_k: int, scope: PolicyScope | None = None
    ) -> list[RetrievedChunk]:
        if self._bm25 is None or not self._chunks:
            return []
        resolved_scope = scope or PolicyScope()
        scores = self._bm25.get_scores(tokenize(query))
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        results: list[RetrievedChunk] = []
        for index in ranked:
            chunk = self._chunks[index]
            if not chunk_matches_scope(chunk, resolved_scope):
                continue
            # BM25 IDF can be negative on tiny corpora; still return the ranked docs.
            result = chunk.model_copy(deep=True)
            result.score = float(scores[index])
            result.rank = len(results) + 1
            result.source = "bm25"
            results.append(result)
            if len(results) >= top_k:
                break
        return results

    def matching_review_chunks(
        self, query: str, *, scope: PolicyScope | None = None
    ) -> list[RetrievedChunk]:
        """Match structured exception rules without depending on BM25 top_k."""
        resolved_scope = scope or PolicyScope()
        return [
            chunk.model_copy(deep=True)
            for chunk in self._chunks
            if chunk_matches_scope(chunk, resolved_scope)
            and review_chunk_matches_query(query, chunk)
        ]

    def matching_topic_chunks(
        self,
        query: str,
        *,
        document_topic: str,
        scope: PolicyScope | None = None,
        limit: int = 2,
    ) -> list[RetrievedChunk]:
        """Rank only the requested policy document within the trusted scope.

        This is the deterministic fallback for anomaly questions; it does not
        depend on dense ranking or model rewrites. Output is bounded and
        deduplicated by construction.
        """
        if self._bm25 is None or not self._chunks or limit <= 0:
            return []
        resolved_scope = scope or PolicyScope()
        scores = self._bm25.get_scores(tokenize(query))
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        results: list[RetrievedChunk] = []
        seen: set[str] = set()
        for index in ranked:
            chunk = self._chunks[index]
            if not chunk_matches_scope(chunk, resolved_scope):
                continue
            if chunk_document_topic(chunk) != document_topic:
                continue
            if chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id)
            result = chunk.model_copy(deep=True)
            result.score = float(scores[index])
            result.rank = len(results) + 1
            result.source = "policy-topic"
            results.append(result)
            if len(results) >= limit:
                break
        return results


class InvertedIndex:
    """Tiny helper used in tests to inspect token coverage."""

    def __init__(self, corpus: list[RetrievedChunk]) -> None:
        self.postings: dict[str, set[str]] = defaultdict(set)
        for chunk in corpus:
            for token in tokenize(chunk.content):
                self.postings[token].add(chunk.chunk_id)
