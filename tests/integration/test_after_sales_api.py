from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import Settings
from app.connectors.errors import ConnectorUnavailable
from app.core.security import generate_staff_token
from app.core.time import utcnow
from app.main import create_app
from app.repositories.db import init_schema
from app.repositories.models import SessionRow
from app.repositories.staff_repository import StaffRepository

ROOT = Path(__file__).resolve().parents[2]
PNG_SHA256 = "431ced6916a2a21a156e38701afe55bbd7f88969fbbfc56d7fe099d47f265460"


def _settings(tmp_path: Path, *, enabled: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        app_env="testing",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        after_sales_enabled=enabled,
        business_connector="fake",
        deepseek_api_key="",
        embedding_api_key="",
        local_embedding_enabled=False,
        cross_encoder_enabled=False,
        cross_encoder_force=False,
        unstructured_enabled=False,
        mcp_enabled=False,
        redis_url="",
        qdrant_url="",
        knowledge_dir=str(ROOT / "knowledge"),
    )


async def _seed_staff(database_url: str) -> dict[str, str]:
    engine = create_async_engine(database_url)
    try:
        await init_schema(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        tokens: dict[str, str] = {}
        specs = [
            ("support_au", ("support",), ("au",), utcnow() + timedelta(days=1), True),
            ("approver_au", ("approver",), ("au", "sg"), utcnow() + timedelta(days=1), True),
            ("auditor_all", ("auditor",), ("au", "sg", "us"), utcnow() + timedelta(days=1), True),
            ("operator_au", ("operator",), ("au",), utcnow() + timedelta(days=1), True),
            ("support_us", ("support",), ("us",), utcnow() + timedelta(days=1), True),
            ("expired", ("support",), ("au",), utcnow() - timedelta(seconds=1), True),
            ("disabled", ("support",), ("au",), utcnow() + timedelta(days=1), False),
        ]
        async with factory.begin() as session:
            repository = StaffRepository(session)
            for name, roles, markets, expires_at, enabled in specs:
                token = generate_staff_token()
                tokens[name] = token
                await repository.create(
                    staff_id=f"staff_{name}",
                    display_name=f"Synthetic {name}",
                    token=token,
                    roles=roles,
                    allowed_markets=markets,
                    expires_at=expires_at,
                    enabled=enabled,
                )
        return tokens
    finally:
        await engine.dispose()


async def _session_owner(database_url: str, session_id: str) -> str | None:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with factory() as session:
            row = await session.get(SessionRow, session_id)
            return row.user_id if row is not None else None
    finally:
        await engine.dispose()


async def _seed_foreign_session(database_url: str, session_id: str, owner: str) -> None:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with factory.begin() as session:
            session.add(SessionRow(session_id=session_id, user_id=owner))
    finally:
        await engine.dispose()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_feature_disabled_keeps_demo_and_hides_after_sales(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=False)
    app = create_app(settings)
    with TestClient(app) as client:
        hidden = client.get("/api/v1/after-sales/me")
        assert hidden.status_code == 404
        assert hidden.json()["error_code"] == "AFTER_SALES_DISABLED"

        chat = client.post(
            "/api/v1/chat",
            json={"message": "你好", "user_id": "u_demo"},
        )
        assert chat.status_code == 200
        assert app.state.container.settings is settings
        assert "create_refund_request" in app.state.container.chat_service.registry.names()


def test_missing_expired_disabled_and_unknown_tokens_never_call_connector(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    tokens = asyncio.run(_seed_staff(settings.database_url))
    app = create_app(settings)
    with TestClient(app) as client:
        connector = app.state.container.business_connector
        original_order = connector.get_service_order
        calls: list[dict] = []

        async def counted_order(**kwargs):
            calls.append(kwargs)
            return await original_order(**kwargs)

        connector.get_service_order = counted_order  # type: ignore[method-assign]

        assert client.get("/api/v1/after-sales/me").status_code == 401
        assert (
            client.get(
                "/api/v1/after-sales/me",
                headers={"Authorization": "Basic nope"},
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/api/v1/after-sales/me",
                headers=_auth(tokens["expired"]),
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/api/v1/after-sales/me",
                headers=_auth(tokens["disabled"]),
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/api/v1/after-sales/au/orders/ord_au_00000042",
                headers=_auth(tokens["expired"]),
            ).status_code
            == 401
        )
        assert calls == []

        me = client.get("/api/v1/after-sales/me", headers=_auth(tokens["support_au"]))
        assert me.status_code == 200
        body = me.json()
        assert body == {
            "staff_id": "staff_support_au",
            "display_name": "Synthetic support_au",
            "roles": ["support"],
            "allowed_markets": ["au"],
        }
        assert "token" not in str(body)
        assert "sha256" not in str(body)

        order = client.get(
            "/api/v1/after-sales/au/orders/ord_au_00000042",
            headers=_auth(tokens["support_au"]),
        )
        assert order.status_code == 200
        assert order.json()["order_id"] == "ord_au_00000042"
        assert calls == [{"market": "au", "order_id": "ord_au_00000042"}]


def test_role_market_and_id_checks_happen_before_connector(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    tokens = asyncio.run(_seed_staff(settings.database_url))
    app = create_app(settings)
    with TestClient(app) as client:
        connector = app.state.container.business_connector
        original_order = connector.get_service_order
        calls = 0

        async def counted(**kwargs):
            nonlocal calls
            calls += 1
            return await original_order(**kwargs)

        connector.get_service_order = counted  # type: ignore[method-assign]

        cross_market = client.get(
            "/api/v1/after-sales/sg/orders/ord_sg_00000042",
            headers=_auth(tokens["support_au"]),
        )
        assert cross_market.status_code == 403
        assert cross_market.json()["error_code"] == "FORBIDDEN_MARKET"

        operator = client.get(
            "/api/v1/after-sales/au/orders/ord_au_00000042",
            headers=_auth(tokens["operator_au"]),
        )
        assert operator.status_code == 403
        assert operator.json()["error_code"] == "FORBIDDEN_ROLE"

        operator_attachment = client.get(
            "/api/v1/after-sales/au/attachments/att_0000000000a1/content",
            headers=_auth(tokens["operator_au"]),
        )
        assert operator_attachment.status_code == 403
        assert operator_attachment.json()["error_code"] == "FORBIDDEN_ROLE"

        invalid_market = client.get(
            "/api/v1/after-sales/xx/orders/ord_au_00000042",
            headers=_auth(tokens["support_au"]),
        )
        assert invalid_market.status_code == 400
        assert invalid_market.json()["error_code"] == "INVALID_REQUEST"

        invalid_id = client.get(
            f"/api/v1/after-sales/au/orders/{'x' * 129}",
            headers=_auth(tokens["support_au"]),
        )
        assert invalid_id.status_code == 400
        assert invalid_id.json()["error_code"] == "INVALID_REQUEST"
        assert calls == 0

        for name, market, order_id in (
            ("approver_au", "au", "ord_au_00000042"),
            ("auditor_all", "us", "ord_us_00000042"),
            ("support_us", "us", "ord_us_00000042"),
        ):
            response = client.get(
                f"/api/v1/after-sales/{market}/orders/{order_id}",
                headers=_auth(tokens[name]),
            )
            assert response.status_code == 200
        assert calls == 3


def test_shipment_and_attachment_content_are_authorized_and_masked(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    tokens = asyncio.run(_seed_staff(settings.database_url))
    app = create_app(settings)
    with TestClient(app) as client:
        headers = _auth(tokens["support_au"])
        shipment = client.get(
            "/api/v1/after-sales/au/orders/ord_au_00000042/shipment",
            headers=headers,
        )
        assert shipment.status_code == 200
        assert shipment.json()["events"][-1]["status"] == "delivered"

        attachment = client.get(
            "/api/v1/after-sales/au/attachments/att_0000000000a1/content",
            headers=headers,
        )
        assert attachment.status_code == 200
        assert attachment.headers["content-type"] == "image/png"
        assert hashlib.sha256(attachment.content).hexdigest() == PNG_SHA256
        assert "invalid" not in attachment.headers.get("content-disposition", "")
        assert "location" not in attachment.headers

        expired = client.get(
            "/api/v1/after-sales/au/attachments/att_0000000000e1/content",
            headers=headers,
        )
        assert expired.status_code == 410
        assert expired.json()["error_code"] == "ATTACHMENT_EXPIRED"

        missing = client.get(
            "/api/v1/after-sales/au/attachments/att_0000000000ff/content",
            headers=headers,
        )
        assert missing.status_code == 404
        assert missing.json()["error_code"] == "NOT_FOUND"


def test_connector_unavailable_is_mapped_without_echoing_details(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    tokens = asyncio.run(_seed_staff(settings.database_url))
    app = create_app(settings)
    with TestClient(app) as client:
        connector = app.state.container.business_connector

        async def unavailable(**kwargs):
            raise ConnectorUnavailable(create_operation=False)

        connector.get_service_order = unavailable  # type: ignore[method-assign]
        response = client.get(
            "/api/v1/after-sales/au/orders/ord_au_00000042",
            headers=_auth(tokens["support_au"]),
        )
        assert response.status_code == 503
        body = response.json()
        assert body["error_code"] == "CONNECTOR_UNAVAILABLE"
        assert "http" not in body["message"]


def test_enabled_chat_requires_staff_replaces_identity_and_forces_epig_scope(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    tokens = asyncio.run(_seed_staff(settings.database_url))
    asyncio.run(_seed_foreign_session(settings.database_url, "sess_foreign", "staff_approver_au"))
    app = create_app(settings)
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/v1/chat",
                json={"message": "你好", "user_id": "forged", "market": "au"},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/v1/chat/stream",
                json={"message": "你好", "user_id": "forged", "market": "au"},
            ).status_code
            == 401
        )

        headers = _auth(tokens["support_au"])
        response = client.post(
            "/api/v1/chat",
            headers=headers,
            json={
                "message": "你好",
                "user_id": "forged",
                "brand": "upstream_demo",
                "market": "au",
                "product_category": "家居",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert "E-pig" in body["answer"]
        owner = asyncio.run(_session_owner(settings.database_url, body["session_id"]))
        assert owner == "staff_support_au"

        cross_market = client.post(
            "/api/v1/chat",
            headers=headers,
            json={"message": "你好", "market": "us", "product_category": "家居"},
        )
        assert cross_market.status_code == 403
        assert cross_market.json()["error_code"] == "FORBIDDEN_MARKET"

        missing_market = client.post(
            "/api/v1/chat",
            headers=headers,
            json={"message": "你好", "product_category": "家居"},
        )
        assert missing_market.status_code == 403
        assert missing_market.json()["error_code"] == "FORBIDDEN_MARKET"

        future_policy = client.post(
            "/api/v1/chat",
            headers=headers,
            json={
                "message": "你好",
                "market": "au",
                "product_category": "家居",
                "policy_as_of": "2999-01-01",
            },
        )
        assert future_policy.status_code == 400
        assert future_policy.json()["error_code"] == "INVALID_REQUEST"

        legacy_confirm = client.post(
            "/api/v1/chat",
            headers=headers,
            json={
                "message": "帮我退款",
                "market": "au",
                "product_category": "家居",
                "confirm_action_id": "act_legacy",
            },
        )
        assert legacy_confirm.status_code == 400
        assert legacy_confirm.json()["error_code"] == "INVALID_REQUEST"

        foreign_session = client.post(
            "/api/v1/chat",
            headers=headers,
            json={
                "message": "你好",
                "market": "au",
                "product_category": "家居",
                "session_id": "sess_foreign",
            },
        )
        assert foreign_session.status_code == 404
        assert foreign_session.json()["error_code"] == "NOT_FOUND"

        names = set(app.state.container.chat_service.registry.names())
        assert "create_refund_request" not in names
        assert "create_return_request" not in names
        assert "cancel_order" not in names


def test_chat_stream_uses_the_staff_identity_and_rejects_privilege_escalation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    tokens = asyncio.run(_seed_staff(settings.database_url))
    asyncio.run(
        _seed_foreign_session(settings.database_url, "sess_foreign_stream", "staff_approver_au")
    )
    app = create_app(settings)
    with TestClient(app) as client:
        headers = _auth(tokens["support_au"])
        with client.stream(
            "POST",
            "/api/v1/chat/stream",
            headers=headers,
            json={
                "message": "你好",
                "user_id": "forged",
                "brand": "upstream_demo",
                "market": "au",
                "product_category": "家居",
            },
        ) as response:
            assert response.status_code == 200
            lines = [line for line in response.iter_lines() if line]

        events = [
            json.loads(line[5:].strip())
            for line in lines
            if line.startswith("data:") and line[5:].strip().startswith("{")
        ]
        answer = next(item["data"] for item in events if item.get("event") == "answer")
        assert "E-pig" in answer["answer"]
        owner = asyncio.run(_session_owner(settings.database_url, answer["session_id"]))
        assert owner == "staff_support_au"

        cross_market = client.post(
            "/api/v1/chat/stream",
            headers=headers,
            json={"message": "你好", "market": "us", "product_category": "家居"},
        )
        assert cross_market.status_code == 403
        assert cross_market.json()["error_code"] == "FORBIDDEN_MARKET"

        foreign_session = client.post(
            "/api/v1/chat/stream",
            headers=headers,
            json={
                "message": "你好",
                "market": "au",
                "product_category": "家居",
                "session_id": "sess_foreign_stream",
            },
        )
        assert foreign_session.status_code == 404
        assert foreign_session.json()["error_code"] == "NOT_FOUND"

        missing_auth = client.post(
            "/api/v1/chat/stream",
            json={"message": "你好", "market": "au"},
        )
        assert missing_auth.status_code == 401
        assert missing_auth.json()["error_code"] == "UNAUTHORIZED"
        assert tokens["support_au"] not in missing_auth.text


def test_staff_errors_never_echo_credentials_or_path_values(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    tokens = asyncio.run(_seed_staff(settings.database_url))
    app = create_app(settings)
    with TestClient(app) as client:
        token_marker = "dtc_staff_SECRET-MARKER-7a1f"
        unauthorized = client.get(
            "/api/v1/after-sales/me",
            headers={"Authorization": f"Bearer {token_marker}"},
        )
        assert unauthorized.status_code == 401
        assert token_marker not in unauthorized.text
        assert "sha256" not in unauthorized.text

        market_marker = "MARKET-SECRET-MARKER"
        invalid_market = client.get(
            f"/api/v1/after-sales/{market_marker}/orders/ord_au_00000042",
            headers=_auth(tokens["support_au"]),
        )
        assert invalid_market.status_code == 400
        assert market_marker not in invalid_market.text


def test_no_public_after_sales_write_route_is_exposed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    tokens = asyncio.run(_seed_staff(settings.database_url))
    app = create_app(settings)
    with TestClient(app) as client:
        document = client.get("/openapi.json").json()
        after_sales_methods = {
            path: set(operations)
            for path, operations in document["paths"].items()
            if path.startswith("/api/v1/after-sales")
        }
        assert after_sales_methods
        # Internal workflow POSTs are allowed, but there is still no direct
        # ticket or execution endpoint. DTC-2c adds only the operator read
        # surface: a sanitised list/detail and one bounded receipt lookup.
        assert not any(
            marker in path for path in after_sales_methods for marker in ("tickets", "execute")
        )
        operator_paths = {
            path: methods for path, methods in after_sales_methods.items() if "/operations" in path
        }
        assert operator_paths == {
            "/api/v1/after-sales/{market}/operations": {"get"},
            "/api/v1/after-sales/{market}/operations/{operation_id}": {"get"},
            "/api/v1/after-sales/{market}/operations/{operation_id}/lookup": {"post"},
        }
        assert {method for methods in after_sales_methods.values() for method in methods} <= {
            "get",
            "post",
        }

        response = client.post(
            "/api/v1/after-sales/au/service/tickets",
            headers=_auth(tokens["approver_au"]),
            json={"request_id": "asr_000000000042"},
        )
        assert response.status_code in {404, 405}
