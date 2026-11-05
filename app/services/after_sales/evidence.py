"""Server-trusted SKU categories and immutable policy evidence snapshots.

Consumers and models never supply the category or policy version. The only
inputs are the current E-pig service order (SKU), explicit Settings mapping
(or the clearly labelled synthetic demo mapping) and the ingested corpus.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.config.settings import Settings
from app.core.exceptions import InvalidRequestError, StateConflictError
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.policy import chunk_matches_scope
from app.schemas.retrieval import PolicyScope, RetrievedChunk

_CHUNK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}$")
SYNTHETIC_SKU = "SYNTH-MUG-001"
SYNTHETIC_CATEGORY = "homewares"


@dataclass(frozen=True)
class TrustedCategory:
    sku: str
    category: str
    source: str

    def as_dict(self) -> dict[str, str]:
        return {"sku": self.sku, "category": self.category, "source": self.source}


@dataclass(frozen=True)
class EvidenceResult:
    chunks: list[dict[str, Any]] = field(default_factory=list)
    policy_version: str | None = None
    evidence_sha256: str = ""
    corpus_revision: str = ""
    as_of: date | None = None

    @property
    def is_empty(self) -> bool:
        return not self.chunks

    def as_snapshot(self) -> dict[str, Any]:
        return {
            "chunks": self.chunks,
            "policy_version": self.policy_version,
            "evidence_sha256": self.evidence_sha256,
            "corpus_revision": self.corpus_revision,
            "as_of": self.as_of.isoformat() if self.as_of is not None else None,
        }


class CategoryResolver:
    """Resolve SKU categories from explicit server config only."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(self, sku: str) -> TrustedCategory | None:
        mapping = self._settings.sku_category_map
        if sku in mapping:
            return TrustedCategory(sku=sku, category=mapping[sku], source="server_config")
        # Development-only synthetic convenience. It is explicitly labelled so
        # approval snapshots cannot mistake it for a production SKU mapping.
        if (
            self._settings.business_connector == "fake"
            and self._settings.app_env != "production"
            and sku == SYNTHETIC_SKU
        ):
            return TrustedCategory(
                sku=sku,
                category=SYNTHETIC_CATEGORY,
                source="synthetic_demo",
            )
        return None


class EvidenceResolver:
    def __init__(self, retriever: HybridRetriever | None) -> None:
        self._retriever = retriever

    @property
    def corpus_revision(self) -> str:
        return self._retriever.corpus_revision if self._retriever is not None else ""

    def resolve(
        self,
        *,
        market: str,
        chunk_ids: list[str],
        trusted_categories: set[str],
        require_category: bool,
        as_of: date | None = None,
    ) -> EvidenceResult:
        if not chunk_ids:
            return EvidenceResult(corpus_revision=self.corpus_revision, as_of=as_of)
        if require_category and not trusted_categories:
            raise StateConflictError("trusted item category is not available")

        normalized: list[str] = []
        for chunk_id in chunk_ids:
            if not _CHUNK_ID_RE.fullmatch(chunk_id):
                raise InvalidRequestError("invalid evidence chunk id")
            if chunk_id not in normalized:
                normalized.append(chunk_id)

        corpus = {
            chunk.chunk_id: chunk for chunk in (self._retriever.corpus if self._retriever else [])
        }
        selected: list[RetrievedChunk] = []
        for chunk_id in normalized:
            chunk = corpus.get(chunk_id)
            if chunk is None:
                raise InvalidRequestError("evidence chunk is not in the ingested corpus")
            if chunk.brand != "epig" or chunk.market != market:
                raise InvalidRequestError("evidence chunk does not belong to this market")
            if require_category and not _scope_compatible(chunk, market, trusted_categories, as_of):
                raise StateConflictError("policy evidence is not applicable as of the server date")
            selected.append(chunk)

        versions = {chunk.policy_version for chunk in selected}
        if len(versions) > 1:
            raise StateConflictError("selected policy evidence has conflicting versions")

        chunks = [_chunk_snapshot(chunk) for chunk in selected]
        payload = {
            "chunks": chunks,
            "policy_version": next(iter(versions), None),
            "corpus_revision": self.corpus_revision,
            "as_of": as_of.isoformat() if as_of is not None else None,
        }
        from app.services.after_sales.payload import sha256_json

        return EvidenceResult(
            chunks=chunks,
            policy_version=next(iter(versions), None),
            evidence_sha256=sha256_json(payload),
            corpus_revision=self.corpus_revision,
            as_of=as_of,
        )


def _scope_compatible(
    chunk: RetrievedChunk,
    market: str,
    trusted_categories: set[str],
    as_of: date | None,
) -> bool:
    for category in trusted_categories:
        scope = PolicyScope(
            brand="epig",
            market=market,  # type: ignore[arg-type]
            product_category=category,
            as_of=as_of,
        )
        if chunk_matches_scope(chunk, scope):
            return True
    return False


def _chunk_snapshot(chunk: RetrievedChunk) -> dict[str, Any]:
    from app.services.after_sales.payload import sha256_text

    return {
        "chunk_id": chunk.chunk_id,
        "document_id": chunk.document_id,
        "title": chunk.title,
        "section": chunk.section,
        "content": chunk.content,
        "policy_version": chunk.policy_version,
        "source_path": chunk.source_path,
        "source_label": chunk.source_label,
        "source_type": chunk.source_type,
        "policy_status": chunk.policy_status,
        "is_demo": chunk.policy_status == "demo",
        "product_categories": list(chunk.product_categories),
        "retrieved_at": chunk.retrieved_at.isoformat() if chunk.retrieved_at else None,
        "effective_from": (chunk.effective_from.isoformat() if chunk.effective_from else None),
        "effective_to": chunk.effective_to.isoformat() if chunk.effective_to else None,
        "content_sha256": sha256_text(chunk.content),
    }
