from datetime import date

import pytest

from app.agents.nodes.faithfulness import faithfulness_node
from app.agents.nodes.generate import generate_node
from app.core.enums import DocumentCategory, RetrievalStrategy
from app.guardrails.hallucination import FaithfulnessResult
from app.providers.base import LLMResponse
from app.providers.fake import FakeLLMProvider
from app.schemas.common import TokenUsage
from app.schemas.retrieval import (
    PolicyScope,
    RetrievedChunk,
    ReviewQueryRule,
    citation_from_chunk,
)

_DEMO_DISCLOSURE = "内部自编演示政策，非官方或生产批准"
_ANSWER_JSON = (
    '{"answer":"正常退货材料以订单和照片为准。","citation_chunk_ids":["normal"],"refused":false}'
)


def _chunk(*, review: bool = False) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id="review" if review else "normal",
        document_id="returns_review" if review else "returns_normal",
        title="E-pig AU returns",
        content="定制商品需人工复核。" if review else "退货材料包括订单号和商品照片。",
        brand="epig",
        market="au",
        category=DocumentCategory.RETURN_POLICY,
        product_categories=["furniture"],
        retrieved_at=date(2026, 11, 5),
        effective_from=date(2026, 11, 5),
        requires_human_review=review,
        review_query_rules=[ReviewQueryRule(topic="custom", all_of=[["定制"]])] if review else [],
        source_type="internal_demo",
        source_label="E-pig 内部自编演示政策",
        source_path="epig/au/returns.md",
    )


def _state() -> dict:
    return {
        "user_query": "普通退货需要哪些材料",
        "policy_scope": PolicyScope(
            brand="epig",
            market="au",
            as_of=date(2026, 11, 5),
            product_category="furniture",
        ),
        "strategy": RetrievalStrategy.SINGLE,
        "retrieved_chunks": [_chunk(review=True), _chunk()],
        "tool_results": [],
    }


class _RewriteLLM:
    async def generate(self, messages, **kwargs) -> LLMResponse:
        return LLMResponse(
            content=(
                '{"answer":"退货材料以订单和交付证明为准。",'
                '"citation_chunk_ids":["alternate"],"refused":false}'
            ),
            usage=TokenUsage(),
            model="fake",
        )


class _Checker:
    def __init__(self, results: list[FaithfulnessResult]) -> None:
        self.results = list(results)
        self.answers: list[str] = []
        self.chunk_ids: list[list[str]] = []

    async def check(self, *, query, answer, chunks, tool_results_text):
        self.answers.append(answer)
        self.chunk_ids.append([chunk.chunk_id for chunk in chunks])
        return self.results.pop(0)


async def _generated_state(llm: FakeLLMProvider) -> dict:
    state = _state()
    generated = await generate_node(state, llm)
    return state | generated


@pytest.mark.asyncio
async def test_checker_fallback_uses_filtered_chunks_and_restores_disclosure() -> None:
    llm = FakeLLMProvider(canned={"": _ANSWER_JSON})
    state = await _generated_state(llm)
    checker = _Checker([FaithfulnessResult(supported=False, rationale="checker_unavailable")])

    result = await faithfulness_node(state, checker, llm)

    assert checker.chunk_ids == [["normal"]]
    assert result["citations"][0].chunk_id == "normal"
    assert _DEMO_DISCLOSURE in result["final_answer"]


@pytest.mark.asyncio
async def test_self_correction_excludes_review_chunks_and_restores_disclosure() -> None:
    llm = FakeLLMProvider(canned={"": _ANSWER_JSON})
    state = await _generated_state(llm)
    checker = _Checker(
        [
            FaithfulnessResult(supported=False, rationale="unsupported"),
            FaithfulnessResult(supported=True),
        ]
    )

    result = await faithfulness_node(state, checker, llm)

    assert _DEMO_DISCLOSURE not in checker.answers[0]
    assert checker.chunk_ids == [["normal"], ["normal"]]
    assert "定制商品需人工复核" not in llm.calls[-1][-1]["content"]
    assert _DEMO_DISCLOSURE in result["final_answer"]


@pytest.mark.asyncio
async def test_unsupported_self_correction_refuses_with_disclosure() -> None:
    llm = FakeLLMProvider(canned={"": _ANSWER_JSON})
    state = await _generated_state(llm)
    checker = _Checker(
        [
            FaithfulnessResult(supported=False, rationale="unsupported"),
            FaithfulnessResult(supported=False, rationale="still_unsupported"),
            FaithfulnessResult(supported=False, rationale="fallback_unsupported"),
        ]
    )

    result = await faithfulness_node(state, checker, llm)

    assert result["refused"] is True
    assert result["citations"] == []
    assert _DEMO_DISCLOSURE in result["final_answer"]


@pytest.mark.asyncio
async def test_supported_rewrite_replaces_stale_citations() -> None:
    original = _chunk()
    alternate = original.model_copy(
        update={
            "chunk_id": "alternate",
            "document_id": "returns_alternate",
            "content": "退货材料包括订单号和交付证明。",
        }
    )
    state = _state() | {
        "final_answer": "旧答案只引用原片段。",
        "citations": [citation_from_chunk(original)],
        "retrieved_chunks": [original, alternate],
    }
    checker = _Checker(
        [
            FaithfulnessResult(supported=False, rationale="unsupported"),
            FaithfulnessResult(supported=True),
        ]
    )

    result = await faithfulness_node(state, checker, _RewriteLLM())

    assert [citation.chunk_id for citation in result["citations"]] == ["alternate"]
    assert result["citations"][0].document_id == "returns_alternate"
    assert _DEMO_DISCLOSURE in result["final_answer"]
