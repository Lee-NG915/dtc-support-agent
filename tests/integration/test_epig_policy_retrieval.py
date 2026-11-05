from datetime import date

import pytest

from app.agents.nodes.generate import generate_node
from app.core.enums import RetrievalStrategy
from app.providers.embeddings import HashEmbeddingProvider
from app.providers.fake import OfflineLLMProvider
from app.providers.reranker import LexicalRerankProvider
from app.retrieval.cache import MemoryRetrievalCache
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.ingest import ingest_to_memory
from app.retrieval.policy import (
    corpus_fingerprint,
    merge_policy_review_chunks,
    policy_review_required,
)
from app.retrieval.reranker import DocumentReranker
from app.retrieval.vector import VectorRetriever
from app.schemas.retrieval import PolicyScope
from app.tools.base import ToolContext
from app.tools.knowledge_tools import SearchKnowledgeTool

SNAPSHOT_DATE = date(2026, 11, 5)
DEMO_DISCLOSURE = "内部自编演示政策，非官方或生产批准"


async def _components():
    embedder = HashEmbeddingProvider(dim=512)
    bm25, store, chunks = await ingest_to_memory("knowledge", embedder)
    retriever = HybridRetriever(
        bm25,
        VectorRetriever(store, embedder),
        rrf_k=60,
        cache=MemoryRetrievalCache(ttl_seconds=60),
        corpus_revision=corpus_fingerprint(chunks),
    )
    return retriever, DocumentReranker(LexicalRerankProvider())


def _scope(market: str, *, as_of: date = SNAPSHOT_DATE, category: str | None = "furniture"):
    return PolicyScope(brand="epig", market=market, as_of=as_of, product_category=category)


async def _retrieve(retriever, reranker, query: str, scope: PolicyScope):
    candidates = await retriever.retrieve(query, top_k=8, scope=scope)
    chunks = await reranker.rerank(query, candidates, top_n=6)
    review_chunks = retriever.matching_review_chunks(query, scope=scope)
    return merge_policy_review_chunks(chunks, review_chunks)


async def _generate(query: str, scope: PolicyScope, chunks, *, strategy=RetrievalStrategy.SINGLE):
    return await generate_node(
        {
            "user_query": query,
            "policy_scope": scope,
            "strategy": strategy,
            "retrieved_chunks": chunks,
            "tool_results": [],
        },
        OfflineLLMProvider(),
    )


def _assert_demo_chunk(chunk, market: str) -> None:
    assert chunk.brand == "epig"
    assert chunk.market == market
    assert chunk.source_type == "internal_demo"
    assert chunk.source_label
    assert chunk.source_path == f"epig/{market}/{chunk.document_id.rsplit('_', 1)[0]}.md"
    assert chunk.source_urls == []
    assert chunk.policy_status == "demo"


def _assert_demo_citation(citation, market: str) -> None:
    assert citation.brand == "epig"
    assert citation.market == market
    assert citation.source_type == "internal_demo"
    assert citation.source_label
    assert citation.source_path
    assert citation.source_urls == []


@pytest.mark.asyncio
async def test_all_markets_answer_normal_return_refund_and_warranty_terms() -> None:
    retriever, reranker = await _components()
    cases = (
        ("au", "mattress", "床垫可以体验多少晚后退货", "returns_", "90 晚"),
        ("au", "furniture", "正价家具可以多少天内退货", "returns_", "45 天"),
        ("au", "furniture", "正常退货退款多久到账", "refunds_", "6–8 个工作日"),
        ("au", "mattress", "床垫保修多少年", "warranty_", "8 年"),
        ("au", "furniture", "家具保修多少年", "warranty_", "4 年"),
        ("us", "mattress", "床垫可以体验多少晚后退货", "returns_", "80 晚"),
        ("us", "furniture", "正价家具可以多少天内退货", "returns_", "30 天"),
        ("us", "furniture", "正常退货退款多久到账", "refunds_", "4–6 个工作日"),
        ("us", "mattress", "床垫保修多少年", "warranty_", "7 年"),
        ("us", "furniture", "家具保修多少年", "warranty_", "3 年"),
        ("sg", "mattress", "床垫可以体验多少晚后退货", "returns_", "60 晚"),
        ("sg", "furniture", "正价家具可以多少天内退货", "returns_", "21 天"),
        ("sg", "furniture", "正常退货退款多久到账", "refunds_", "7–10 个工作日"),
        ("sg", "mattress", "床垫保修多少年", "warranty_", "6 年"),
        ("sg", "furniture", "家具保修多少年", "warranty_", "2 年"),
    )
    for market, category, query, expected_document, expected_term in cases:
        scope = _scope(market, category=category)
        chunks = await _retrieve(retriever, reranker, query, scope)
        assert chunks, (market, query)
        assert chunks[0].document_id.startswith(expected_document), (market, query)
        assert expected_term in "\n".join(chunk.content for chunk in chunks)
        assert all(chunk.market == market and chunk.brand == "epig" for chunk in chunks)
        for chunk in chunks:
            _assert_demo_chunk(chunk, market)
        result = await _generate(query, scope, chunks)
        assert result["refused"] is False, (market, query)
        assert DEMO_DISCLOSURE in result["final_answer"]
        assert result["citations"]
        for citation in result["citations"]:
            _assert_demo_citation(citation, market)


