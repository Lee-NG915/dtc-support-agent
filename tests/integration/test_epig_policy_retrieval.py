import json
from datetime import date

import pytest

from app.agents.nodes.generate import generate_node
from app.agents.nodes.retrieval import retrieval_node
from app.config.settings import Settings
from app.core.enums import RetrievalStrategy
from app.providers.embeddings import HashEmbeddingProvider
from app.providers.fake import FakeLLMProvider, OfflineLLMProvider
from app.providers.reranker import LexicalRerankProvider
from app.retrieval.cache import MemoryRetrievalCache
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.ingest import ingest_to_memory
from app.retrieval.policy import (
    chunk_document_topic,
    chunk_matches_scope,
    corpus_fingerprint,
    merge_policy_review_chunks,
    policy_review_chunks,
    policy_review_required,
    policy_review_topics,
)
from app.retrieval.query_rewrite import QueryRewriter
from app.retrieval.reranker import DocumentReranker
from app.retrieval.self_rag import SelfRAGLoop
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


H05_QUERY = "椅子拆箱少了一包螺丝，纸箱还在。先拍哪些东西给客服核对，是否现在就能保证免费寄一包？"
MISSING_CHECKLIST = (
    "缺件材料还需提供已收到的商品与配件合照、包装清单、全部包材照片，并列明缺少部件。"
)


def _retrieval_settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="testing",
        retrieval_top_k_simple=4,
        retrieval_top_k_complex=8,
        rerank_top_n=5,
        max_retrieval_hops=1,
    )


