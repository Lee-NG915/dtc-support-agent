"""Knowledge-base ingestion into BM25 and the vector store."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.config.policy_profiles import MANAGED_POLICY_PREFIXES
from app.config.settings import Settings, get_settings
from app.core.enums import DocumentCategory
from app.providers.base import EmbeddingProvider
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.chunking import document_id_from_path
from app.retrieval.cleaning import dedupe_chunks
from app.retrieval.policy import load_policy_manifest
from app.retrieval.unstructured_parser import list_knowledge_files, parse_path
from app.retrieval.vector import (
    InMemoryVectorStore,
    QdrantVectorStore,
    VectorRecord,
    build_records,
)
from app.schemas.retrieval import RetrievedChunk

# Prefix rules are ordered longest-intent first. Unknown stems fall back to FAQ.
_CATEGORY_PREFIXES: tuple[tuple[str, DocumentCategory], ...] = (
    ("return_", DocumentCategory.RETURN_POLICY),
    ("exchange_", DocumentCategory.RETURN_POLICY),
    ("missing_", DocumentCategory.RETURN_POLICY),
    ("refund_", DocumentCategory.RETURN_POLICY),
    ("shipping", DocumentCategory.SHIPPING),
    ("logistics", DocumentCategory.SHIPPING),
    ("pickup", DocumentCategory.SHIPPING),
    ("warranty", DocumentCategory.WARRANTY),
    ("repair", DocumentCategory.WARRANTY),
    ("coupon", DocumentCategory.COUPON),
    ("promo", DocumentCategory.COUPON),
    ("gift_card", DocumentCategory.COUPON),
    ("installment", DocumentCategory.PAYMENT),
    ("size_", DocumentCategory.PRODUCT),
    ("product_", DocumentCategory.PRODUCT),
    ("install", DocumentCategory.PRODUCT),
    ("payment", DocumentCategory.PAYMENT),
    ("invoice", DocumentCategory.PAYMENT),
    ("account", DocumentCategory.ACCOUNT),
    ("privacy", DocumentCategory.ACCOUNT),
    ("membership", DocumentCategory.ACCOUNT),
    ("points_", DocumentCategory.ACCOUNT),
    ("complaint", DocumentCategory.AFTERSALE),
    ("handoff", DocumentCategory.AFTERSALE),
    ("aftersale", DocumentCategory.AFTERSALE),
    ("live_", DocumentCategory.COUPON),
    ("cross_border", DocumentCategory.SHIPPING),
    ("restock", DocumentCategory.PRODUCT),
    ("packaging", DocumentCategory.PRODUCT),
    ("price_protect", DocumentCategory.AFTERSALE),
    ("faq", DocumentCategory.FAQ),
)


def category_for_stem(stem: str) -> DocumentCategory:
    lowered = stem.lower()
    for prefix, category in _CATEGORY_PREFIXES:
        if lowered == prefix.rstrip("_") or lowered.startswith(prefix):
            return category
    return DocumentCategory.FAQ


class KnowledgeIngestor:
    def __init__(
        self,
        knowledge_dir: str,
        embedder: EmbeddingProvider,
        settings: Settings | None = None,
    ) -> None:
        self._knowledge_dir = Path(knowledge_dir)
        self._embedder = embedder
        self._settings = settings or get_settings()

    async def load(self) -> tuple[list[RetrievedChunk], list[VectorRecord]]:
        files = list_knowledge_files(self._knowledge_dir)
        policy_documents = load_policy_manifest(self._knowledge_dir)
        matched_manifest_paths: set[str] = set()
        chunks: list[RetrievedChunk] = []
        for path in files:
            relative_path = path.relative_to(self._knowledge_dir).as_posix()
            policy = policy_documents.get(relative_path)
            if policy is None and relative_path.startswith(MANAGED_POLICY_PREFIXES):
                raise ValueError(
                    f"managed policy document lacks manifest metadata: {relative_path}"
                )
            if policy is not None:
                matched_manifest_paths.add(relative_path)
            category = policy.category if policy else category_for_stem(path.stem)
            document_id = document_id_from_path(path, root=self._knowledge_dir)
            title = policy.title if policy else path.stem
            file_chunks = await asyncio.to_thread(
                parse_path,
                path,
                document_id=document_id,
                title=title,
                category=category,
                settings=self._settings,
                policy_version=policy.policy_version if policy else "v1",
            )
            if policy is not None:
                if policy.requires_human_review:
                    sections = {chunk.section for chunk in file_chunks}
                    missing_sections = [
                        section for section in policy.review_sections if section not in sections
                    ]
                    if missing_sections:
                        raise ValueError(
                            f"policy review sections not found in {relative_path}: "
                            f"{missing_sections}"
                        )
                file_chunks = [
                    chunk.model_copy(update=policy.chunk_fields(chunk.section))
                    for chunk in file_chunks
                ]
            chunks.extend(file_chunks)
        missing = sorted(set(policy_documents) - matched_manifest_paths)
        if missing:
            raise ValueError(f"policy manifest references missing documents: {missing}")
        chunks = dedupe_chunks(chunks)
        if not chunks:
            return [], []
        embeddings = await self._embedder.embed([chunk.content for chunk in chunks])
        records = build_records(chunks, embeddings)
        return chunks, records


async def ingest_to_memory(
    knowledge_dir: str,
    embedder: EmbeddingProvider,
    settings: Settings | None = None,
) -> tuple[BM25Retriever, InMemoryVectorStore, list[RetrievedChunk]]:
    ingestor = KnowledgeIngestor(knowledge_dir, embedder, settings=settings)
    chunks, records = await ingestor.load()
    bm25 = BM25Retriever(chunks)
    store = InMemoryVectorStore()
    store.upsert(records)
    return bm25, store, chunks


async def ingest_knowledge(
    knowledge_dir: str,
    embedder: EmbeddingProvider,
    *,
    settings: Settings | None = None,
    qdrant_url: str = "",
    qdrant_collection: str = "cs_knowledge",
    embedding_dim: int = 512,
) -> tuple[BM25Retriever, InMemoryVectorStore | QdrantVectorStore, list[RetrievedChunk]]:
    """Prefer Qdrant when configured; fall back to memory if Qdrant is down."""
    logger = logging.getLogger(__name__)
    resolved = settings or get_settings()
    ingestor = KnowledgeIngestor(knowledge_dir, embedder, settings=resolved)
    chunks, records = await ingestor.load()
    if not chunks:
        raise ValueError("refusing to ingest an empty knowledge corpus")
    bm25 = BM25Retriever(chunks)
    if qdrant_url:
        store = QdrantVectorStore(qdrant_url, qdrant_collection, embedding_dim)
        try:
            await asyncio.to_thread(store.ensure_collection)
            await asyncio.to_thread(store.upsert, records)
            await asyncio.to_thread(store.remove_stale, {chunk.chunk_id for chunk in chunks})
            return bm25, store, chunks
        except Exception:
            logger.warning("qdrant_ingest_failed_using_memory")
    memory = InMemoryVectorStore()
    memory.upsert(records)
    return bm25, memory, chunks
