import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agents.nodes.generate import _extractive_fallback, generate_node, refuse_node
from app.agents.nodes.planner import Planner, planner_node
from app.agents.nodes.router import router_node
from app.core.enums import (
    DocumentCategory,
    IntentLabel,
    QueryComplexity,
    RetrievalStrategy,
)
from app.core.exceptions import RetrievalError
from app.providers.embeddings import HashEmbeddingProvider
from app.providers.fake import OfflineLLMProvider
from app.retrieval.cache import cache_key
from app.retrieval.cleaning import dedupe_chunks
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.ingest import KnowledgeIngestor, ingest_knowledge
from app.retrieval.policy import (
    PolicyDocument,
    chunk_matches_scope,
    corpus_fingerprint,
    policy_review_required,
    policy_review_topics,
)
from app.retrieval.vector import QdrantVectorStore
from app.schemas.agent import ComplexityResult, IntentResult
from app.schemas.chat import ChatRequest
from app.schemas.retrieval import (
    Citation,
    PolicyScope,
    RetrievedChunk,
    ReviewQueryRule,
    citation_from_chunk,
)
from app.tools.base import ToolContext
from app.tools.factory import build_tool_registry
from app.tools.knowledge_tools import GetWarrantyTool

SNAPSHOT_DATE = date(2026, 11, 5)
DEMO_DISCLOSURE = "内部自编演示政策，非官方或生产批准"


def _chunk(
    market: str = "au", *, review: bool = False, review_terms: list[str] | None = None
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"{market}_returns_00",
        document_id=f"{market}_returns",
        title=f"E-pig {market.upper()} returns",
        content="家具交付次日起 45 天内可申请退货；具体条件以演示条款为准。",
        brand="epig",
        market=market,
        category=DocumentCategory.RETURN_POLICY,
        product_categories=["furniture"],
        retrieved_at=SNAPSHOT_DATE,
        effective_from=SNAPSHOT_DATE,
        policy_status="demo",
        requires_human_review=review,
        review_query_rules=(
            [ReviewQueryRule(topic="test", all_of=[review_terms])] if review_terms else []
        ),
        source_type="internal_demo",
        source_label="E-pig 内部自编演示政策",
        source_path=f"epig/{market}/returns.md",
        source_urls=[],
        source_updated="内部自编演示，无外部来源",
    )


def _assert_demo_source(chunk: RetrievedChunk) -> None:
    assert chunk.source_type == "internal_demo"
    assert chunk.source_label
    assert chunk.source_path
    assert chunk.source_urls == []
    assert chunk.policy_status == "demo"


def _manifest_entry() -> dict:
    return {
        "path": "epig/au/returns.md",
        "title": "E-pig AU returns",
        "brand": "epig",
        "market": "au",
        "category": "return_policy",
        "product_categories": ["furniture"],
        "policy_version": "demo-2026-11-05",
        "retrieved_at": "2026-11-05",
        "effective_from": "2026-11-05",
        "status": "demo",
        "source_type": "internal_demo",
        "source_label": "E-pig 内部自编演示政策",
        "source_path": "epig/au/returns.md",
        "source_urls": [],
        "source_updated": "内部自编演示，无外部来源",
    }


def test_epig_scope_requires_explicit_market() -> None:
    with pytest.raises(ValidationError, match="explicit supported market"):
        PolicyScope(brand="epig")
    with pytest.raises(ValidationError, match="global demo scope"):
        PolicyScope(brand="upstream_demo", market="au")
    with pytest.raises(ValidationError, match="explicit supported market"):
        ChatRequest(message="可以退货吗", brand="epig")


def test_epig_scope_rejects_future_policy_date() -> None:
    tomorrow = date.today() + timedelta(days=1)
    with pytest.raises(ValidationError, match="future policy date"):
        PolicyScope(
            brand="epig",
            market="au",
            as_of=tomorrow,
            product_category="furniture",
        )


def test_epig_scope_defaults_direct_retrieval_to_today() -> None:
    scope = PolicyScope(brand="epig", market="au", product_category="furniture")
    assert scope.as_of == date.today()


