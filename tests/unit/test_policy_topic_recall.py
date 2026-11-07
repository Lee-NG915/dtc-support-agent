"""Deterministic anomaly-topic hints and bounded supplemental merging."""

from __future__ import annotations

from datetime import date

from app.core.enums import DocumentCategory
from app.retrieval.policy import merge_policy_topic_chunks, policy_topic_hints
from app.schemas.retrieval import RetrievedChunk

SNAPSHOT_DATE = date(2026, 11, 5)


def _chunk(chunk_id: str, *, topic: str = "faults-missing") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=f"{topic}_abc12345",
        title="E-pig SG 损坏、错发与缺件处理材料",
        content="缺件：已收到的商品与配件合照、包装清单、全部包材照片，并列明缺少部件。",
        brand="epig",
        market="sg",
        category=DocumentCategory.AFTERSALE,
        product_categories=["furniture"],
        retrieved_at=SNAPSHOT_DATE,
        effective_from=SNAPSHOT_DATE,
        policy_status="demo",
        source_type="internal_demo",
        source_label="E-pig 内部自编演示政策",
        source_path=f"epig/sg/{topic}.md",
        source_updated="内部自编演示，无外部来源",
    )


def test_missing_parts_wording_returns_exact_retrieval_hint() -> None:
    hints = policy_topic_hints("椅子拆箱少了一包螺丝，纸箱还在。先拍哪些东西给客服核对？")

    assert [hint.topic for hint in hints] == ["missing_parts"]
    hint = hints[0]
    assert hint.document_topic == "faults-missing"
    assert "缺件" in hint.query
    assert "材料清单" in hint.query
    assert "包装清单" in hint.query
    assert "全部包材" in hint.query


def test_ordinary_waiting_wording_does_not_trigger_anomaly_hints() -> None:
    assert policy_topic_hints("请少等一会儿，我马上帮您查") == ()
    assert policy_topic_hints("帮我减少一些包装材料") == ()


def test_wrong_item_and_transport_damage_wording_trigger_their_topics() -> None:
    assert [hint.topic for hint in policy_topic_hints("收到的商品发错了")] == ["wrong_item"]
    assert [hint.topic for hint in policy_topic_hints("运输中压坏了，箱子破损")] == [
        "transport_damage"
    ]
    assert [hint.topic for hint in policy_topic_hints("the delivered item is a wrong item")] == [
        "wrong_item"
    ]


def test_merge_appends_only_new_topic_chunks_and_respects_the_bound() -> None:
    existing = [_chunk("delivery_abc12345_03", topic="delivery")]
    topic = [
        _chunk("faults-missing_abc12345_02"),
        _chunk("faults-missing_abc12345_03"),
        _chunk("faults-missing_abc12345_01"),
    ]

    merged = merge_policy_topic_chunks(existing, topic, limit=2)

    assert [chunk.chunk_id for chunk in merged] == [
        "delivery_abc12345_03",
        "faults-missing_abc12345_02",
        "faults-missing_abc12345_03",
    ]


def test_merge_deduplicates_chunks_already_in_ranked_results() -> None:
    duplicate = _chunk("faults-missing_abc12345_02")
    merged = merge_policy_topic_chunks(
        [duplicate],
        [duplicate, _chunk("faults-missing_abc12345_03")],
        limit=2,
    )

    assert [chunk.chunk_id for chunk in merged] == [
        "faults-missing_abc12345_02",
        "faults-missing_abc12345_03",
    ]