@pytest.mark.asyncio
async def test_only_manifest_exception_topics_require_review_with_demo_sources() -> None:
    retriever, reranker = await _components()
    cases = (
        ("au", "furniture", "使用过且有污渍的沙发还能退吗", "returns_"),
        ("us", "mattress", "定制床垫可以退货吗", "returns_"),
        ("us", "furniture", "专门给我做的沙发不想要了可以退吗", "returns_"),
        ("au", "furniture", "按我的尺寸制作的家具能退货吗", "returns_"),
        ("au", "mattress", "定制床垫能退吗", "returns_"),
        ("us", "furniture", "配送费能退吗", "refunds_"),
        ("sg", "furniture", "最终销售家具能否取消订单", "cancellation_"),
        ("au", "furniture", "偏远地址配送多久能到", "delivery_"),
        ("us", "furniture", "已发货订单可以取消吗", "cancellation_"),
    )
    for market, category, query, expected_citation in cases:
        scope = _scope(market, category=category)
        chunks = await _retrieve(retriever, reranker, query, scope)
        assert chunks
        assert policy_review_required(query, chunks) is True, query
        result = await _generate(query, scope, chunks)
        assert result["refused"] is True
        assert "人工" in result["final_answer"]
        assert DEMO_DISCLOSURE in result["final_answer"]
        assert result["citations"]
        assert any(
            citation.document_id.startswith(expected_citation) for citation in result["citations"]
        )
        for citation in result["citations"]:
            _assert_demo_citation(citation, market)


@pytest.mark.asyncio
async def test_delivery_damage_and_missing_items_use_material_guidance_not_review() -> None:
    retriever, reranker = await _components()
    for market, query in (
        ("au", "收到运输损坏的沙发怎么办"),
        ("us", "家具运输中损坏了需要提交什么材料"),
        ("sg", "组装家具缺少零件要准备什么材料"),
    ):
        scope = _scope(market, category="furniture")
        chunks = await _retrieve(retriever, reranker, query, scope)
        assert chunks[0].document_id.startswith("faults-missing_")
        assert policy_review_required(query, chunks) is False
        result = await _generate(query, scope, chunks)
        assert result["refused"] is False
        assert result["retrieved_chunks"][0].document_id.startswith("faults-missing_")
        assert all(not chunk.requires_human_review for chunk in result["retrieved_chunks"])
        assert result["citations"][0].document_id.startswith("faults-missing_")
        assert DEMO_DISCLOSURE in result["final_answer"]


@pytest.mark.asyncio
async def test_missing_category_history_and_unsupported_electronics_return_no_evidence() -> None:
    retriever, reranker = await _components()
    empty_scopes = (
        _scope("au", as_of=date(2026, 11, 4)),
        _scope("us", category="electronics"),
        _scope("sg", category=None),
    )
    for scope in empty_scopes:
        assert await _retrieve(retriever, reranker, "家具保修多少年", scope) == []
        result = await _generate("家具保修多少年", scope, [])
        assert result["refused"] is True
        assert result["citations"] == []
        assert DEMO_DISCLOSURE in result["final_answer"]
        if scope.product_category is None:
            assert "商品类别" in result["final_answer"]
        else:
            assert "没有可用的政策证据" in result["final_answer"]


@pytest.mark.asyncio
async def test_legacy_demo_and_epig_market_cache_entries_never_mix() -> None:
    retriever, reranker = await _components()
    au = await _retrieve(retriever, reranker, "家具保修多少年", _scope("au"))
    us = await _retrieve(retriever, reranker, "家具保修多少年", _scope("us"))
    sg = await _retrieve(retriever, reranker, "家具保修多少年", _scope("sg"))
    legacy = await retriever.retrieve("定制商品能不能七天无理由退货", top_k=5)
    assert {chunk.market for chunk in au} == {"au"}
    assert {chunk.market for chunk in us} == {"us"}
    assert {chunk.market for chunk in sg} == {"sg"}
    assert all(chunk.brand == "epig" for chunk in au + us + sg)
    assert legacy
    assert {chunk.brand for chunk in legacy} == {"upstream_demo"}
    assert {chunk.market for chunk in legacy} == {"global"}


@pytest.mark.asyncio
async def test_tool_path_returns_scoped_chunks_and_complete_internal_source_fields() -> None:
    retriever, _ = await _components()
    scope = _scope("au", category="mattress")
    result = await SearchKnowledgeTool(retriever).run(
        {"query": "床垫保修多少年"},
        ToolContext(
            user_id="agent",
            session_id="session",
            trace_id="trace",
            policy_scope=scope,
        ),
    )
    chunks = result.output["chunks"]
    assert result.success is True
    assert chunks[0]["document_id"].startswith("warranty_")
    assert chunks[0]["market"] == "au"
    assert chunks[0]["brand"] == "epig"
    assert chunks[0]["source_type"] == "internal_demo"
    assert chunks[0]["source_label"]
    assert chunks[0]["source_path"] == "epig/au/warranty.md"
    assert chunks[0]["source_urls"] == []
    assert chunks[0]["policy_status"] == "demo"
    assert chunks[0]["retrieved_at"] == SNAPSHOT_DATE.isoformat()


