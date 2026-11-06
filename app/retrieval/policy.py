"""Policy metadata, applicability rules and corpus versioning."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.config.policy_profiles import MANAGED_POLICY_PREFIXES, get_policy_profile
from app.core.enums import DocumentCategory
from app.schemas.retrieval import (
    PolicyBrand,
    PolicyMarket,
    PolicyScope,
    RetrievedChunk,
    ReviewQueryRule,
)

_ALLOWED_SOURCE_HOSTS = {
    "au": {"au.koala.com", "au-koala.gorgias.help"},
    "us": {"us.koala.com", "koala-faqs.gorgias.help"},
}


class PolicyDocument(BaseModel):
    model_config = {"extra": "forbid"}

    path: str
    title: str = Field(min_length=1, max_length=200)
    brand: PolicyBrand = "koala"
    market: PolicyMarket
    category: DocumentCategory
    product_categories: list[str] = Field(min_length=1)
    policy_version: str = Field(min_length=1, max_length=100)
    retrieved_at: date
    effective_from: date | None = None
    effective_to: date | None = None
    status: str = "demo"
    requires_human_review: bool = False
    review_sections: list[str] = Field(default_factory=lambda: ["人工复核"])
    review_query_rules: list[ReviewQueryRule] = Field(default_factory=list)
    source_type: Literal["external_public", "internal_demo"] = "external_public"
    source_label: str = ""
    source_path: str = ""
    source_urls: list[str] = Field(default_factory=list)
    source_updated: str = ""

    @field_validator("brand")
    @classmethod
    def validate_brand(cls, value: PolicyBrand) -> PolicyBrand:
        if value not in {"koala", "epig"}:
            raise ValueError("policy manifest entries must identify a managed policy pack")
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in {"demo", "approved"}:
            raise ValueError("policy status must be demo or approved")
        return value

    @model_validator(mode="after")
    def validate_sources_and_scope(self) -> PolicyDocument:
        if not self.path.startswith(f"{self.brand}/{self.market}/"):
            raise ValueError("policy document path must match its declared brand and market")
        if self.brand == "koala":
            self._validate_legacy_koala_source()
        else:
            self._validate_epig_demo_source()
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("policy effective_to cannot be earlier than effective_from")
        if self.requires_human_review and not self.review_sections:
            raise ValueError("review policy documents must declare review_sections")
        if any(not section.strip() for section in self.review_sections):
            raise ValueError("review section names cannot be blank")
        if self.requires_human_review and not self.review_query_rules:
            raise ValueError("review policy documents must declare review_query_rules")
        if not self.requires_human_review and self.review_query_rules:
            raise ValueError("non-review policy documents cannot declare review_query_rules")
        return self

    def _validate_legacy_koala_source(self) -> None:
        if self.market not in _ALLOWED_SOURCE_HOSTS:
            raise ValueError("only verified AU/US Koala policy documents may be ingested")
        if self.source_type != "external_public" or not self.source_urls:
            raise ValueError("Koala public summaries require external_public source URLs")
        for value in self.source_urls:
            parsed = urlparse(value)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in _ALLOWED_SOURCE_HOSTS[self.market]
            ):
                raise ValueError(f"unapproved Koala policy source: {value}")

    def _validate_epig_demo_source(self) -> None:
        if self.market not in {"au", "us", "sg"}:
            raise ValueError("E-pig demo policies require AU, US or SG market scope")
        if self.status != "demo":
            raise ValueError("E-pig internal policies must remain demo status")
        if self.source_type != "internal_demo" or self.source_urls:
            raise ValueError("E-pig demo policies cannot claim external source URLs")
        if not self.source_label.strip() or not self.source_path.strip():
            raise ValueError("E-pig demo policies require an internal source label and path")
        if self.source_path != self.path:
            raise ValueError("E-pig demo source_path must match the actual policy path")

    def chunk_fields(self, section: str | None) -> dict:
        requires_review = self.requires_human_review and section in self.review_sections
        return {
            "brand": self.brand,
            "market": self.market,
            "product_categories": self.product_categories,
            "retrieved_at": self.retrieved_at,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "policy_status": self.status,
            "requires_human_review": requires_review,
            "review_query_rules": self.review_query_rules if requires_review else [],
            "source_type": self.source_type,
            "source_label": self.source_label,
            "source_path": self.source_path,
            "source_urls": self.source_urls,
            "source_updated": self.source_updated,
        }


class PolicyManifest(BaseModel):
    schema_version: int = 1
    documents: list[PolicyDocument]

    @model_validator(mode="after")
    def validate_documents(self) -> PolicyManifest:
        paths = [document.path for document in self.documents]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate policy manifest paths")
        for document in self.documents:
            path = Path(document.path)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not document.path.startswith(MANAGED_POLICY_PREFIXES)
            ):
                raise ValueError(f"unsafe policy manifest path: {document.path}")
            if path.suffix.lower() != ".md":
                raise ValueError(f"managed policy documents must be Markdown: {document.path}")
        return self


def load_policy_manifest(knowledge_dir: Path) -> dict[str, PolicyDocument]:
    path = knowledge_dir / "policy-manifest.json"
    if not path.exists():
        return {}
    try:
        manifest = PolicyManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid policy manifest: {path}") from exc
    return {document.path: document for document in manifest.documents}


def chunk_matches_scope(chunk: RetrievedChunk, scope: PolicyScope) -> bool:
    if chunk.brand != scope.brand or chunk.market != scope.market:
        return False
    profile = get_policy_profile(chunk.brand)
    if profile.requires_category and not scope.product_category:
        return False
    if scope.product_category and scope.product_category not in chunk.product_categories:
        return False
    if scope.as_of is None or not profile.filter_dates:
        return True
    if chunk.retrieved_at is None:
        return False
    # A current snapshot cannot establish the terms of an earlier order.
    applicable_from = chunk.effective_from or chunk.retrieved_at
    if scope.as_of < applicable_from:
        return False
    return chunk.effective_to is None or scope.as_of <= chunk.effective_to


def applicable_from(chunk: RetrievedChunk) -> str:
    value = chunk.effective_from or chunk.retrieved_at
    return value.isoformat() if value else ""


def chunk_document_topic(chunk: RetrievedChunk) -> str:
    """Return the manifest file stem a chunk belongs to.

    ``source_path`` is server-derived for managed policies; legacy corpus
    chunks fall back to the stable ``document_id`` prefix.
    """
    if chunk.source_path:
        name = chunk.source_path.rsplit("/", 1)[-1]
        if name.endswith(".md"):
            name = name[:-3]
        return name
    document_id = chunk.document_id
    return document_id.rsplit("_", 1)[0] if "_" in document_id else document_id


@dataclass(frozen=True)
class PolicyTopicHint:
    """Deterministic query topic that justifies one bounded policy recall."""

    topic: str
    query: str
    document_topic: str


@dataclass(frozen=True)
class _PolicyTopicRule:
    hint: PolicyTopicHint
    pattern: re.Pattern[str]


_POLICY_TOPIC_RULES: tuple[_PolicyTopicRule, ...] = (
    _PolicyTopicRule(
        hint=PolicyTopicHint(
            topic="missing_parts",
            # Exact Chinese retrieval hints. The policy chunk that answers this
            # topic is the missing/wrong/damaged materials list, not delivery.
            query="缺件 少件 漏发 漏寄 材料清单 包装清单 全部包材 缺少部件",
            document_topic="faults-missing",
        ),
        # Exclude 减少一... so "少等一会儿" and ordinary reduction wording stay quiet.
        pattern=re.compile(
            r"(?<!减)(?:缺件|少件|漏发|漏寄|缺少(?:部件|零件|配件|螺丝|包|件)"
            r"|少(?:了)?[一二两三四五六七八九十几\d]|missing\s+(?:part|item))",
            re.IGNORECASE,
        ),
    ),
    _PolicyTopicRule(
        hint=PolicyTopicHint(
            topic="wrong_item",
            query="错发 发错 实收商品 原订商品 材料清单 包装清单 全部包材",
            document_topic="faults-missing",
        ),
        pattern=re.compile(r"错发|发错|wrong\s+(?:item|product)", re.IGNORECASE),
    ),
    _PolicyTopicRule(
        hint=PolicyTopicHint(
            topic="transport_damage",
            query="运输损坏 破损 压坏 受损部位近照 包装标签照片 材料清单",
            document_topic="faults-missing",
        ),
        pattern=re.compile(r"破损|损坏|压坏|裂|damaged|crushed", re.IGNORECASE),
    ),
)

_MAX_POLICY_TOPIC_SUPPLEMENTS = 2


def policy_topic_hints(query: str) -> tuple[PolicyTopicHint, ...]:
    """Return deterministic recall hints for logistics anomaly topics.

    The hints do not depend on model intent classification or rewriting, so a
    query like "椅子拆箱少了一包螺丝" reliably reaches the faults-missing
    materials list even when hybrid ranking prefers the delivery document.
    """
    return tuple(rule.hint for rule in _POLICY_TOPIC_RULES if rule.pattern.search(query))


def merge_policy_topic_chunks(
    chunks: list[RetrievedChunk],
    topic_chunks: list[RetrievedChunk],
    *,
    limit: int = _MAX_POLICY_TOPIC_SUPPLEMENTS,
) -> list[RetrievedChunk]:
    """Append missing topic evidence without dropping or duplicating results."""
    if limit <= 0:
        return list(chunks)
    merged = list(chunks)
    existing = {chunk.chunk_id for chunk in chunks}
    appended = 0
    for chunk in topic_chunks:
        if appended >= limit:
            break
        if chunk.chunk_id in existing:
            continue
        merged.append(chunk)
        existing.add(chunk.chunk_id)
        appended += 1
    return merged


def _normalize_review_text(value: str) -> str:
    return "".join(value.casefold().split())


def _review_rule_matches_query(normalized_query: str, rule: ReviewQueryRule) -> bool:
    return all(
        any(_normalize_review_text(term) in normalized_query for term in alternatives)
        for alternatives in rule.all_of
    )


def review_chunk_matches_query(query: str, chunk: RetrievedChunk) -> bool:
    if not chunk.requires_human_review:
        return False
    if not chunk.review_query_rules:
        return True
    normalized_query = _normalize_review_text(query)
    return any(
        _review_rule_matches_query(normalized_query, rule) for rule in chunk.review_query_rules
    )


def policy_review_chunks(query: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Return review evidence relevant to the query across every cited document."""
    # Retrieval rank is not evidence that an exception applies. Chunks without
    # structured rules retain the legacy fail-closed behavior in the matcher above.
    return [chunk for chunk in chunks if review_chunk_matches_query(query, chunk)]