def test_scope_filters_brand_market_product_and_snapshot_date() -> None:
    chunk = _chunk()
    au = PolicyScope(brand="epig", market="au", as_of=SNAPSHOT_DATE, product_category="furniture")
    assert chunk_matches_scope(chunk, au)
    assert not chunk_matches_scope(
        chunk, PolicyScope(brand="epig", market="au", as_of=SNAPSHOT_DATE)
    )
    assert not chunk_matches_scope(
        chunk, PolicyScope(brand="epig", market="us", as_of=SNAPSHOT_DATE)
    )
    assert not chunk_matches_scope(chunk, PolicyScope())
    assert not chunk_matches_scope(
        chunk,
        PolicyScope(brand="epig", market="au", as_of=SNAPSHOT_DATE, product_category="mattress"),
    )
    assert not chunk_matches_scope(
        chunk, PolicyScope(brand="epig", market="au", as_of=date(2026, 11, 4))
    )
    assert not chunk_matches_scope(
        chunk,
        PolicyScope(brand="epig", market="au", as_of=SNAPSHOT_DATE, product_category="electronics"),
    )


def test_cache_key_separates_policy_scope_and_corpus_revision() -> None:
    au = PolicyScope(brand="epig", market="au", as_of=SNAPSHOT_DATE, product_category="furniture")
    us = PolicyScope(brand="epig", market="us", as_of=SNAPSHOT_DATE)
    legacy = PolicyScope()
    assert cache_key("returns", 4, scope=au) != cache_key("returns", 4, scope=us)
    assert cache_key("returns", 4, scope=au) != cache_key("returns", 4, scope=legacy)
    assert cache_key("returns", 4, scope=au, revision="one") != cache_key(
        "returns", 4, scope=au, revision="two"
    )


def test_exact_duplicate_text_is_retained_for_different_markets() -> None:
    chunks = [_chunk("au"), _chunk("us"), _chunk("sg")]
    assert len(dedupe_chunks(chunks)) == 3


def test_exact_duplicate_text_retains_distinct_review_and_date_semantics() -> None:
    ordinary = _chunk("au")
    review = ordinary.model_copy(
        update={
            "chunk_id": "au_returns_review",
            "requires_human_review": True,
            "review_query_rules": [ReviewQueryRule(topic="condition", all_of=[["定制"]])],
        }
    )
    later = ordinary.model_copy(
        update={"chunk_id": "au_returns_later", "effective_from": date(2026, 11, 6)}
    )
    assert len(dedupe_chunks([ordinary, review, later])) == 3