async def _run_retrieval_node(
    retriever: HybridRetriever,
    reranker: DocumentReranker,
    scope: PolicyScope,
    strategy: RetrievalStrategy,
    query: str,
) -> dict:
    settings = _retrieval_settings()
    offline = OfflineLLMProvider()
    rewriter = QueryRewriter(offline)
    self_rag = SelfRAGLoop(retriever, reranker, rewriter, offline, settings)
    return await retrieval_node(
        {
            "user_query": query,
            "policy_scope": scope,
            "strategy": strategy,
            "tool_results": [],
        },
        retriever=retriever,
        reranker=reranker,
        rewriter=rewriter,
        self_rag=self_rag,
        settings=settings,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", [RetrievalStrategy.SINGLE, RetrievalStrategy.MULTI])
async def test_retrieval_node_adds_bounded_faults_missing_evidence(
    strategy: RetrievalStrategy,
) -> None:
    retriever, reranker = await _components()
    scope = _scope("sg", category="furniture")

    result = await _run_retrieval_node(retriever, reranker, scope, strategy, H05_QUERY)

    chunks = result["retrieved_chunks"]
    ids = [chunk.chunk_id for chunk in chunks]
    assert len(ids) == len(set(ids))
    faults = [chunk for chunk in chunks if chunk_document_topic(chunk) == "faults-missing"]
    assert faults
    assert len(faults) <= 2
    assert any("包装清单" in chunk.content and "全部包材" in chunk.content for chunk in faults)
    assert all(chunk_matches_scope(chunk, scope) for chunk in chunks)


@pytest.mark.asyncio
async def test_generate_appends_server_checklist_and_cites_real_faults_chunk() -> None:
    retriever, reranker = await _components()
    scope = _scope("sg", category="furniture")
    retrieval = await _run_retrieval_node(
        retriever, reranker, scope, RetrievalStrategy.MULTI, H05_QUERY
    )
    chunks = retrieval["retrieved_chunks"]
    faults = next(
        chunk
        for chunk in chunks
        if chunk_document_topic(chunk) == "faults-missing"
        and "包装清单" in chunk.content
        and "全部包材" in chunk.content
    )
    delivery = next(
        (chunk for chunk in chunks if chunk_document_topic(chunk) == "delivery"), chunks[0]
    )
    provider = FakeLLMProvider(
        canned={
            "": json.dumps(
                {
                    "answer": "请先按配送交付检查拍照，并保留纸箱。",
                    "citation_chunk_ids": [delivery.chunk_id],
                    "refused": False,
                },
                ensure_ascii=False,
            )
        }
    )

    result = await generate_node(
        {
            "user_query": H05_QUERY,
            "policy_scope": scope,
            "strategy": RetrievalStrategy.MULTI,
            "retrieved_chunks": chunks,
            "tool_results": [],
        },
        provider,
    )

    assert result["refused"] is False
    assert MISSING_CHECKLIST in result["final_answer"]
    citation_ids = {citation.chunk_id for citation in result["citations"]}
    assert faults.chunk_id in citation_ids
    assert delivery.chunk_id in citation_ids


@pytest.mark.asyncio
async def test_generate_never_invents_the_checklist_without_a_faults_chunk() -> None:
    retriever, _ = await _components()
    scope = _scope("sg", category="furniture")
    delivery = next(
        chunk
        for chunk in retriever.corpus
        if chunk.market == "sg"
        and chunk_document_topic(chunk) == "delivery"
        and chunk_matches_scope(chunk, scope)
    )
    provider = FakeLLMProvider(
        canned={
            "": json.dumps(
                {
                    "answer": "请先按配送交付检查拍照，并保留纸箱。",
                    "citation_chunk_ids": [delivery.chunk_id],
                    "refused": False,
                },
                ensure_ascii=False,
            )
        }
    )

    result = await generate_node(
        {
            "user_query": H05_QUERY,
            "policy_scope": scope,
            "strategy": RetrievalStrategy.SINGLE,
            "retrieved_chunks": [delivery],
            "tool_results": [],
        },
        provider,
    )

    assert "包装清单" not in result["final_answer"]
    assert "全部包材" not in result["final_answer"]
    assert [citation.chunk_id for citation in result["citations"]] == [delivery.chunk_id]


@pytest.mark.asyncio
async def test_generate_does_not_complete_a_refused_answer() -> None:
    retriever, reranker = await _components()
    scope = _scope("sg", category="furniture")
    retrieval = await _run_retrieval_node(
        retriever, reranker, scope, RetrievalStrategy.MULTI, H05_QUERY
    )
    chunks = retrieval["retrieved_chunks"]
    provider = FakeLLMProvider(
        canned={
            "": json.dumps(
                {
                    "answer": "无法处理。",
                    "citation_chunk_ids": [],
                    "refused": True,
                },
                ensure_ascii=False,
            )
        }
    )

    result = await generate_node(
        {
            "user_query": H05_QUERY,
            "policy_scope": scope,
            "strategy": RetrievalStrategy.MULTI,
            "retrieved_chunks": chunks,
            "tool_results": [],
        },
        provider,
    )

    assert result["refused"] is True
    assert "包装清单" not in result["final_answer"]
    assert result["citations"] == []


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


V2_DATE = date(2026, 11, 6)
H04_V1_QUERY = "货已经出库了，能否直接替我撤掉订单并保证把配送费全部退回来？"
H04_V3_QUERY = "AU退货已验收，但配送费没退，原卡又注销了。还能按普通六到八天等到原卡吗？"
H06_V1_QUERY = "偏远邮编暂时查不到，普通床品时效是不是也能当作我的保证送达日期？"
H05_V2_QUERY = "新加坡床垫角落被狗咬坏了，能直接按保修换新吗？这种情况是否还要看意外损坏规则？"
H06_V2_QUERY = (
    "美国偏远山区地址只能提供无法确认的邮编，沙发能保证四到八天送到吗？运费是不是固定的？"
)


class _NeverCalledLLM:
    """Fail the test if the review branch reaches the generation provider."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("review branch must not call the model provider")


@pytest.mark.asyncio
async def test_review_handoff_guidance_is_topic_specific_bounded_and_model_free() -> None:
    retriever, reranker = await _components()
    cases = (
        (
            "au",
            "furniture",
            SNAPSHOT_DATE,
            H04_V1_QUERY,
            {"service_fee_refund"},
            ("原订单的实际支付构成", "不能预先承诺配送费、装配费或附加费一定全部返还"),
        ),
        (
            "au",
            "homewares",
            SNAPSHOT_DATE,
            H04_V3_QUERY,
            {"service_fee_refund", "payment_refund_exception"},
            (
                "原订单的实际支付构成",
                "原支付方式当前是否可用",
                "不能预先承诺配送费、装配费或附加费一定全部返还",
                "不能承诺按固定支付渠道、固定金额或固定日期退回",
            ),
        ),
        (
            "au",
            "furniture",
            SNAPSHOT_DATE,
            "原支付账户失效了，还能退款到礼品卡吗？",
            {"payment_refund_exception"},
            ("原支付方式当前是否可用", "不能承诺按固定支付渠道、固定金额或固定日期退回"),
        ),
        (
            "us",
            "bedding",
            SNAPSHOT_DATE,
            H06_V1_QUERY,
            {"regional_delivery"},
            ("收货地址与邮编", "不能承诺特殊或偏远地址一定可送、运费固定或按常规时效送达"),
        ),
        (
            "sg",
            "mattress",
            V2_DATE,
            H05_V2_QUERY,
            {"accidental_damage", "accidental_damage_warranty"},
            ("意外发生经过、时间和问题部位照片", "不能预先承诺按保修处理、换新或赔偿"),
        ),
        (
            "us",
            "furniture",
            V2_DATE,
            H06_V2_QUERY,
            {"regional_delivery"},
            ("收货地址与邮编", "不能承诺特殊或偏远地址一定可送、运费固定或按常规时效送达"),
        ),
        (
            "us",
            "furniture",
            SNAPSHOT_DATE,
            "定制家具可以退货吗",
            {"custom_or_final_sale_return", "custom_or_final_sale_cancellation"},
            ("定制/定做/按尺寸制作", "不能预先拒绝退货，也不排除商品质量问题和法定权利"),
        ),
        (
            "au",
            "furniture",
            SNAPSHOT_DATE,
            "使用过且有污渍的沙发还能退吗",
            {"used_or_damaged_return", "acceptance_or_amount_dispute"},
            ("使用痕迹或污渍照片", "不能预先拒绝退货，也不排除商品质量问题和法定权利"),
        ),
        (
            "au",
            "furniture",
            SNAPSHOT_DATE,
            "部分退货时赠品和优惠怎么分摊",
            {"partial_return_allocation", "partial_refund_allocation"},
            ("部分退回的具体商品、赠品和组合优惠明细", "不能预先承诺优惠保留或退款金额"),
        ),
        (
            "au",
            "furniture",
            SNAPSHOT_DATE,
            "只想取消其中一件，优惠会怎么分摊",
            {"partial_cancellation", "partial_refund_allocation", "partial_return_allocation"},
            ("需取消的具体商品、订单当前状态", "不能预先承诺部分取消成功、优惠保留或退款金额"),
        ),
        (
            "us",
            "furniture",
            SNAPSHOT_DATE,
            "已发货订单可以取消吗",
            {"shipped_cancellation_or_change", "shipped_delivery_change"},
            ("当前物流节点和原订单商品", "不能预先承诺拦截成功、全额退款或保留原优惠"),
        ),
        (
            "us",
            "furniture",
            SNAPSHOT_DATE,
            "已发货可以改地址吗",
            {"shipped_cancellation_or_change", "shipped_delivery_change"},
            ("改地址、改收货人或改约请求", "不能保证地址变更或配送改约一定成功"),
        ),
        (
            "au",
            "furniture",
            SNAPSHOT_DATE,
            "无电梯六楼能送货入户吗",
            {"special_delivery_access"},
            ("电梯、楼梯、门禁和停车条件", "不能预先承诺特殊入户、吊装安排或由此产生的费用"),
        ),
        (
            "au",
            "mattress",
            SNAPSHOT_DATE,
            "酒店使用过的床垫还有保修吗",
            {"nonstandard_use_or_repair"},
            ("商业或酒店使用", "不能预先判断保修资格或承诺赔付"),
        ),
        (
            "au",
            "furniture",
            SNAPSHOT_DATE,
            "配送事故导致人身伤害，谁负责赔偿？",
            {"injury_property_or_legal_liability"},
            ("受伤或财产损失记录", "不能预先认定责任、赔偿金额或法律结果"),
        ),
        (
            "au",
            "furniture",
            SNAPSHOT_DATE,
            "破损家具退货退款金额有争议",
            {"acceptance_or_amount_dispute", "used_or_damaged_return"},
            ("验收记录、商品状态照片", "不能预先确定验收结果或退款金额"),
        ),
        (
            "sg",
            "furniture",
            SNAPSHOT_DATE,
            "最终销售家具能否取消订单",
            {"custom_or_final_sale_cancellation", "custom_or_final_sale_return"},
            ("当前生产和出库状态", "不能预先确认取消资格、退款结果或保留优惠"),
        ),
    )

    for market, category, as_of, query, expected_topics, fragments in cases:
        scope = _scope(market, category=category, as_of=as_of)
        chunks = await _retrieve(retriever, reranker, query, scope)
        assert policy_review_required(query, chunks) is True, query

        provider = _NeverCalledLLM()
        result = await generate_node(
            {
                "user_query": query,
                "policy_scope": scope,
                "strategy": RetrievalStrategy.SINGLE,
                "retrieved_chunks": chunks,
                "tool_results": [],
            },
            provider,  # type: ignore[arg-type]
        )

        answer = result["final_answer"]
        assert provider.calls == 0, query
        assert result["refused"] is True, query
        assert "需人工核实，不能直接形成真实订单处理承诺" in answer, query
        assert answer.count("转人工前请先核对") == 1, query
        for fragment in fragments:
            assert fragment in answer, (query, fragment)
        assert set(policy_review_topics(query, policy_review_chunks(query, chunks))) == (
            expected_topics
        ), query
        assert result["citations"], query
        assert all(citation.requires_human_review for citation in result["citations"]), query
        assert all(
            citation.brand == "epig" and citation.market == market
            for citation in result["citations"]
        ), query

        # Both accidental-damage topics share one checklist; it must not repeat.
        if expected_topics == {"accidental_damage", "accidental_damage_warranty"}:
            assert answer.count("意外发生经过") == 1
            assert answer.count("不能预先承诺按保修处理") == 1