def merge_policy_review_chunks(
    chunks: list[RetrievedChunk], review_chunks: list[RetrievedChunk]
) -> list[RetrievedChunk]:
    """Keep ranked review evidence first and append matches lost to truncation."""
    review_ids = {chunk.chunk_id for chunk in review_chunks}
    retrieved_ids = {chunk.chunk_id for chunk in chunks}
    return [
        *(chunk for chunk in chunks if chunk.chunk_id in review_ids),
        *(chunk for chunk in review_chunks if chunk.chunk_id not in retrieved_ids),
        *(chunk for chunk in chunks if chunk.chunk_id not in review_ids),
    ]


def policy_review_required(query: str, chunks: list[RetrievedChunk]) -> bool:
    return bool(policy_review_chunks(query, chunks))


def policy_review_topics(query: str, chunks: list[RetrievedChunk]) -> tuple[str, ...]:
    """Return the structured review topics the query actually matches.

    Topics are de-duplicated and kept in first-match order so callers can
    produce deterministic, bounded handoff guidance. Chunks without structured
    rules still fail closed in :func:`review_chunk_matches_query`, but they do
    not invent a topic.
    """
    normalized_query = _normalize_review_text(query)
    matched: dict[str, None] = {}
    for chunk in chunks:
        if not chunk.requires_human_review:
            continue
        for rule in chunk.review_query_rules:
            if _review_rule_matches_query(normalized_query, rule):
                matched.setdefault(rule.topic, None)
    return tuple(matched)


def corpus_fingerprint(chunks: list[RetrievedChunk]) -> str:
    payload = [
        {
            "chunk_id": chunk.chunk_id,
            "document_id": chunk.document_id,
            "title": chunk.title,
            "content": chunk.content,
            "section": chunk.section,
            "category": chunk.category.value,
            "policy_version": chunk.policy_version,
            "brand": chunk.brand,
            "market": chunk.market,
            "product_categories": sorted(chunk.product_categories),
            "policy_status": chunk.policy_status,
            "requires_human_review": chunk.requires_human_review,
            "review_query_rules": [rule.model_dump() for rule in chunk.review_query_rules],
            "source_type": chunk.source_type,
            "source_label": chunk.source_label,
            "source_path": chunk.source_path,
            "source_urls": chunk.source_urls,
            "source_updated": chunk.source_updated,
            "retrieved_at": chunk.retrieved_at.isoformat() if chunk.retrieved_at else None,
            "effective_from": (chunk.effective_from.isoformat() if chunk.effective_from else None),
            "effective_to": chunk.effective_to.isoformat() if chunk.effective_to else None,
        }
        for chunk in sorted(chunks, key=lambda item: item.chunk_id)
    ]
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
