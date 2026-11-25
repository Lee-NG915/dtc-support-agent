"""Consumer GitHub session, OAuth config and self-service ownership tests."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.dependencies import ConsumerActor
from app.api.routes.chat import _apply_consumer_identity
from app.config.settings import Settings
from app.core.security import (
    create_consumer_session_token,
    verify_consumer_session_token,
)
from app.main import create_app
from app.repositories.models import Base
from app.schemas.chat import ChatRequest
from app.services.seed import seed_demo_data

SECRET = "test-consumer-secret-0123456789abcdef"


@pytest.fixture(scope="module", autouse=True)
def _seed_demo_orders():
    async def seed():
        engine = create_async_engine("sqlite+aiosqlite:///./data/local/test.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await seed_demo_data(session)
            await session.commit()
        await engine.dispose()

    import asyncio

    asyncio.run(seed())


def _settings(**overrides) -> Settings:
    values = {
        "consumer_auth_enabled": True,
        "consumer_session_secret": SECRET,
        "consumer_demo_github_ids": "12345",
        "github_client_id": "cid",
        "github_client_secret": "csecret",
        "github_oauth_redirect_url": "http://localhost/auth/callback",
    }
    values.update(overrides)
    return Settings(**values)


def _cookie(github_id: str = "12345", login: str = "demo") -> str:
    return create_consumer_session_token(
        github_id=github_id, login=login, secret=SECRET, ttl_seconds=3600
    )


def test_consumer_session_roundtrip_and_tamper_rejection() -> None:
    token = _cookie()
    assert verify_consumer_session_token(token, secret=SECRET) == ("12345", "demo")
    assert (
        verify_consumer_session_token(token, secret="wrong-secret-0123456789abcdef012345") is None
    )
    assert verify_consumer_session_token(token[:-2] + "xx", secret=SECRET) is None
    expired = create_consumer_session_token(
        github_id="12345",
        login="demo",
        secret=SECRET,
        ttl_seconds=60,
        now=datetime.now(UTC) - timedelta(hours=2),
    )
    assert verify_consumer_session_token(expired, secret=SECRET) is None


def test_consumer_auth_requires_session_secret() -> None:
    try:
        _settings(consumer_session_secret="")
    except ValueError as exc:
        assert "at least 32 characters" in str(exc)
    else:
        raise AssertionError("expected secret validation to fail")


def test_auth_me_reports_signed_out_and_signed_in() -> None:
    with TestClient(create_app(_settings())) as client:
        assert client.get("/api/v1/auth/me").json() == {"signed_in": False}
        client.cookies.set("dtc_consumer_session", _cookie())
        body = client.get("/api/v1/auth/me").json()
        assert body["signed_in"] is True
        assert body["github_id"] == "12345"
        assert body["login"] == "demo"
        assert body["demo_data_available"] is True


def test_consumer_orders_require_session() -> None:
    with TestClient(create_app(_settings())) as client:
        response = client.get("/api/v1/consumer/orders")
        assert response.status_code == 401


def test_consumer_orders_unmapped_identity_sees_empty_data() -> None:
    with TestClient(create_app(_settings())) as client:
        client.cookies.set("dtc_consumer_session", _cookie(github_id="999", login="nobody"))
        body = client.get("/api/v1/consumer/orders").json()
        assert body == {"items": [], "total": 0}
        response = client.get("/api/v1/consumer/orders/ORD10001")
        assert response.status_code == 400


def test_consumer_orders_mapped_identity_sees_demo_orders() -> None:
    with TestClient(create_app(_settings())) as client:
        client.cookies.set("dtc_consumer_session", _cookie())
        body = client.get("/api/v1/consumer/orders").json()
        assert body["total"] >= 1
        order_ids = {item["order_id"] for item in body["items"]}
        assert "ORD10001" in order_ids

        detail = client.get("/api/v1/consumer/orders/ORD10001").json()
        assert detail["order_id"] == "ORD10001"
        assert detail["items"]

        shipment = client.get("/api/v1/consumer/orders/ORD10001/shipment").json()
        assert shipment["order_id"] == "ORD10001"
        assert shipment["events"]

        after_sales = client.get("/api/v1/consumer/after-sales").json()
        assert after_sales["total"] >= 0


def test_chat_requires_session_in_consumer_mode() -> None:
    with TestClient(create_app(_settings())) as client:
        response = client.post("/api/v1/chat", json={"message": "你好", "user_id": "u_demo"})
        assert response.status_code == 401


def test_chat_identity_is_overridden_by_session() -> None:
    container_settings = _settings()
    actor = ConsumerActor("12345", "demo", "u_demo")
    payload = ChatRequest(message="查订单", user_id="someone_else")
    resolved = _apply_consumer_identity(
        payload, actor, container=type("C", (), {"settings": container_settings})()
    )
    assert resolved.user_id == "u_demo"

    unmapped = ConsumerActor("999", "nobody", None)
    resolved = _apply_consumer_identity(
        payload, unmapped, container=type("C", (), {"settings": container_settings})()
    )
    assert resolved.user_id == "gh_999"