def test_epig_manifest_requires_demo_internal_source_without_urls() -> None:
    document = PolicyDocument(**_manifest_entry())
    assert document.status == "demo"
    assert document.source_type == "internal_demo"
    assert document.source_path == document.path


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"status": "approved"}, "must remain demo status"),
        (
            {"source_urls": ["https://example.com/returns"]},
            "cannot claim external source URLs",
        ),
        ({"source_type": "external_public"}, "cannot claim external source URLs"),
        ({"source_label": "   "}, "require an internal source label and path"),
        ({"source_path": ""}, "require an internal source label and path"),
        ({"source_path": "epig/au/other.md"}, "source_path must match"),
        ({"path": "epig/us/returns.md"}, "path must match"),
        (
            {"effective_from": "2026-12-01", "effective_to": "2026-11-01"},
            "effective_to cannot be earlier",
        ),
        (
            {
                "market": "global",
                "path": "epig/global/returns.md",
                "source_path": "epig/global/returns.md",
            },
            "require AU, US or SG",
        ),
    ],
)
def test_epig_manifest_rejects_invalid_source_claims(update: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        PolicyDocument(**(_manifest_entry() | update))


def test_legacy_koala_chunk_and_citation_payloads_default_to_legacy_source() -> None:
    chunk_payload = {
        "chunk_id": "legacy_koala_au_returns_00",
        "document_id": "koala/au/returns",
        "title": "Legacy Koala AU returns",
        "content": "旧 Koala payload，仅用于反序列化兼容。",
        "brand": "koala",
        "market": "au",
        "category": "return_policy",
        "source_urls": ["https://au.koala.com/pages/120-day-trial"],
    }
    chunk = RetrievedChunk.model_validate(chunk_payload)
    assert chunk.source_type == "legacy"
    assert chunk.source_label == ""
    assert chunk.source_path == ""
    citation = Citation.model_validate(
        {
            "document_id": chunk.document_id,
            "title": chunk.title,
            "chunk_id": chunk.chunk_id,
            "brand": "koala",
            "market": "au",
            "source_urls": chunk.source_urls,
        }
    )
    assert citation.source_type == "legacy"
    assert citation.source_label == ""
    assert citation.source_path == ""
    assert citation_from_chunk(chunk).source_type == "legacy"


def test_qdrant_legacy_payload_deserializes_with_legacy_source_defaults() -> None:
    legacy_payload = {
        "chunk_id": "legacy_koala_au_returns_00",
        "document_id": "koala/au/returns",
        "title": "Legacy Koala AU returns",
        "content": "旧 Qdrant payload，仅用于兼容测试。",
        "category": "return_policy",
        "brand": "koala",
        "market": "au",
        "source_urls": ["https://au.koala.com/pages/120-day-trial"],
    }

    class _LegacyClient:
        def search(self, **kwargs):
            return [SimpleNamespace(id="legacy-point", payload=legacy_payload, score=0.5)]

    store = QdrantVectorStore("http://unused", "test", 8)
    store._ensure_client = _LegacyClient  # type: ignore[method-assign]
    chunks = store.search([0.0] * 8, top_k=1)
    assert len(chunks) == 1
    assert chunks[0].brand == "koala"
    assert chunks[0].source_type == "legacy"
    assert chunks[0].source_label == ""
    assert chunks[0].source_path == ""


def test_corpus_fingerprint_covers_citation_source_and_review_metadata() -> None:
    chunk = _chunk(review=True, review_terms=["污渍"])
    revision = corpus_fingerprint([chunk])
    for update in (
        {"title": "changed"},
        {"section": "人工复核"},
        {"source_label": "changed"},
        {"source_path": "epig/au/other.md"},
        {"source_type": "legacy"},
        {"policy_status": "approved"},
        {"review_query_rules": [ReviewQueryRule(topic="condition", all_of=[["破损"]])]},
    ):
        assert corpus_fingerprint([chunk.model_copy(update=update)]) != revision


def test_review_uses_compound_topic_match_across_documents() -> None:
    ordinary = _chunk()
    exception = _chunk(review=True, review_terms=["最低试用"])
    exception.chunk_id = "au_returns_review"
    assert not policy_review_required("正价家具多少天内可以退货", [ordinary, exception])
    assert policy_review_required("床垫最低试用多久", [ordinary, exception])
    assert not policy_review_required("释义未命中词表", [exception])
    assert policy_review_required("任何例外问题", [_chunk(review=True)])
    regional = _chunk("sg", review=True, review_terms=["偏远"])
    assert policy_review_required("偏远地址能配送吗", [regional])
    damaged_refund = ReviewQueryRule(
        topic="acceptance_or_amount_dispute", all_of=[["破损", "污渍"], ["退款", "退货"]]
    )
    other_document = exception.model_copy(
        update={"document_id": "refunds", "review_query_rules": [damaged_refund]}
    )
    assert not policy_review_required("正常退款多久到账", [ordinary, other_document])
    assert policy_review_required("破损家具退货后多久能退款", [ordinary, other_document])


def test_policy_review_topics_lists_only_matching_structured_rules() -> None:
    fee = ReviewQueryRule(topic="service_fee_refund", all_of=[["配送费", "运费"], ["退"]])
    payment = ReviewQueryRule(topic="payment_refund_exception", all_of=[["原卡注销"], ["退款"]])
    chunk = _chunk(review=True).model_copy(update={"review_query_rules": [fee, payment]})
    legacy_review = _chunk(review=True)

    assert policy_review_topics("配送费能退吗", [chunk]) == ("service_fee_refund",)
    assert policy_review_topics("原卡注销后退款怎么办", [chunk]) == ("payment_refund_exception",)
    assert policy_review_topics("配送费和原卡注销的退款", [chunk]) == (
        "service_fee_refund",
        "payment_refund_exception",
    )
    assert policy_review_topics("普通退货运费", [_chunk(), chunk]) == ("service_fee_refund",)
    # A rule-free review chunk still fails closed, but it cannot invent a topic.
    assert policy_review_topics("任何问题", [legacy_review]) == ()


def test_review_rules_reject_blank_terms_and_require_all_topic_groups() -> None:
    with pytest.raises(ValidationError, match="cannot be empty or blank"):
        ReviewQueryRule(topic="bad", all_of=[["   "]])
    with pytest.raises(ValidationError, match="cannot be empty or blank"):
        ReviewQueryRule(topic="bad", all_of=[[]])
    rule = ReviewQueryRule(
        topic="used_return", all_of=[["已使用", "用过", "污渍"], ["退货", "退回", "退"]]
    )
    chunk = _chunk(review=True).model_copy(update={"review_query_rules": [rule]})
    ordinary = _chunk()
    assert not policy_review_required("退货至少需要准备什么材料", [ordinary, chunk])
    assert not policy_review_required("床垫最少多少钱", [ordinary, chunk])
    assert policy_review_required("用过的沙发能不能退货", [ordinary, chunk])


@pytest.mark.asyncio
async def test_review_only_evidence_without_topic_match_fails_closed() -> None:
    chunk = _chunk(review=True, review_terms=["定制"])
    result = await generate_node(
        {
            "user_query": "普通家具退货需要哪些材料",
            "policy_scope": PolicyScope(
                brand="epig", market="au", as_of=SNAPSHOT_DATE, product_category="furniture"
            ),
            "strategy": RetrievalStrategy.SINGLE,
            "retrieved_chunks": [chunk],
            "tool_results": [],
        },
        OfflineLLMProvider(),
    )
    assert result["refused"] is True
    assert result["citations"] == []
    assert result["retrieved_chunks"] == []
    assert "没有可用的政策证据" in result["final_answer"]
    assert DEMO_DISCLOSURE in result["final_answer"]


@pytest.mark.asyncio
async def test_sg_has_normal_demo_answer_with_sg_citation() -> None:
    chunk = _chunk("sg")
    result = await generate_node(
        {
            "user_query": "新加坡家具可以退货吗",
            "policy_scope": PolicyScope(
                brand="epig", market="sg", as_of=SNAPSHOT_DATE, product_category="furniture"
            ),
            "strategy": RetrievalStrategy.SINGLE,
            "retrieved_chunks": [chunk],
            "tool_results": [],
        },
        OfflineLLMProvider(),
    )
    assert result["refused"] is False
    assert result["citations"][0].market == "sg"
    assert DEMO_DISCLOSURE in result["final_answer"]


@pytest.mark.asyncio
async def test_matching_demo_exception_requires_human_review() -> None:
    chunk = _chunk(review=True)
    result = await generate_node(
        {
            "user_query": "床垫最少试用几天才能退",
            "policy_scope": PolicyScope(
                brand="epig", market="au", as_of=SNAPSHOT_DATE, product_category="furniture"
            ),
            "strategy": RetrievalStrategy.SINGLE,
            "retrieved_chunks": [chunk],
            "tool_results": [],
        },
        OfflineLLMProvider(),
    )
    assert result["refused"] is True
    assert "人工" in result["final_answer"]
    assert "转人工前请先核对" not in result["final_answer"]
    assert DEMO_DISCLOSURE in result["final_answer"]
    citation = result["citations"][0]
    assert citation.source_type == "internal_demo"
    assert citation.source_label == chunk.source_label
    assert citation.source_path == chunk.source_path
    assert citation.source_urls == []
    assert citation.requires_human_review is True


class _NeverCalledLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("review branch must not call the model provider")


@pytest.mark.asyncio
async def test_review_guidance_is_deduplicated_bounded_and_model_free() -> None:
    rules = [
        ReviewQueryRule(topic=topic, all_of=[[term]])
        for topic, term in (
            ("service_fee_refund", "配送费"),
            ("payment_refund_exception", "原卡注销"),
            ("regional_delivery", "偏远"),
            ("special_delivery_access", "无电梯"),
            ("shipped_cancellation_or_change", "已发货"),
            ("accidental_damage", "意外损坏"),
            ("accidental_damage_warranty", "意外损坏"),
            ("injury_property_or_legal_liability", "受伤"),
            ("custom_or_final_sale_return", "定制"),
            ("partial_return_allocation", "部分退货"),
        )
    ]
    chunk = _chunk(review=True).model_copy(update={"review_query_rules": rules})
    provider = _NeverCalledLLM()

    result = await generate_node(
        {
            "user_query": "配送费 原卡注销 偏远 无电梯 已发货 意外损坏 受伤 定制 部分退货",
            "policy_scope": PolicyScope(
                brand="epig", market="au", as_of=SNAPSHOT_DATE, product_category="furniture"
            ),
            "strategy": RetrievalStrategy.SINGLE,
            "retrieved_chunks": [chunk],
            "tool_results": [],
        },
        provider,  # type: ignore[arg-type]
    )

    answer = result["final_answer"]
    check_lines = [line for line in answer.splitlines() if line.partition(". ")[0].isdigit()]
    boundary_lines = [line for line in answer.splitlines() if line.startswith("- ")]
    assert provider.calls == 0
    assert result["refused"] is True
    assert answer.count("转人工前请先核对") == 1
    assert len(check_lines) == 4
    assert len(boundary_lines) == 3
    assert len(check_lines) == len(set(check_lines))
    assert len(boundary_lines) == len(set(boundary_lines))
    assert all(citation.requires_human_review for citation in result["citations"])
    assert all(
        citation.brand == "epig" and citation.market == "au" for citation in result["citations"]
    )


@pytest.mark.asyncio
async def test_rule_free_review_chunk_keeps_generic_conservative_answer() -> None:
    provider = _NeverCalledLLM()
    result = await generate_node(
        {
            "user_query": "配送费能退吗",
            "policy_scope": PolicyScope(
                brand="epig", market="au", as_of=SNAPSHOT_DATE, product_category="furniture"
            ),
            "strategy": RetrievalStrategy.SINGLE,
            "retrieved_chunks": [_chunk(review=True)],
            "tool_results": [],
        },
        provider,  # type: ignore[arg-type]
    )

    assert provider.calls == 0
    assert result["refused"] is True
    assert "需人工核实，不能直接形成真实订单处理承诺" in result["final_answer"]
    assert "转人工前请先核对" not in result["final_answer"]
    assert DEMO_DISCLOSURE in result["final_answer"]
    assert len(result["citations"]) == 1
    assert result["citations"][0].requires_human_review is True


@pytest.mark.asyncio
async def test_koala_review_branch_keeps_generic_answer_without_guidance() -> None:
    chunk = _chunk(review=True, review_terms=["配送费"]).model_copy(
        update={"brand": "koala", "market": "au"}
    )
    provider = _NeverCalledLLM()
    result = await generate_node(
        {
            "user_query": "配送费能退吗",
            "policy_scope": PolicyScope(
                brand="koala", market="au", as_of=SNAPSHOT_DATE, product_category="furniture"
            ),
            "strategy": RetrievalStrategy.SINGLE,
            "retrieved_chunks": [chunk],
            "tool_results": [],
        },
        provider,  # type: ignore[arg-type]
    )

    assert provider.calls == 0
    assert result["refused"] is True
    assert "来源冲突或适用条件需要人工解释" in result["final_answer"]
    assert "转人工前请先核对" not in result["final_answer"]
    assert result["citations"][0].brand == "koala"
    assert result["citations"][0].market == "au"


@pytest.mark.asyncio
async def test_missing_product_category_fails_closed_even_on_direct_route() -> None:
    result = await generate_node(
        {
            "user_query": "床垫保修多少年",
            "intent": IntentResult(
                label=IntentLabel.FAQ_POLICY, confidence=0.9, prompt_version="test"
            ),
            "policy_scope": PolicyScope(brand="epig", market="us", as_of=SNAPSHOT_DATE),
            "strategy": RetrievalStrategy.DIRECT,
            "retrieved_chunks": [],
            "tool_results": [],
        },
        OfflineLLMProvider(),
    )
    assert result["refused"] is True
    assert result["citations"] == []
    assert "商品类别" in result["final_answer"]
    assert DEMO_DISCLOSURE in result["final_answer"]


@pytest.mark.asyncio
async def test_misclassified_policy_question_cannot_bypass_evidence_as_chitchat() -> None:
    result = await generate_node(
        {
            "user_query": "家具保修多少年",
            "intent": IntentResult(
                label=IntentLabel.CHITCHAT, confidence=1.0, prompt_version="test"
            ),
            "policy_scope": PolicyScope(
                brand="epig",
                market="au",
                as_of=SNAPSHOT_DATE,
                product_category="furniture",
            ),
            "strategy": RetrievalStrategy.DIRECT,
            "retrieved_chunks": [],
            "tool_results": [],
        },
        OfflineLLMProvider(),
    )
    assert result["refused"] is True
    assert result["citations"] == []
    assert "没有可用的政策证据" in result["final_answer"]


@pytest.mark.asyncio
async def test_deterministic_social_query_and_refusal_keep_demo_behavior() -> None:
    state = {
        "user_query": "谢谢",
        "intent": IntentResult(label=IntentLabel.CHITCHAT, confidence=1.0, prompt_version="test"),
        "policy_scope": PolicyScope(
            brand="epig", market="au", as_of=SNAPSHOT_DATE, product_category="furniture"
        ),
        "strategy": RetrievalStrategy.DIRECT,
        "retrieved_chunks": [],
        "tool_results": [],
    }
    social = await generate_node(state, OfflineLLMProvider())
    assert social["refused"] is False
    assert DEMO_DISCLOSURE in social["final_answer"]

    refused = await refuse_node(state)
    assert refused["refused"] is True
    assert DEMO_DISCLOSURE in refused["final_answer"]


@pytest.mark.asyncio
async def test_unrelated_exception_section_does_not_block_normal_direct_answer() -> None:
    normal = _chunk()
    exception = _chunk(review=True, review_terms=["污渍"])
    exception.chunk_id = "au_returns_review"
    result = await generate_node(
        {
            "user_query": "正价家具多少天内可以退货",
            "policy_scope": PolicyScope(
                brand="epig",
                market="au",
                as_of=SNAPSHOT_DATE,
                product_category="furniture",
            ),
            "strategy": RetrievalStrategy.DIRECT,
            "retrieved_chunks": [normal, exception],
            "tool_results": [],
        },
        OfflineLLMProvider(),
    )
    assert result["refused"] is False
    assert result["citations"][0].chunk_id == normal.chunk_id
    assert DEMO_DISCLOSURE in result["final_answer"]


class _DirectRouter:
    async def route(self, query: str, intent: IntentResult) -> ComplexityResult:
        return ComplexityResult(
            complexity=QueryComplexity.SIMPLE,
            strategy=RetrievalStrategy.DIRECT,
            confidence=1.0,
            rationale="model_direct",
        )


@pytest.mark.asyncio
async def test_router_forces_epig_policy_direct_route_to_retrieval() -> None:
    state = {
        "user_query": "家具保修多少年",
        "intent": IntentResult(label=IntentLabel.FAQ_POLICY, confidence=0.9, prompt_version="test"),
        "policy_scope": PolicyScope(
            brand="epig", market="au", as_of=SNAPSHOT_DATE, product_category="furniture"
        ),
    }
    result = await router_node(state, _DirectRouter())  # type: ignore[arg-type]
    assert result["strategy"] == RetrievalStrategy.SINGLE
    assert "epig_policy_requires_evidence" in result["complexity"].rationale

    state["intent"] = IntentResult(
        label=IntentLabel.GREETING, confidence=1.0, prompt_version="test"
    )
    misclassified = await router_node(state, _DirectRouter())  # type: ignore[arg-type]
    assert misclassified["strategy"] == RetrievalStrategy.SINGLE

    state["user_query"] = "你好"
    greeting = await router_node(state, _DirectRouter())  # type: ignore[arg-type]
    assert greeting["strategy"] == RetrievalStrategy.DIRECT


def test_empty_evidence_fallback_does_not_quote_scope_as_policy() -> None:
    answer = _extractive_fallback(
        "家具保修多少年", "政策范围：epig/au；日期：2026-11-05\n(无检索结果)", "(无)"
    )
    assert "政策范围" not in answer
    assert "无法基于现有信息" in answer


@pytest.mark.asyncio
async def test_offline_policy_answer_preserves_market_and_internal_source_citation() -> None:
    chunk = _chunk()
    result = await generate_node(
        {
            "user_query": "沙发退货政策",
            "policy_scope": PolicyScope(
                brand="epig", market="au", as_of=SNAPSHOT_DATE, product_category="furniture"
            ),
            "strategy": RetrievalStrategy.SINGLE,
            "retrieved_chunks": [chunk],
            "tool_results": [],
        },
        OfflineLLMProvider(),
    )
    citation = result["citations"][0]
    assert citation.market == "au"
    assert citation.brand == "epig"
    assert citation.source_type == "internal_demo"
    assert citation.source_label == chunk.source_label
    assert citation.source_path == chunk.source_path
    assert citation.source_urls == []
    assert DEMO_DISCLOSURE in result["final_answer"]


@pytest.mark.asyncio
async def test_generic_warranty_tool_cannot_answer_epig_sku() -> None:
    scope = PolicyScope(
        brand="epig", market="au", as_of=SNAPSHOT_DATE, product_category="furniture"
    )
    context = ToolContext(
        user_id="agent",
        session_id="session",
        trace_id="trace",
        policy_scope=scope,
    )
    result = await GetWarrantyTool().run({"sku": "EPIG-SOFA"}, context)
    assert result.success is False
    assert "policy search" in (result.error or "")


@pytest.mark.asyncio
async def test_epig_policy_planner_cannot_emit_generic_demo_writes() -> None:
    registry = build_tool_registry(None)  # type: ignore[arg-type]
    planner = Planner(OfflineLLMProvider(), registry)
    result = await planner_node(
        {
            "user_query": "ORD1001 我要退货并退款",
            "policy_scope": PolicyScope(
                brand="epig", market="au", as_of=SNAPSHOT_DATE, product_category="furniture"
            ),
        },
        planner,
    )
    assert result["pending_action"] is None
    assert [step.tool_name for step in result["plan"].steps] == ["search_knowledge"]


class _FailingLexical:
    def search(self, query: str, *, top_k: int, scope: PolicyScope):
        raise RuntimeError("lexical unavailable")


class _EmptyDense:
    async def search(self, query: str, *, top_k: int, scope: PolicyScope):
        return []


class _RecordingCache:
    def __init__(self) -> None:
        self.writes = 0

    async def get(self, *args, **kwargs):
        return None

    async def set(self, *args, **kwargs):
        self.writes += 1


@pytest.mark.asyncio
async def test_degraded_empty_hybrid_result_is_an_error_and_is_not_cached() -> None:
    cache = _RecordingCache()
    retriever = HybridRetriever(
        _FailingLexical(),  # type: ignore[arg-type]
        _EmptyDense(),  # type: ignore[arg-type]
        rrf_k=60,
        cache=cache,  # type: ignore[arg-type]
    )
    with pytest.raises(RetrievalError, match="degraded retrieval"):
        await retriever.retrieve("家具保修", top_k=4)
    assert cache.writes == 0


def test_qdrant_empty_cleanup_and_missing_category_fail_closed() -> None:
    store = QdrantVectorStore("http://unused", "test", 8)
    with pytest.raises(ValueError, match="empty corpus"):
        store.remove_stale(set())
    scope = PolicyScope(brand="epig", market="us", as_of=SNAPSHOT_DATE)
    assert store.search([], top_k=4, scope=scope) == []


@pytest.mark.asyncio
async def test_ingest_rejects_empty_corpus(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty knowledge corpus"):
        await ingest_knowledge(str(tmp_path), HashEmbeddingProvider(dim=8))


@pytest.mark.asyncio
async def test_ingest_rejects_missing_review_section(tmp_path: Path) -> None:
    policy_dir = tmp_path / "epig" / "au"
    policy_dir.mkdir(parents=True)
    (policy_dir / "returns.md").write_text(
        "# Demo\n\n> 演示\n\n## 正常条款\n\n45 天。\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": 1,
        "documents": [
            _manifest_entry()
            | {
                "requires_human_review": True,
                "review_sections": ["人工复核"],
                "review_query_rules": [{"topic": "condition", "all_of": [["污渍"], ["退"]]}],
            }
        ],
    }
    (tmp_path / "policy-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="review sections not found"):
        await KnowledgeIngestor(str(tmp_path), HashEmbeddingProvider(dim=8)).load()
