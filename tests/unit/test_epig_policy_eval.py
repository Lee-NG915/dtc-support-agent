import json

import pytest

from app.providers.base import LLMResponse
from app.schemas.common import TokenUsage
from eval import runner


class _EvidenceLLM:
    async def generate(self, messages, **kwargs):
        user_text = messages[-1]["content"]
        evidence = user_text.split("检索证据：\n", 1)[1].split("\n\n工具结果：", 1)[0]
        chunk_id = evidence.split("[", 1)[1].split("]", 1)[0]
        content = json.dumps(
            {"answer": evidence, "citation_chunk_ids": [chunk_id], "refused": False},
            ensure_ascii=False,
        )
        return LLMResponse(content=content, usage=TokenUsage(total_tokens=8), model="test")


class _UncitedLLM:
    async def generate(self, messages, **kwargs):
        return LLMResponse(
            content='{"answer":"无法确定","citation_chunk_ids":[],"refused":false}',
            usage=TokenUsage(total_tokens=8),
            model="test",
        )


@pytest.mark.asyncio
async def test_epig_generation_eval_separates_model_calls_from_deterministic_guards() -> None:
    rows = runner.load_jsonl(runner.EPIG_POLICY_PATH)
    expected_model_calls = sum(
        not (row.get("expected_empty") or row.get("expect_review")) for row in rows
    )
    result = await runner.eval_epig_policy_generation(_EvidenceLLM())
    assert result["n"] == len(rows)
    assert result["passed"] == len(rows)
    assert result["attempted_model_calls"] == expected_model_calls
    assert result["fallback_answers"] == 0
    assert result["disclosure_failures"] == 0
    assert result["model_generation_tested"] is True
    assert result["full_graph_tested"] is False
    assert result["faithfulness_tested"] is False
    assert all("answer" not in case for case in result["cases"])


@pytest.mark.asyncio
async def test_epig_generation_eval_fails_normal_answers_without_terms_or_citations() -> None:
    rows = runner.load_jsonl(runner.EPIG_POLICY_PATH)
    expected_model_calls = sum(
        not (row.get("expected_empty") or row.get("expect_review")) for row in rows
    )
    result = await runner.eval_epig_policy_generation(_UncitedLLM())
    assert result["passed"] == len(rows) - expected_model_calls
    assert result["n"] == len(rows)
    failures = [case for case in result["cases"] if not case["passed"]]
    assert len(failures) == expected_model_calls
    assert all(not case["terms_ok"] and not case["citations_ok"] for case in failures)
    assert all(case["disclosure_ok"] for case in result["cases"])


def test_generation_eval_accepts_equivalent_terms_without_weakening_evidence_terms() -> None:
    row = {
        "required_terms": ["八年"],
        "required_answer_term_groups": [["八年", "8 年", "8年"]],
    }
    assert runner._missing_answer_term_groups(row, "有限保修为 8 年。") == []
    assert runner._missing_answer_term_groups(row, "有限保修为三年。") == ["八年 / 8 年 / 8年"]
    assert "8 年" not in row["required_terms"]


def test_answer_term_overrides_do_not_drop_unlisted_required_terms() -> None:
    row = {
        "required_terms": ["未发货", "取消"],
        "required_answer_term_groups": [["未发货", "尚未出库"]],
    }
    assert runner._missing_answer_term_groups(row, "尚未出库时可以申请。") == ["取消"]
    assert runner._missing_answer_term_groups(row, "尚未出库时可以取消。") == []
