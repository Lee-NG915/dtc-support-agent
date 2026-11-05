from fastapi.testclient import TestClient

from app.config.settings import clear_settings_cache
from app.main import create_app

DEMO_DISCLOSURE = "内部自编演示政策，非官方或生产批准"


def _client() -> TestClient:
    clear_settings_cache()
    return TestClient(create_app())


def _policy_payload(message: str, market: str, session_id: str) -> dict:
    return {
        "message": message,
        "user_id": "demo_agent",
        "session_id": session_id,
        "brand": "epig",
        "market": market,
        "policy_as_of": "2026-11-05",
        "product_category": "furniture",
    }


def _assert_demo_citations(payload: dict, market: str) -> None:
    assert payload["citations"]
    for citation in payload["citations"]:
        assert citation["brand"] == "epig"
        assert citation["market"] == market
        assert citation["source_type"] == "internal_demo"
        assert citation["source_label"]
        assert citation["source_path"]
        assert citation["source_urls"] == []


def test_au_us_same_query_and_session_use_only_current_market() -> None:
    with _client() as client:
        au_response = client.post(
            "/api/v1/chat", json=_policy_payload("家具保修多少年", "au", "market-switch")
        )
        us_response = client.post(
            "/api/v1/chat", json=_policy_payload("家具保修多少年", "us", "market-switch")
        )
    assert au_response.status_code == 200
    assert us_response.status_code == 200
    au = au_response.json()
    us = us_response.json()
    assert au["refused"] is False
    assert us["refused"] is False
    assert "4 年" in au["answer"]
    assert "3 年" in us["answer"]
    assert DEMO_DISCLOSURE in au["answer"]
    assert DEMO_DISCLOSURE in us["answer"]
    _assert_demo_citations(au, "au")
    _assert_demo_citations(us, "us")


def test_sg_has_normal_answer_and_history_and_electronics_fail_closed() -> None:
    with _client() as client:
        sg_response = client.post(
            "/api/v1/chat", json=_policy_payload("家具保修多少年", "sg", "sg")
        )
        historical = _policy_payload("家具保修多少年", "au", "historical")
        historical["policy_as_of"] = "2026-11-04"
        history_response = client.post("/api/v1/chat", json=historical)
        unsupported = _policy_payload("电子产品保修多少年", "us", "electronics")
        unsupported["product_category"] = "electronics"
        unsupported_response = client.post("/api/v1/chat", json=unsupported)
    assert sg_response.status_code == 200
    assert history_response.status_code == 200
    assert unsupported_response.status_code == 200
    sg = sg_response.json()
    assert sg["refused"] is False
    assert "2 年" in sg["answer"]
    assert DEMO_DISCLOSURE in sg["answer"]
    _assert_demo_citations(sg, "sg")
    for response in (history_response, unsupported_response):
        assert response.json()["refused"] is True
        assert response.json()["citations"] == []
        assert "没有可用的政策证据" in response.json()["answer"]
        assert DEMO_DISCLOSURE in response.json()["answer"]


def test_shipped_cancellation_and_demo_order_write_cannot_bypass_policy_guards() -> None:
    with _client() as client:
        shipped_cancellation = client.post(
            "/api/v1/chat",
            json=_policy_payload("已发货订单可以取消吗", "au", "shipped-cancel"),
        )
        demo_write = client.post(
            "/api/v1/chat",
            json=_policy_payload("ORD1001 我要退货并退款", "au", "demo-write"),
        )
    assert shipped_cancellation.status_code == 200
    assert demo_write.status_code == 200
    shipped = shipped_cancellation.json()
    assert shipped["refused"] is True
    assert "人工" in shipped["answer"]
    assert DEMO_DISCLOSURE in shipped["answer"]
    _assert_demo_citations(shipped, "au")
    assert demo_write.json()["pending_action"] is None
    assert "已提交" not in demo_write.json()["answer"]
    assert "已取消" not in demo_write.json()["answer"]


def test_missing_category_future_date_and_normal_return_term_through_http() -> None:
    with _client() as client:
        missing_category = _policy_payload("床垫保修多少年", "us", "missing-category")
        missing_category.pop("product_category")
        missing_response = client.post("/api/v1/chat", json=missing_category)

        future = _policy_payload("家具保修多少年", "au", "future-date")
        future["policy_as_of"] = "2099-01-01"
        future_response = client.post("/api/v1/chat", json=future)

        normal_response = client.post(
            "/api/v1/chat",
            json=_policy_payload("家具普通退货期限是多少天", "us", "normal-return"),
        )

    assert missing_response.status_code == 200
    assert missing_response.json()["refused"] is True
    assert missing_response.json()["citations"] == []
    assert "商品类别" in missing_response.json()["answer"]
    assert DEMO_DISCLOSURE in missing_response.json()["answer"]
    assert future_response.status_code == 422
    assert normal_response.status_code == 200
    assert normal_response.json()["refused"] is False
    assert "30 天" in normal_response.json()["answer"]
    assert DEMO_DISCLOSURE in normal_response.json()["answer"]
    _assert_demo_citations(normal_response.json(), "us")
