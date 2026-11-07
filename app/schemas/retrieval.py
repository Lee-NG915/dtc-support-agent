"""Retrieved chunk, policy-scope and citation schemas."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config.policy_profiles import get_policy_profile
from app.core.enums import DocumentCategory

PolicyBrand = Literal["upstream_demo", "koala", "epig"]
PolicyMarket = Literal["global", "au", "sg", "us"]
PolicySourceType = Literal["external_public", "internal_demo", "legacy"]


class PolicyScope(BaseModel):
    """Trusted business context used to filter every policy retrieval path."""

    brand: PolicyBrand = "upstream_demo"
    market: PolicyMarket = "global"
    as_of: date | None = None
    product_category: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_brand_market(self) -> PolicyScope:
        profile = get_policy_profile(self.brand)
        if self.market not in profile.allowed_markets:
            if self.brand == "upstream_demo":
                raise ValueError("upstream demo policies only support the global demo scope")
            raise ValueError(f"{self.brand} policy retrieval requires an explicit supported market")
        if profile.default_as_of_today and self.as_of is None:
            self.as_of = date.today()
        if profile.filter_dates and self.as_of and self.as_of > date.today():
            raise ValueError(f"{self.brand} policy retrieval cannot use a future policy date")
        return self

    def cache_payload(self) -> dict[str, str | None]:
        return {
            "brand": self.brand,
            "market": self.market,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "product_category": self.product_category,
        }


class ReviewQueryRule(BaseModel):
    """One review topic: every group must match, terms within a group are alternatives."""

    topic: str = Field(min_length=1, max_length=100)
    all_of: list[list[str]] = Field(min_length=1)

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("review rule topic cannot be blank")
        return value

    @field_validator("all_of")
    @classmethod
    def validate_term_groups(cls, groups: list[list[str]]) -> list[list[str]]:
        if any(not group or any(not term.strip() for term in group) for group in groups):
            raise ValueError("review rule terms and groups cannot be empty or blank")
        return groups


class RetrievedChunk(BaseModel):
    chunk_id: str
    document_id: str
    title: str
    content: str
    score: float = 0.0
    rank: int | None = None
    source: str = "hybrid"
    category: DocumentCategory = DocumentCategory.FAQ
    section: str | None = None
    chunk_index: int = 0
    policy_version: str = "v1"
    brand: PolicyBrand = "upstream_demo"
    market: PolicyMarket = "global"
    product_categories: list[str] = Field(default_factory=list)
    retrieved_at: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    policy_status: Literal["demo", "approved"] = "demo"
    requires_human_review: bool = False
    review_query_rules: list[ReviewQueryRule] = Field(default_factory=list)
    source_type: PolicySourceType = "legacy"
    source_label: str = ""
    source_path: str = ""
    source_urls: list[str] = Field(default_factory=list)
    source_updated: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)


class Citation(BaseModel):
    document_id: str
    title: str
    chunk_id: str
    section: str | None = None
    policy_version: str = "v1"
    brand: PolicyBrand = "upstream_demo"
    market: PolicyMarket = "global"
    retrieved_at: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    requires_human_review: bool = False
    source_type: PolicySourceType = "legacy"
    source_label: str = ""
    source_path: str = ""
    source_urls: list[str] = Field(default_factory=list)
    source_updated: str = ""


def citation_from_chunk(chunk: RetrievedChunk) -> Citation:
    return Citation(
        document_id=chunk.document_id,
        title=chunk.title,
        chunk_id=chunk.chunk_id,
        section=chunk.section,
        policy_version=chunk.policy_version,
        brand=chunk.brand,
        market=chunk.market,
        retrieved_at=chunk.retrieved_at,
        effective_from=chunk.effective_from,
        effective_to=chunk.effective_to,
        requires_human_review=chunk.requires_human_review,
        source_type=chunk.source_type,
        source_label=chunk.source_label,
        source_path=chunk.source_path,
        source_urls=chunk.source_urls,
        source_updated=chunk.source_updated,
    )


class RetrievalRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    category: DocumentCategory | None = None
    policy_scope: PolicyScope = Field(default_factory=PolicyScope)
