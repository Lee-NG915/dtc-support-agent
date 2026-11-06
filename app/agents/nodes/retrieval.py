"""Document retrieval node."""

from __future__ import annotations

import time

from app.agents.state import AgentState
from app.config.settings import Settings
from app.core.enums import RetrievalStrategy
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.policy import (
    merge_policy_review_chunks,
    merge_policy_topic_chunks,
    policy_topic_hints,
)
from app.retrieval.query_rewrite import QueryRewriter
from app.retrieval.reranker import DocumentReranker
from app.retrieval.self_rag import SelfRAGLoop
from app.schemas.common import LatencyBreakdown
from app.schemas.retrieval import PolicyScope, RetrievedChunk


async def retrieval_node(
    state: AgentState,
    *,
    retriever: HybridRetriever,
    reranker: DocumentReranker,
    rewriter: QueryRewriter,
    self_rag: SelfRAGLoop,
    settings: Settings,
) -> dict:
    started = time.perf_counter()
    query = state["user_query"]
    scope = state.get("policy_scope") or PolicyScope()
    strategy = state.get("strategy")
    top_k = (
        settings.retrieval_top_k_complex
        if strategy == RetrievalStrategy.MULTI
        else settings.retrieval_top_k_simple
    )
    if strategy == RetrievalStrategy.MULTI:
        chunks = await self_rag.retrieve(query, top_k=top_k, scope=scope)
        rewritten = [query]
    else:
        suggestions = await rewriter.rewrite(query)
        rewritten = list(dict.fromkeys([query, *suggestions]))[:2]
        by_id: dict[str, RetrievedChunk] = {}
        for search_query in rewritten:
            for candidate in await retriever.retrieve(search_query, top_k=top_k, scope=scope):
                by_id.setdefault(candidate.chunk_id, candidate)
        chunks = await reranker.rerank(query, list(by_id.values()), top_n=settings.rerank_top_n)
    review_chunks = retriever.matching_review_chunks(query, scope=scope)
    chunks = merge_policy_review_chunks(chunks, review_chunks)
    # Anomaly wording is matched deterministically, not via model intent or
    # rewriting. Each hint recalls only its trusted-scope document topic; the
    # merge appends a bounded, deduplicated supplement without disturbing the
    # ranked results or review evidence above.
    topic_chunks: list[RetrievedChunk] = []
    for hint in policy_topic_hints(query):
        topic_chunks.extend(
            retriever.matching_topic_chunks(
                hint.query,
                document_topic=hint.document_topic,
                scope=scope,
                limit=2,
            )
        )
    chunks = merge_policy_topic_chunks(chunks, topic_chunks)
    latency = state.get("latency") or LatencyBreakdown()
    latency.retrieval_ms = (time.perf_counter() - started) * 1000
    return {
        "retrieved_chunks": chunks,
        "rewritten_queries": rewritten,
        "latency": latency,
    }