@pytest.mark.asyncio
async def test_normal_terms_refunds_materials_delivery_and_unshipped_cancel_do_not_review() -> None:
    retriever, reranker = await _components()
    cases = (
        ("au", "furniture", "正价家具可以多少天内退货", "returns_"),
        ("us", "furniture", "正常退货退款啥时候到账", "refunds_"),
        ("sg", "furniture", "家具正常配送多久到", "delivery_"),
        ("au", "furniture", "退货申请需要哪些订单号和商品照片", "returns_"),
        ("us", "mattress", "床垫有限保修覆盖范围和申请材料", "warranty_"),
        ("sg", "furniture", "标准现货还没发货可以取消整单吗", "cancellation_"),
    )
    for market, category, query, expected_document in cases:
        scope = _scope(market, category=category)
        chunks = await _retrieve(retriever, reranker, query, scope)
        assert chunks[0].document_id.startswith(expected_document), query
        assert policy_review_required(query, chunks) is False, query
        result = await _generate(query, scope, chunks)
        assert result["refused"] is False, query
        assert DEMO_DISCLOSURE in result["final_answer"]
        assert result["citations"]
        assert all(not citation.requires_human_review for citation in result["citations"])


@pytest.mark.asyncio
async def test_tool_preserves_complete_exception_evidence_and_review_rules() -> None:
    retriever, _ = await _components()
    result = await SearchKnowledgeTool(retriever).run(
        {"query": "定制商品能否退货"},
        ToolContext(
            user_id="agent",
            session_id="session",
            trace_id="trace",
            policy_scope=_scope("us"),
        ),
    )
    assert result.success is True
    chunks = result.output["chunks"]
    assert any(len(item["content"]) > 100 for item in chunks)
    assert any(item["content"].endswith("本文不执行退货操作。") for item in chunks)
    assert any(item["content"].endswith("质量问题和适用法定权利需独立评估。") for item in chunks)
    assert any("定制" in item["content"] and "人工" in item["content"] for item in chunks)
    assert any(item["review_query_rules"] for item in chunks)
    assert all(item["source_type"] == "internal_demo" for item in chunks)


@pytest.mark.asyncio
async def test_structured_review_match_is_not_lost_to_top_k_truncation() -> None:
    retriever, _ = await _components()
    scope = _scope("us")
    query = "专门给我做的沙发不想要了可以退吗"
    ordinary = await retriever.retrieve("正价家具退货期限", top_k=1, scope=scope)
    assert all(not chunk.requires_human_review for chunk in ordinary)

    review_chunks = retriever.matching_review_chunks(query, scope=scope)
    assert review_chunks
    assert all(chunk.document_id.startswith("returns_") for chunk in review_chunks)
    result = await _generate(query, scope, [*review_chunks, *ordinary])
    assert result["refused"] is True
    assert result["citations"][0].document_id.startswith("returns_")


@pytest.mark.asyncio
async def test_review_gate_covers_cross_document_paraphrases_without_false_positives() -> None:
    retriever, reranker = await _components()
    review_cases = (
        ("au", "furniture", "破损家具退货后多久能退款", ("returns_", "refunds_")),
        ("au", "furniture", "沙发用过还能退货吗", ("returns_",)),
        ("us", "furniture", "定制家具可以退货吗", ("returns_",)),
        ("sg", "furniture", "发货后还能拦截订单吗", ("cancellation_", "delivery_")),
    )
    for market, category, query, expected_documents in review_cases:
        scope = _scope(market, category=category)
        chunks = await _retrieve(retriever, reranker, query, scope)
        assert policy_review_required(query, chunks) is True, query
        result = await _generate(query, scope, chunks)
        assert result["refused"] is True
        cited = {citation.document_id.rsplit("_", 1)[0] for citation in result["citations"]}
        assert any(
            citation.document_id.startswith(expected)
            for expected in expected_documents
            for citation in result["citations"]
        ), (query, cited)
        assert DEMO_DISCLOSURE in result["final_answer"]

    normal_cases = (
        ("au", "furniture", "配送费多少钱、如何试算"),
        ("au", "furniture", "退货申请需要哪些订单号和商品照片"),
        ("au", "mattress", "床垫正常体验期是多少晚"),
        ("us", "furniture", "正常退款多久到账"),
        ("sg", "furniture", "普通家具的保修年限是多少"),
        ("au", "furniture", "定制家具订单什么时候发货"),
    )
    for market, category, query in normal_cases:
        scope = _scope(market, category=category)
        chunks = await _retrieve(retriever, reranker, query, scope)
        assert policy_review_required(query, chunks) is False, query
        result = await _generate(query, scope, chunks, strategy=RetrievalStrategy.DIRECT)
        assert result["refused"] is False
        assert DEMO_DISCLOSURE in result["final_answer"]
        assert result["citations"]
        assert all(not citation.requires_human_review for citation in result["citations"])
