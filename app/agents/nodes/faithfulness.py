"""Self-RAG faithfulness check and answer self-correction."""

from __future__ import annotations

from app.agents.nodes.generate import (
    AnswerPayload,
    _extractive_fallback,
    _offline_answer,
    _with_demo_disclosure,
)
from app.agents.state import AgentState
from app.config.policy_profiles import get_policy_profile
from app.core.enums import RetrievalStrategy
from app.core.exceptions import LLMOutputError, LLMProviderError
from app.guardrails.hallucination import FaithfulnessChecker, FaithfulnessResult
from app.observability.metrics import HALLUCINATION_FLAGS
from app.prompts.faithfulness import SELF_CORRECT_SYSTEM, SELF_CORRECT_USER
from app.providers.base import LLMProvider
from app.providers.fake import OfflineLLMProvider
from app.providers.json_parser import parse_model
from app.schemas.retrieval import PolicyScope, RetrievedChunk, citation_from_chunk


async def self_correct_answer(
    *,
    llm: LLMProvider,
    checker: FaithfulnessChecker,
    query: str,
    answer: str,
    chunks: list[RetrievedChunk],
    tool_results_text: str,
    first_result: FaithfulnessResult,
) -> tuple[str, FaithfulnessResult, bool, list[str]]:
    """Rewrite once, then use extractive fallback; return answer evidence IDs."""
    evidence = (
        "\n\n".join(f"[{c.chunk_id}] {c.title}\n{c.content}" for c in chunks[:6]) or "(无检索结果)"
    )
    try:
        response = await llm.generate(
            [
                {"role": "system", "content": SELF_CORRECT_SYSTEM},
                {
                    "role": "user",
                    "content": SELF_CORRECT_USER.format(
                        query=query,
                        answer=answer,
                        critique=first_result.rationale
                        or ",".join(first_result.hallucinated_spans),
                        evidence=evidence,
                        tool_results=tool_results_text or "(无)",
                    ),
                },
            ],
            temperature=0.0,
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        payload = parse_model(response.content, AnswerPayload)
        rewritten = payload.answer
        judged = await checker.check(
            query=query,
            answer=rewritten,
            chunks=chunks,
            tool_results_text=tool_results_text,
        )
        if judged.supported:
            return rewritten, judged, True, payload.citation_chunk_ids
    except (LLMProviderError, LLMOutputError):
        pass
    patched = _extractive_fallback(query, evidence, tool_results_text)
    judged = await checker.check(
        query=query,
        answer=patched,
        chunks=chunks,
        tool_results_text=tool_results_text,
    )
    fallback_citation_ids = [chunks[0].chunk_id] if chunks else []
    return patched, judged, True, fallback_citation_ids


async def faithfulness_node(
    state: AgentState, checker: FaithfulnessChecker, llm: LLMProvider
) -> dict:
    answer = state.get("final_answer") or ""
    scope = state.get("policy_scope") or PolicyScope()
    disclosure = get_policy_profile(scope.brand).demo_disclosure
    if (
        state.get("refused")
        or state.get("pending_action")
        or (state.get("extra") or {}).get("missing_order_id")
    ):
        return {}
    chunks = state.get("retrieved_chunks") or []
    tools = state.get("tool_results") or []
    if isinstance(llm, OfflineLLMProvider) or state.get("strategy") == RetrievalStrategy.DIRECT:
        return {}
    if state.get("strategy") in {RetrievalStrategy.SINGLE, RetrievalStrategy.MULTI} and not chunks:
        return {
            "final_answer": _with_demo_disclosure(
                "现有资料不足，暂时无法可靠回答。请补充具体商品或政策信息。",
                scope.brand,
            ),
            "citations": [],
            "refused": True,
        }
    # The deterministic demo disclosure is not a model claim and needs no evidence.
    answer_for_check = answer.replace(disclosure, "").strip() if disclosure else answer
    tool_text = str([t.model_dump() for t in tools])
    result = await checker.check(
        query=state["user_query"],
        answer=answer_for_check,
        chunks=chunks,
        tool_results_text=tool_text,
    )
    extra = {"faithfulness": result.model_dump(), "self_corrected": False}
    if result.supported:
        return {"extra": extra}
    HALLUCINATION_FLAGS.inc()
    if result.rationale == "checker_unavailable":
        fallback_answer, citation_ids = _offline_answer(state)
        citations = [
            citation_from_chunk(chunk) for chunk in chunks if chunk.chunk_id in citation_ids
        ]
        return {
            "final_answer": _with_demo_disclosure(fallback_answer, scope.brand),
            "citations": citations,
            "extra": extra,
        }
    patched, judged, corrected, patched_citation_ids = await self_correct_answer(
        llm=llm,
        checker=checker,
        query=state["user_query"],
        answer=answer_for_check,
        chunks=chunks,
        tool_results_text=tool_text,
        first_result=result,
    )
    extra = {"faithfulness": judged.model_dump(), "self_corrected": corrected}
    if not judged.supported:
        return {
            "final_answer": _with_demo_disclosure(
                "现有证据不足，暂时无法给出可靠答复。请补充信息或转人工。",
                scope.brand,
            ),
            "citations": [],
            "refused": True,
            "extra": extra,
        }
    citations = [
        citation_from_chunk(chunk)
        for chunk in chunks
        if chunk.chunk_id in set(patched_citation_ids)
    ]
    return {
        "final_answer": _with_demo_disclosure(patched, scope.brand),
        "citations": citations,
        "extra": extra,
    }
