from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import Settings
from app.connectors.models import ServiceRequestFeed
from app.core.exceptions import RevisionConflictError, StateConflictError
from app.core.security import generate_staff_token
from app.core.time import utcnow
from app.main import create_app
from app.repositories.after_sales_repository import AfterSalesRepository
from app.repositories.db import init_schema
from app.repositories.models import (
    AfterSalesApprovalRow,
    AfterSalesCaseRow,
    AfterSalesEventRow,
    AfterSalesOperationRow,
    AfterSalesStaffRow,
)
from app.repositories.staff_repository import StaffRepository
from app.schemas.after_sales import StaffActor

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Ctx:
    client: TestClient
    app: Any
    tokens: dict[str, str]
    database_url: str


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "app_env": "testing",
        "log_level": "CRITICAL",
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        "after_sales_enabled": True,
        "business_connector": "fake",
        "deepseek_api_key": "",
        "embedding_api_key": "",
        "local_embedding_enabled": False,
        "cross_encoder_enabled": False,
        "cross_encoder_force": False,
        "unstructured_enabled": False,
        "mcp_enabled": False,
        "redis_url": "",
        "qdrant_url": "",
        "knowledge_dir": str(ROOT / "knowledge"),
    }
    values.update(overrides)
    return Settings(**values)


async def _seed_staff(database_url: str) -> dict[str, str]:
    engine = create_async_engine(database_url)
    try:
        await init_schema(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        specs = [
            ("staff_support_au", ("support",), ("au",)),
            ("staff_support_us", ("support",), ("us",)),
            ("staff_approver_au", ("approver",), ("au", "sg")),
            ("staff_approver_us", ("approver",), ("us",)),
            ("staff_dual", ("support", "approver"), ("au",)),
            ("staff_auditor", ("auditor",), ("au", "sg", "us")),
            ("staff_operator", ("operator",), ("au",)),
        ]
        tokens: dict[str, str] = {}
        async with factory.begin() as session:
            repository = StaffRepository(session)
            for staff_id, roles, markets in specs:
                token = generate_staff_token()
                tokens[staff_id] = token
                await repository.create(
                    staff_id=staff_id,
                    display_name=f"Synthetic {staff_id}",
                    token=token,
                    roles=roles,
                    allowed_markets=markets,
                    expires_at=utcnow() + timedelta(days=1),
                )
        return tokens
    finally:
        await engine.dispose()


@pytest.fixture
def ctx(tmp_path: Path) -> Ctx:
    settings = _settings(tmp_path)
    tokens = asyncio.run(_seed_staff(settings.database_url))
    app = create_app(settings)
    with TestClient(app) as client:
        yield Ctx(client=client, app=app, tokens=tokens, database_url=settings.database_url)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _sync(ctx: Ctx, market: str = "au", staff: str = "staff_support_au") -> dict[str, Any]:
    response = ctx.client.post(
        f"/api/v1/after-sales/{market}/cases/sync",
        headers=_auth(ctx.tokens[staff]),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _cases(
    ctx: Ctx,
    market: str = "au",
    staff: str = "staff_support_au",
    **params: Any,
) -> dict[str, Any]:
    response = ctx.client.get(
        f"/api/v1/after-sales/{market}/cases",
        headers=_auth(ctx.tokens[staff]),
        params=params,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _detail(ctx: Ctx, case_id: int, market: str = "au", staff: str = "staff_support_au"):
    response = ctx.client.get(
        f"/api/v1/after-sales/{market}/cases/{case_id}",
        headers=_auth(ctx.tokens[staff]),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _document_topic(chunk: Any) -> str:
    if chunk.source_path:
        return chunk.source_path.rsplit("/", 1)[-1].removesuffix(".md")
    return chunk.document_id.rsplit("_", 1)[0]


def _evidence_id_for(ctx: Ctx, topic: str, market: str = "au") -> str:
    return next(
        chunk.chunk_id
        for chunk in ctx.app.state.container.retriever.corpus
        if chunk.brand == "epig"
        and chunk.market == market
        and "homewares" in chunk.product_categories
        and _document_topic(chunk) == topic
    )


def _evidence_id(ctx: Ctx, market: str = "au") -> str:
    corpus = ctx.app.state.container.retriever.corpus
    preferred = [
        chunk.chunk_id
        for chunk in corpus
        if chunk.brand == "epig"
        and chunk.market == market
        and "homewares" in chunk.product_categories
        and _document_topic(chunk) == "faults-missing"
    ]
    if preferred:
        return preferred[0]
    return next(
        chunk.chunk_id
        for chunk in corpus
        if chunk.brand == "epig"
        and chunk.market == market
        and "homewares" in chunk.product_categories
    )


def _draft(
    ctx: Ctx,
    case_id: int,
    revision: int,
    evidence_ids: list[str] | None = None,
    *,
    staff: str = "staff_support_au",
    **overrides: Any,
):
    body: dict[str, Any] = {
        "expected_revision": revision,
        "action": "reship",
        "internal_summary": "Manual review of the synthetic damaged-item request.",
        "consumer_reply": "We will reship the item and share the new tracking number.",
        "evidence_chunk_ids": evidence_ids,
    }
    if evidence_ids is None:
        body["evidence_chunk_ids"] = [_evidence_id(ctx)]
    body.update(overrides)
    return ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/drafts",
        headers=_auth(ctx.tokens[staff]),
        json=body,
    )


def _submit(ctx: Ctx, case_id: int, version: int, revision: int, staff="staff_support_au"):
    return ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/drafts/{version}/submit",
        headers=_auth(ctx.tokens[staff]),
        json={"expected_revision": revision},
    )


def _material(
    ctx: Ctx,
    case_id: int,
    revision: int,
    material_type: str,
    *,
    source: str = "Synthetic check with a recorded source",
    content: str = "A synthetic agent verified the item condition.",
    **extra: Any,
):
    body: dict[str, Any] = {
        "expected_revision": revision,
        "material_type": material_type,
        "source": source,
        "content": content,
    }
    body.update(extra)
    return ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/materials",
        headers=_auth(ctx.tokens["staff_support_au"]),
        json=body,
    )


def _approve(
    ctx: Ctx,
    case_id: int,
    approval_id: str,
    revision: int,
    staff="staff_approver_au",
):
    return ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/approvals/{approval_id}/approve",
        headers=_auth(ctx.tokens[staff]),
        json={"expected_revision": revision},
    )


def _reject(
    ctx: Ctx,
    case_id: int,
    approval_id: str,
    revision: int,
    reason: str | None,
    staff="staff_approver_au",
):
    body: dict[str, Any] = {"expected_revision": revision}
    if reason is not None:
        body["reason"] = reason
    return ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/approvals/{approval_id}/reject",
        headers=_auth(ctx.tokens[staff]),
        json=body,
    )


def _prepare_submitted(ctx: Ctx) -> tuple[int, dict[str, Any], str]:
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    created = _draft(ctx, case_id, _detail(ctx, case_id)["revision"])
    assert created.status_code == 200, created.text
    detail = created.json()
    submitted = _submit(ctx, case_id, detail["current_draft"]["version"], detail["revision"])
    assert submitted.status_code == 200, submitted.text
    detail = submitted.json()
    return case_id, detail, detail["approvals"][-1]["approval_id"]


def _set_remote_ticket(
    ctx: Ctx,
    *,
    ticket_id: str = "tic_000000000043",
    sequence: str = "00000000000000000043",
    market: str = "au",
) -> None:
    state = ctx.app.state.container.business_connector._state.requests[(market, "asr_000000000042")]
    state.request = state.request.model_copy(
        update={
            "status": "ticket_created",
            "ticket_id": ticket_id,
            "sequence": sequence,
            "updated_at": state.request.updated_at + timedelta(minutes=10),
        }
    )


async def _update_row(database_url: str, model, pk: Any, **values: Any) -> None:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with factory.begin() as session:
            row = await session.get(model, pk)
            assert row is not None
            for key, value in values.items():
                setattr(row, key, value)
    finally:
        await engine.dispose()


async def _fetch_row(database_url: str, model, pk: Any):
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with factory() as session:
            return await session.get(model, pk)
    finally:
        await engine.dispose()


async def _count(database_url: str, model) -> int:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with factory() as session:
            return int(await session.scalar(select(func.count()).select_from(model)) or 0)
    finally:
        await engine.dispose()


async def _rows(database_url: str, model, order_by=None) -> list[Any]:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with factory() as session:
            statement = select(model)
            if order_by is not None:
                statement = statement.order_by(order_by)
            return list(await session.scalars(statement))
    finally:
        await engine.dispose()


def _cursor_row(database_url: str):
    async def load():
        engine = create_async_engine(database_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
            async with factory() as session:
                from app.repositories.models import AfterSalesSyncCursorRow

                return await session.get(AfterSalesSyncCursorRow, "au")
        finally:
            await engine.dispose()

    return asyncio.run(load())


# ---------------------------------------------------------------- sync/read


def test_sync_is_idempotent_filterable_and_safely_summarised(ctx: Ctx) -> None:
    connector = ctx.app.state.container.business_connector
    original = connector.list_service_requests

    async def replay_full_page(**kwargs: Any):
        return await original(market=kwargs["market"], cursor=None, limit=kwargs["limit"])

    connector.list_service_requests = replay_full_page  # type: ignore[method-assign]
    first = _sync(ctx)
    second = _sync(ctx)
    assert first["created"] == 1
    assert second["created"] == 0 and second["updated"] == 0

    listing = _cases(ctx, status="processing", request_type="damaged", limit=10)
    assert listing["total"] == 1
    item = listing["items"][0]
    assert "description" not in item
    assert "description_summary" not in item
    assert "consumer_reply" not in item
    assert item["attachment_count"] == 1
    assert item["remote_status"] == "pending"
    assert listing["last_success_at"] is not None

    assert _cases(ctx, status="completed")["total"] == 0
    assert _cases(ctx, request_type="return")["total"] == 0
    assert _cases(ctx, limit=1, offset=0)["has_more"] is False
    assert _cases(ctx, limit=1, offset=1)["items"] == []

    events = ctx.client.get(
        f"/api/v1/after-sales/au/cases/{item['case_id']}/events",
        headers=_auth(ctx.tokens["staff_support_au"]),
    )
    assert events.status_code == 200
    assert events.json()["items"][0]["event_type"] == "case_received"


def test_queue_summary_never_copies_consumer_free_text(ctx: Ctx) -> None:
    connector = ctx.app.state.container.business_connector
    state = connector._state.requests[("au", "asr_000000000042")]
    markers = {
        "phone": "+61 400 555 987",
        "name": "Jane Q. Example",
        "address": "17 Example Street, Sydney",
    }
    state.request = state.request.model_copy(
        update={
            "description": (
                f"Call {markers['phone']}; customer {markers['name']} at "
                f"{markers['address']} says the mug arrived cracked."
            )
        }
    )
    _sync(ctx)
    listing = _cases(ctx)
    raw_listing = str(listing)
    for marker in markers.values():
        assert marker not in raw_listing
    assert listing["items"][0]["attachment_count"] == 1

    # Authorised detail views still need the input to do the work.
    case_id = listing["items"][0]["case_id"]
    detail = _detail(ctx, case_id)
    for marker in markers.values():
        assert marker in detail["description"]


def test_pagination_and_market_object_scoping(ctx: Ctx) -> None:
    connector = ctx.app.state.container.business_connector
    original = connector.list_service_requests
    base = connector._state.requests[("au", "asr_000000000042")].request

    async def two_items(**kwargs: Any):
        return ServiceRequestFeed(
            items=[
                base,
                base.model_copy(
                    update={
                        "request_id": "asr_000000000043",
                        "sequence": "00000000000000000043",
                    }
                ),
            ],
            high_watermark="43",
            next_cursor="page2",
            has_more=False,
        )

    connector.list_service_requests = two_items  # type: ignore[method-assign]
    _sync(ctx)
    page_one = _cases(ctx, limit=1, offset=0)
    page_two = _cases(ctx, limit=1, offset=1)
    assert page_one["total"] == 2 and page_one["has_more"] is True
    assert page_two["total"] == 2 and page_two["has_more"] is False
    assert page_one["items"][0]["case_id"] != page_two["items"][0]["case_id"]

    au_case = page_one["items"][0]["case_id"]
    cross_market = ctx.client.get(
        f"/api/v1/after-sales/sg/cases/{au_case}",
        headers=_auth(ctx.tokens["staff_approver_au"]),
    )
    assert cross_market.status_code == 404
    connector.list_service_requests = original  # type: ignore[method-assign]


def test_roles_market_and_feature_boundaries(ctx: Ctx) -> None:
    assert (
        ctx.client.get(
            "/api/v1/after-sales/au/cases",
            headers=_auth(ctx.tokens["staff_operator"]),
        ).status_code
        == 403
    )
    assert (
        ctx.client.get(
            "/api/v1/after-sales/au/cases",
            headers=_auth(ctx.tokens["staff_support_us"]),
        ).status_code
        == 403
    )
    assert (
        ctx.client.post(
            "/api/v1/after-sales/au/cases/sync",
            headers=_auth(ctx.tokens["staff_approver_au"]),
        ).status_code
        == 403
    )
    assert (
        ctx.client.post(
            "/api/v1/after-sales/au/cases/sync",
            headers=_auth(ctx.tokens["staff_auditor"]),
        ).status_code
        == 403
    )
    assert ctx.client.get("/api/v1/after-sales/au/cases").status_code == 401
    assert (
        ctx.client.get(
            f"/api/v1/after-sales/{'x' * 20}/cases",
            headers=_auth(ctx.tokens["staff_support_au"]),
        ).status_code
        == 400
    )
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    auditor_read = ctx.client.get(
        f"/api/v1/after-sales/au/cases/{case_id}",
        headers=_auth(ctx.tokens["staff_auditor"]),
    )
    assert auditor_read.status_code == 200
    assert "description" in auditor_read.json()


# ------------------------------------------------------------------ materials


def test_material_append_requires_revision_and_is_audited(ctx: Ctx) -> None:
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    revision = _detail(ctx, case_id)["revision"]

    stale = ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/materials",
        headers=_auth(ctx.tokens["staff_support_au"]),
        json={
            "expected_revision": revision + 1,
            "material_type": "manual_verification",
            "source": "Synthetic warehouse phone call",
            "content": "The synthetic warehouse confirmed the parcel was crushed.",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "REVISION_CONFLICT"

    forged = ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/materials",
        headers=_auth(ctx.tokens["staff_support_au"]),
        json={
            "expected_revision": revision,
            "material_type": "manual_verification",
            "source": "Synthetic warehouse phone call",
            "content": "confirmed",
            "created_by_staff_id": "forged",
        },
    )
    assert forged.status_code == 422

    added = ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/materials",
        headers=_auth(ctx.tokens["staff_support_au"]),
        json={
            "expected_revision": revision,
            "material_type": "manual_verification",
            "source": "Synthetic warehouse phone call",
            "content": "The synthetic warehouse confirmed the parcel was crushed.",
        },
    )
    assert added.status_code == 200, added.text
    detail = added.json()
    assert detail["material_revision"] == 1
    assert detail["revision"] == revision + 1
    assert detail["materials"][0]["created_by_staff_id"] == "staff_support_au"

    events = ctx.client.get(
        f"/api/v1/after-sales/au/cases/{case_id}/events",
        headers=_auth(ctx.tokens["staff_support_au"]),
    ).json()
    assert any(item["event_type"] == "material_added" for item in events["items"])


# --------------------------------------------------------------------- drafts


def test_draft_versions_are_immutable_and_require_current_revision(ctx: Ctx) -> None:
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    evidence = _evidence_id(ctx)
    revision = _detail(ctx, case_id)["revision"]

    first = _draft(ctx, case_id, revision, [evidence])
    assert first.status_code == 200, first.text
    first_detail = first.json()
    first_version = first_detail["current_draft"]["version"]
    assert first_detail["current_draft"]["status"] == "editing"

    second = _draft(ctx, case_id, first_detail["revision"], [evidence], action="return")
    assert second.status_code == 200, second.text
    second_detail = second.json()
    assert second_detail["current_draft"]["version"] == first_version + 1
    versions = {item["version"]: item for item in second_detail["drafts"]}
    assert versions[first_version]["status"] == "superseded"
    assert versions[first_version]["action"] == "reship"

    stale_submit = _submit(ctx, case_id, second_detail["current_draft"]["version"], revision)
    assert stale_submit.status_code == 409
    assert stale_submit.json()["error_code"] == "REVISION_CONFLICT"

    old_submit = _submit(ctx, case_id, first_version, second_detail["revision"])
    assert old_submit.status_code == 409
    assert old_submit.json()["error_code"] == "STATE_CONFLICT"


def test_submit_forged_scope_fields_and_missing_evidence_enter_needs_info(
    ctx: Ctx,
) -> None:
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    revision = _detail(ctx, case_id)["revision"]

    forged = _draft(
        ctx,
        case_id,
        revision,
        [],
        policy_version="attacker-version",
    )
    assert forged.status_code == 422

    empty = _draft(ctx, case_id, revision, [])
    assert empty.status_code == 200
    submitted = _submit(
        ctx, case_id, empty.json()["current_draft"]["version"], empty.json()["revision"]
    )
    assert submitted.status_code == 409
    assert submitted.json()["error_code"] == "STATE_CONFLICT"
    assert _detail(ctx, case_id)["state"] == "needs_info"

    other_market_chunk = next(
        chunk.chunk_id
        for chunk in ctx.app.state.container.retriever.corpus
        if chunk.brand == "epig" and chunk.market == "us"
    )
    # Material returns the case to processing before the next draft attempt.
    detail = _detail(ctx, case_id)
    ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/materials",
        headers=_auth(ctx.tokens["staff_support_au"]),
        json={
            "expected_revision": detail["revision"],
            "material_type": "manual_verification",
            "source": "Synthetic follow-up",
            "content": "Synthetic evidence recorded.",
        },
    )
    cross_market = _draft(ctx, case_id, _detail(ctx, case_id)["revision"], [other_market_chunk])
    assert cross_market.status_code == 400
    assert cross_market.json()["error_code"] == "INVALID_REQUEST"

    fabricated = _draft(ctx, case_id, _detail(ctx, case_id)["revision"], ["chunk_does_not_exist"])
    assert fabricated.status_code == 400


def test_production_without_trusted_sku_mapping_goes_needs_info(tmp_path: Path) -> None:
    settings = _settings(tmp_path, app_env="production")
    tokens = asyncio.run(_seed_staff(settings.database_url))
    app = create_app(settings)
    with TestClient(app) as client:
        ctx = Ctx(client=client, app=app, tokens=tokens, database_url=settings.database_url)
        _sync(ctx)
        case_id = _cases(ctx)["items"][0]["case_id"]
        created = _draft(ctx, case_id, _detail(ctx, case_id)["revision"])
        assert created.status_code == 200
        submitted = _submit(
            ctx,
            case_id,
            created.json()["current_draft"]["version"],
            created.json()["revision"],
        )
        assert submitted.status_code == 409
        assert submitted.json()["error_code"] == "STATE_CONFLICT"
        detail = _detail(ctx, case_id)
        assert detail["state"] == "needs_info"
        assert detail["current_draft"]["status"] == "editing"


# ------------------------------------------------------- action/material guards


def _set_shipment_status(ctx: Ctx, status: str) -> None:
    connector = ctx.app.state.container.business_connector
    key = ("au", "ord_au_00000042")
    shipment = connector._state.shipments[key]
    events = shipment.events
    if status != "delivered":
        events = [event for event in events if event.status != "delivered"]
    connector._state.shipments[key] = shipment.model_copy(
        update={"status": status, "events": events}
    )


def _without_delivered_event(ctx: Ctx) -> None:
    connector = ctx.app.state.container.business_connector
    key = ("au", "ord_au_00000042")
    shipment = connector._state.shipments[key]
    connector._state.shipments[key] = shipment.model_copy(
        update={
            "status": "delivered",
            "events": [event for event in shipment.events if event.status != "delivered"],
        }
    )


def _set_delivered_event_at(ctx: Ctx, occurred_at: datetime) -> None:
    connector = ctx.app.state.container.business_connector
    key = ("au", "ord_au_00000042")
    shipment = connector._state.shipments[key]
    connector._state.shipments[key] = shipment.model_copy(
        update={
            "events": [
                event.model_copy(update={"occurred_at": occurred_at})
                if event.status == "delivered"
                else event
                for event in shipment.events
            ]
        }
    )


def _prepare_return_submitted(ctx: Ctx) -> tuple[int, dict[str, Any], str]:
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    created = _draft(
        ctx,
        case_id,
        _detail(ctx, case_id)["revision"],
        [_evidence_id_for(ctx, "returns")],
        action="return",
    )
    assert created.status_code == 200, created.text
    detail = created.json()
    submitted = _submit(ctx, case_id, detail["current_draft"]["version"], detail["revision"])
    assert submitted.status_code == 200, submitted.text
    detail = submitted.json()
    return case_id, detail, detail["approvals"][-1]["approval_id"]


def test_return_submit_requires_returns_policy_and_real_delivery(ctx: Ctx) -> None:
    _set_shipment_status(ctx, "in_transit")
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    returns_evidence = _evidence_id_for(ctx, "returns")
    faults_evidence = _evidence_id_for(ctx, "faults-missing")

    wrong_policy = _draft(
        ctx, case_id, _detail(ctx, case_id)["revision"], [faults_evidence], action="return"
    )
    submitted = _submit(
        ctx,
        case_id,
        wrong_policy.json()["current_draft"]["version"],
        wrong_policy.json()["revision"],
    )
    assert submitted.status_code == 409
    assert _detail(ctx, case_id)["state"] == "needs_info"
    assert "missing_action_policy" in submitted.json()["message"]

    # A generic internal note is not proof of delivery.
    detail = _detail(ctx, case_id)
    noted = _material(
        ctx,
        case_id,
        detail["revision"],
        "internal_note",
        source="Synthetic call note",
        content="The consumer says it was delivered.",
    )
    assert noted.status_code == 200, noted.text
    proper = _draft(ctx, case_id, noted.json()["revision"], [returns_evidence], action="return")
    assert proper.status_code == 200, proper.text
    no_proof = _submit(
        ctx,
        case_id,
        proper.json()["current_draft"]["version"],
        proper.json()["revision"],
    )
    assert no_proof.status_code == 409
    assert "missing_delivery_proof" in no_proof.json()["message"]
    assert _detail(ctx, case_id)["state"] == "needs_info"

    # A sourced, SKU-linked manual verification with an actual delivery date
    # is accepted and enters the approval snapshot.
    detail = _detail(ctx, case_id)
    verified = _material(
        ctx,
        case_id,
        detail["revision"],
        "manual_verification",
        source="Synthetic courier tracking screen",
        content="Agent checked photos and the carrier delivery record.",
        applies_to_sku="SYNTH-MUG-001",
        delivered_at=(utcnow() - timedelta(days=2)).isoformat(),
    )
    assert verified.status_code == 200, verified.text
    resubmitted = _submit(
        ctx,
        case_id,
        proper.json()["current_draft"]["version"],
        verified.json()["revision"],
    )
    assert resubmitted.status_code == 200, resubmitted.text
    approval_id = resubmitted.json()["approvals"][-1]["approval_id"]
    approval = asyncio.run(_fetch_row(ctx.database_url, AfterSalesApprovalRow, approval_id))
    snapshot = json.loads(approval.snapshot_json)
    assert snapshot["delivery_proof"]["kind"] == "manual_verification"
    assert snapshot["delivery_proof"]["applies_to_sku"] == "SYNTH-MUG-001"
    assert (
        snapshot["delivery_proof"]["material_id"] == verified.json()["materials"][-1]["material_id"]
    )
    assert any(
        item["material_type"] == "manual_verification"
        and item["applies_to_sku"] == "SYNTH-MUG-001"
        and item["delivered_at"] is not None
        for item in resubmitted.json()["materials"]
    )


def test_return_accepts_real_delivered_shipment(ctx: Ctx) -> None:
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    created = _draft(
        ctx,
        case_id,
        _detail(ctx, case_id)["revision"],
        [_evidence_id_for(ctx, "returns")],
        action="return",
    )
    assert created.status_code == 200, created.text
    submitted = _submit(
        ctx,
        case_id,
        created.json()["current_draft"]["version"],
        created.json()["revision"],
    )
    assert submitted.status_code == 200, submitted.text
    approval_id = submitted.json()["approvals"][-1]["approval_id"]
    approval = asyncio.run(_fetch_row(ctx.database_url, AfterSalesApprovalRow, approval_id))
    snapshot = json.loads(approval.snapshot_json)
    assert snapshot["delivery_proof"]["kind"] == "shipment"
    assert snapshot["delivery_proof"]["shipment_id"] == "shp_000000000042"


def test_delivered_status_without_delivered_event_is_not_proof(ctx: Ctx) -> None:
    _without_delivered_event(ctx)
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    created = _draft(
        ctx,
        case_id,
        _detail(ctx, case_id)["revision"],
        [_evidence_id_for(ctx, "returns")],
        action="return",
    )
    assert created.status_code == 200, created.text
    submitted = _submit(
        ctx, case_id, created.json()["current_draft"]["version"], created.json()["revision"]
    )
    assert submitted.status_code == 409
    assert "missing_delivery_proof" in submitted.json()["message"]
    detail = _detail(ctx, case_id)
    assert detail["state"] == "needs_info"
    assert detail["approvals"] == []
    assert detail["operations"] == []


def test_future_delivered_event_is_not_proof(ctx: Ctx) -> None:
    _set_delivered_event_at(ctx, utcnow() + timedelta(days=1))
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    created = _draft(
        ctx,
        case_id,
        _detail(ctx, case_id)["revision"],
        [_evidence_id_for(ctx, "returns")],
        action="return",
    )
    assert created.status_code == 200, created.text
    submitted = _submit(
        ctx, case_id, created.json()["current_draft"]["version"], created.json()["revision"]
    )
    assert submitted.status_code == 409
    assert "delivery_proof_in_future" in submitted.json()["message"]
    detail = _detail(ctx, case_id)
    assert detail["state"] == "needs_info"
    assert detail["approvals"] == []
    assert detail["operations"] == []


def test_sourced_manual_delivery_proof_substitutes_for_missing_event(ctx: Ctx) -> None:
    _without_delivered_event(ctx)
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    created = _draft(
        ctx,
        case_id,
        _detail(ctx, case_id)["revision"],
        [_evidence_id_for(ctx, "returns")],
        action="return",
    )
    assert created.status_code == 200, created.text
    detail = created.json()
    missing = _submit(ctx, case_id, detail["current_draft"]["version"], detail["revision"])
    assert missing.status_code == 409
    assert "missing_delivery_proof" in missing.json()["message"]

    verified = _material(
        ctx,
        case_id,
        _detail(ctx, case_id)["revision"],
        "manual_verification",
        source="Synthetic carrier tracking screen",
        content="Agent verified the actual delivery date against the carrier record.",
        applies_to_sku="SYNTH-MUG-001",
        delivered_at=(utcnow() - timedelta(days=2)).isoformat(),
    )
    assert verified.status_code == 200, verified.text
    submitted = _submit(
        ctx, case_id, detail["current_draft"]["version"], verified.json()["revision"]
    )
    assert submitted.status_code == 200, submitted.text
    approval_id = submitted.json()["approvals"][-1]["approval_id"]
    approval = asyncio.run(_fetch_row(ctx.database_url, AfterSalesApprovalRow, approval_id))
    snapshot = json.loads(approval.snapshot_json)
    assert snapshot["delivery_proof"]["kind"] == "manual_verification"
    assert snapshot["delivery_proof"]["delivered_at"] is not None


def test_approval_revalidates_delivery_event_time(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_return_submitted(ctx)
    _set_delivered_event_at(ctx, utcnow() + timedelta(days=1))
    approved = _approve(ctx, case_id, approval_id, detail["revision"])
    assert approved.status_code == 409
    after = _detail(ctx, case_id)
    approval = next(item for item in after["approvals"] if item["approval_id"] == approval_id)
    assert approval["status"] == "invalidated"
    assert "delivery_proof_changed" in approval["decision_reason"]
    assert after["operations"] == []


def test_unrelated_shipment_note_does_not_invalidate_delivery_proof(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_return_submitted(ctx)
    connector = ctx.app.state.container.business_connector
    key = ("au", "ord_au_00000042")
    shipment = connector._state.shipments[key]
    note = shipment.events[0].model_copy(
        update={
            "event_id": "she_000000000099",
            "description": "Carrier added an unrelated later scan note.",
            "occurred_at": utcnow() - timedelta(hours=1),
        }
    )
    connector._state.shipments[key] = shipment.model_copy(
        update={"events": [*shipment.events, note]}
    )
    approved = _approve(ctx, case_id, approval_id, detail["revision"])
    assert approved.status_code == 200, approved.text
    operations = approved.json()["operations"]
    assert len(operations) == 1
    assert operations[0]["status"] == "prepared"


def test_refund_requires_refunds_policy(ctx: Ctx) -> None:
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    wrong = _draft(
        ctx,
        case_id,
        _detail(ctx, case_id)["revision"],
        [_evidence_id_for(ctx, "faults-missing")],
        action="refund",
    )
    submitted = _submit(
        ctx, case_id, wrong.json()["current_draft"]["version"], wrong.json()["revision"]
    )
    assert submitted.status_code == 409
    assert "missing_action_policy" in submitted.json()["message"]

    detail = _detail(ctx, case_id)
    noted = _material(ctx, case_id, detail["revision"], "internal_note")
    proper = _draft(
        ctx,
        case_id,
        noted.json()["revision"],
        [_evidence_id_for(ctx, "refunds")],
        action="refund",
    )
    accepted = _submit(
        ctx, case_id, proper.json()["current_draft"]["version"], proper.json()["revision"]
    )
    assert accepted.status_code == 200, accepted.text


def test_reship_requires_faults_missing_policy(ctx: Ctx) -> None:
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    wrong = _draft(
        ctx,
        case_id,
        _detail(ctx, case_id)["revision"],
        [_evidence_id_for(ctx, "returns")],
        action="reship",
    )
    submitted = _submit(
        ctx, case_id, wrong.json()["current_draft"]["version"], wrong.json()["revision"]
    )
    assert submitted.status_code == 409
    assert "missing_action_policy" in submitted.json()["message"]

    detail = _detail(ctx, case_id)
    noted = _material(ctx, case_id, detail["revision"], "internal_note")
    proper = _draft(
        ctx,
        case_id,
        noted.json()["revision"],
        [_evidence_id_for(ctx, "faults-missing")],
        action="reship",
    )
    accepted = _submit(
        ctx, case_id, proper.json()["current_draft"]["version"], proper.json()["revision"]
    )
    assert accepted.status_code == 200, accepted.text


def test_reship_requires_condition_evidence(ctx: Ctx) -> None:
    connector = ctx.app.state.container.business_connector
    state = connector._state.requests[("au", "asr_000000000042")]
    state.request = state.request.model_copy(update={"attachments": []})
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    created = _draft(ctx, case_id, _detail(ctx, case_id)["revision"])
    assert created.status_code == 200, created.text
    submitted = _submit(
        ctx, case_id, created.json()["current_draft"]["version"], created.json()["revision"]
    )
    assert submitted.status_code == 409
    assert "missing_condition_evidence" in submitted.json()["message"]
    assert _detail(ctx, case_id)["state"] == "needs_info"

    detail = _detail(ctx, case_id)
    verified = _material(
        ctx,
        case_id,
        detail["revision"],
        "manual_verification",
        source="Synthetic photo review by an authorised agent",
        content="Agent inspected the uploaded images and described the crushed corner.",
    )
    accepted = _submit(
        ctx,
        case_id,
        created.json()["current_draft"]["version"],
        verified.json()["revision"],
    )
    assert accepted.status_code == 200, accepted.text


@pytest.mark.parametrize("action", ["reject", "other"])
def test_reject_or_other_requires_manual_verification(ctx: Ctx, action: str) -> None:
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    created = _draft(
        ctx,
        case_id,
        _detail(ctx, case_id)["revision"],
        [_evidence_id_for(ctx, "faults-missing")],
        action=action,
    )
    assert created.status_code == 200, created.text
    submitted = _submit(
        ctx, case_id, created.json()["current_draft"]["version"], created.json()["revision"]
    )
    assert submitted.status_code == 409
    assert "missing_manual_verification" in submitted.json()["message"]
    assert _detail(ctx, case_id)["state"] == "needs_info"

    detail = _detail(ctx, case_id)
    verified = _material(
        ctx,
        case_id,
        detail["revision"],
        "manual_verification",
        source="Synthetic supervisor review",
        content="Supervisor reviewed the order, photos and applicable policy.",
    )
    accepted = _submit(
        ctx,
        case_id,
        created.json()["current_draft"]["version"],
        verified.json()["revision"],
    )
    assert accepted.status_code == 200, accepted.text


def test_material_delivery_fields_are_typed_and_not_in_the_future(ctx: Ctx) -> None:
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    revision = _detail(ctx, case_id)["revision"]
    future = _material(
        ctx,
        case_id,
        revision,
        "manual_verification",
        applies_to_sku="SYNTH-MUG-001",
        delivered_at=(utcnow() + timedelta(days=1)).isoformat(),
    )
    assert future.status_code == 422
    naive = _material(
        ctx,
        case_id,
        revision,
        "manual_verification",
        applies_to_sku="SYNTH-MUG-001",
        delivered_at="2026-01-14T06:45:00",
    )
    assert naive.status_code == 422
    generic = _material(
        ctx,
        case_id,
        revision,
        "internal_note",
        applies_to_sku="SYNTH-MUG-001",
    )
    assert generic.status_code == 422


# ------------------------------------------------------------------ approvals


def test_approval_prepares_one_operation_and_never_calls_create_ticket(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_submitted(ctx)
    fake = ctx.app.state.container.business_connector
    assert fake.create_ticket_calls == 0

    approved = _approve(ctx, case_id, approval_id, detail["revision"])
    assert approved.status_code == 200, approved.text
    result = approved.json()
    approval = next(item for item in result["approvals"] if item["approval_id"] == approval_id)
    assert approval["status"] == "approved"
    assert result["current_draft"]["status"] == "approved"
    assert len(result["operations"]) == 1
    operation = result["operations"][0]
    assert operation["status"] == "prepared"
    assert operation["payload"]["approval_id"] == approval_id
    assert operation["payload"]["draft_version"] == str(result["current_draft"]["version"])
    assert operation["payload"]["order_version"] == result["order"]["order_version"]
    assert operation["payload"]["policy_version"] == result["current_draft"]["policy_version"]
    assert operation["idempotency_key"].startswith("dtc_")
    assert fake.create_ticket_calls == 0

    repeat = _approve(ctx, case_id, approval_id, detail["revision"])
    assert repeat.status_code == 409
    operations = asyncio.run(_rows(ctx.database_url, AfterSalesOperationRow))
    assert len(operations) == 1
    assert fake.create_ticket_calls == 0


def test_self_approval_is_forbidden_even_with_both_roles(ctx: Ctx) -> None:
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    created = _draft(ctx, case_id, _detail(ctx, case_id)["revision"], staff="staff_dual")
    submitted = _submit(
        ctx,
        case_id,
        created.json()["current_draft"]["version"],
        created.json()["revision"],
        staff="staff_dual",
    )
    assert submitted.status_code == 200, submitted.text
    detail = submitted.json()
    approval_id = detail["approvals"][-1]["approval_id"]

    response = _approve(ctx, case_id, approval_id, detail["revision"], staff="staff_dual")
    assert response.status_code == 403
    assert response.json()["error_code"] == "SELF_APPROVAL_FORBIDDEN"
    assert _detail(ctx, case_id)["approvals"][-1]["status"] == "pending"


def test_internal_rejection_requires_reason_and_creates_no_operation(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_submitted(ctx)
    fake = ctx.app.state.container.business_connector

    missing_reason = _reject(ctx, case_id, approval_id, detail["revision"], None)
    assert missing_reason.status_code == 422

    rejected = _reject(
        ctx,
        case_id,
        approval_id,
        detail["revision"],
        "Synthetic internal reason: evidence needs a manual check.",
    )
    assert rejected.status_code == 200, rejected.text
    result = rejected.json()
    assert result["state"] == "processing"
    assert result["approvals"][-1]["status"] == "rejected"
    assert result["approvals"][-1]["decision_reason"]
    assert result["current_draft"]["status"] == "rejected"
    assert result["operations"] == []
    assert fake.create_ticket_calls == 0

    duplicate = _approve(ctx, case_id, approval_id, result["revision"])
    assert duplicate.status_code == 409


# ----------------------------------------------------- invalidation/expiry


def test_expired_approval_cannot_execute(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_submitted(ctx)
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesApprovalRow,
            approval_id,
            expires_at=utcnow() - timedelta(seconds=1),
        )
    )
    response = _approve(ctx, case_id, approval_id, detail["revision"])
    assert response.status_code == 409
    assert response.json()["error_code"] == "STATE_CONFLICT"
    result = _detail(ctx, case_id)
    assert result["state"] == "processing"
    assert result["approvals"][-1]["status"] == "expired"
    assert result["current_draft"]["status"] == "invalidated"
    assert result["operations"] == []


def test_requester_revocation_invalidates_approval(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_submitted(ctx)
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesStaffRow,
            "staff_support_au",
            roles="auditor",
        )
    )
    response = _approve(ctx, case_id, approval_id, detail["revision"])
    assert response.status_code == 409
    result = _detail(ctx, case_id)
    assert result["approvals"][-1]["status"] == "invalidated"
    assert "requester_authorization_changed" in (result["approvals"][-1]["decision_reason"] or "")
    assert result["operations"] == []


def test_order_version_change_invalidates_approval(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_submitted(ctx)
    connector = ctx.app.state.container.business_connector
    key = ("au", "ord_au_00000042")
    order = connector._state.orders[key]
    connector._state.orders[key] = order.model_copy(update={"order_version": "ov_changed_0001"})
    response = _approve(ctx, case_id, approval_id, detail["revision"])
    assert response.status_code == 409
    result = _detail(ctx, case_id)
    assert result["approvals"][-1]["status"] == "invalidated"
    assert "order_version_changed" in (result["approvals"][-1]["decision_reason"] or "")
    assert result["operations"] == []


def test_attachment_change_invalidates_approval(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_submitted(ctx)
    connector = ctx.app.state.container.business_connector
    key = ("au", "asr_000000000042")
    state = connector._state.requests[key]
    expired = state.request.attachments[0].model_copy(update={"status": "expired"})
    state.request = state.request.model_copy(
        update={
            "attachments": [expired],
            "updated_at": state.request.updated_at + timedelta(minutes=5),
        }
    )
    response = _approve(ctx, case_id, approval_id, detail["revision"])
    assert response.status_code == 409
    result = _detail(ctx, case_id)
    assert result["approvals"][-1]["status"] == "invalidated"
    reason = result["approvals"][-1]["decision_reason"] or ""
    assert "attachments_changed" in reason or "attachment_expired" in reason
    assert result["operations"] == []


def test_policy_evidence_change_invalidates_approval(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_submitted(ctx)
    evidence_id = detail["current_draft"]["evidence"][0]["chunk_id"]
    chunks = ctx.app.state.container.retriever._bm25._chunks
    for index, chunk in enumerate(chunks):
        if chunk.chunk_id == evidence_id:
            chunks[index] = chunk.model_copy(update={"content": chunk.content + " updated"})
            break
    response = _approve(ctx, case_id, approval_id, detail["revision"])
    assert response.status_code == 409
    result = _detail(ctx, case_id)
    assert result["approvals"][-1]["status"] == "invalidated"
    assert "policy_changed" in (result["approvals"][-1]["decision_reason"] or "")
    assert result["operations"] == []


def test_material_change_invalidates_pending_approval(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_submitted(ctx)
    added = ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/materials",
        headers=_auth(ctx.tokens["staff_support_au"]),
        json={
            "expected_revision": detail["revision"],
            "material_type": "manual_verification",
            "source": "Synthetic follow-up",
            "content": "A later synthetic check changed the context.",
        },
    )
    assert added.status_code == 200, added.text
    result = added.json()
    assert result["state"] == "processing"
    approval = next(item for item in result["approvals"] if item["approval_id"] == approval_id)
    assert approval["status"] == "invalidated"
    assert result["current_draft"]["status"] == "invalidated"
    assert result["operations"] == []
    assert _approve(ctx, case_id, approval_id, result["revision"]).status_code == 409


# --------------------------------------------------- prepared failure/rework


def test_prepared_operation_failure_releases_guard_and_allows_resubmit(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_submitted(ctx)
    approved = _approve(ctx, case_id, approval_id, detail["revision"])
    assert approved.status_code == 200
    detail = approved.json()
    old_operation = detail["operations"][0]
    assert old_operation["status"] == "prepared"

    added = ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/materials",
        headers=_auth(ctx.tokens["staff_support_au"]),
        json={
            "expected_revision": detail["revision"],
            "material_type": "manual_verification",
            "source": "Synthetic follow-up",
            "content": "New verified context after approval.",
        },
    )
    assert added.status_code == 200, added.text
    detail = added.json()
    old = next(
        item
        for item in detail["operations"]
        if item["operation_id"] == old_operation["operation_id"]
    )
    assert old["status"] == "failed"
    assert old["failure_reason"] == "materials_changed"
    assert any(
        item["approval_id"] == approval_id and item["status"] == "invalidated"
        for item in detail["approvals"]
    )
    assert detail["state"] == "processing"

    new_draft = _draft(ctx, case_id, detail["revision"])
    assert new_draft.status_code == 200
    submitted = _submit(
        ctx,
        case_id,
        new_draft.json()["current_draft"]["version"],
        new_draft.json()["revision"],
    )
    assert submitted.status_code == 200, submitted.text
    new_approval = submitted.json()["approvals"][-1]["approval_id"]
    reapproved = _approve(ctx, case_id, new_approval, submitted.json()["revision"])
    assert reapproved.status_code == 200, reapproved.text
    operations = reapproved.json()["operations"]
    assert sum(1 for item in operations if item["status"] == "prepared") == 1
    assert any(item["status"] == "failed" for item in operations)
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0


def test_dispatched_or_unknown_operation_keeps_context_and_blocks_second_write(
    ctx: Ctx,
) -> None:
    case_id, detail, approval_id = _prepare_submitted(ctx)
    approved = _approve(ctx, case_id, approval_id, detail["revision"])
    assert approved.status_code == 200
    detail = approved.json()
    operation_id = detail["operations"][0]["operation_id"]
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesOperationRow,
            operation_id,
            status="dispatching",
            dispatched_at=utcnow(),
        )
    )

    added = ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/materials",
        headers=_auth(ctx.tokens["staff_support_au"]),
        json={
            "expected_revision": detail["revision"],
            "material_type": "manual_verification",
            "source": "Synthetic follow-up",
            "content": "Context recorded while dispatch is unknown.",
        },
    )
    assert added.status_code == 200, added.text
    detail = added.json()
    operation = next(item for item in detail["operations"] if item["operation_id"] == operation_id)
    assert operation["status"] == "dispatching"
    approval = next(item for item in detail["approvals"] if item["approval_id"] == approval_id)
    assert approval["status"] == "approved"

    candidate = _draft(ctx, case_id, detail["revision"])
    assert candidate.status_code == 200
    candidate_detail = candidate.json()
    # The executing context keeps the pointer; the new version is a candidate
    # that cannot be submitted while dispatch is unresolved.
    assert candidate_detail["current_draft"]["draft_id"] == detail["current_draft"]["draft_id"]
    candidate_version = max(item["version"] for item in candidate_detail["drafts"])
    assert candidate_version > detail["current_draft"]["version"]
    blocked = _submit(ctx, case_id, candidate_version, candidate_detail["revision"])
    assert blocked.status_code == 409
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0


@pytest.mark.parametrize("state", ["executing", "reconciliation_required"])
def test_unknown_execution_context_allows_candidate_but_blocks_submission(
    ctx: Ctx, state: str
) -> None:
    case_id, detail, approval_id = _prepare_submitted(ctx)
    approved = _approve(ctx, case_id, approval_id, detail["revision"])
    assert approved.status_code == 200, approved.text
    detail = approved.json()
    operation_id = detail["operations"][0]["operation_id"]
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesOperationRow,
            operation_id,
            status="dispatching",
            dispatched_at=utcnow(),
        )
    )
    asyncio.run(_update_row(ctx.database_url, AfterSalesCaseRow, case_id, state=state))

    candidate = _draft(ctx, case_id, detail["revision"])
    assert candidate.status_code == 200, candidate.text
    after = candidate.json()
    assert after["state"] == state
    assert after["current_draft"]["draft_id"] == detail["current_draft"]["draft_id"]
    assert after["current_draft"]["status"] == "approved"
    approval = next(item for item in after["approvals"] if item["approval_id"] == approval_id)
    assert approval["status"] == "approved"
    operation = next(item for item in after["operations"] if item["operation_id"] == operation_id)
    assert operation["status"] == "dispatching"
    candidate_version = max(item["version"] for item in after["drafts"])
    candidate_draft = next(item for item in after["drafts"] if item["version"] == candidate_version)
    assert candidate_draft["status"] == "editing"

    blocked = _submit(ctx, case_id, candidate_version, after["revision"])
    assert blocked.status_code == 409
    assert blocked.json()["error_code"] == "STATE_CONFLICT"
    final = _detail(ctx, case_id)
    assert len(final["operations"]) == 1
    assert final["current_draft"]["draft_id"] == detail["current_draft"]["draft_id"]
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0


def test_completed_case_cannot_add_a_candidate(ctx: Ctx) -> None:
    case_id, detail, _ = _prepare_submitted(ctx)
    asyncio.run(_update_row(ctx.database_url, AfterSalesCaseRow, case_id, state="completed"))
    blocked = _draft(ctx, case_id, detail["revision"])
    assert blocked.status_code == 409
    assert blocked.json()["error_code"] == "STATE_CONFLICT"


# ---------------------------------------------------------------- sync edge


def test_old_pending_feed_cannot_regress_unverified_remote_ticket(ctx: Ctx) -> None:
    connector = ctx.app.state.container.business_connector
    original = connector.list_service_requests
    base = connector._state.requests[("au", "asr_000000000042")].request
    later = base.updated_at + timedelta(minutes=10)

    async def ticketed(**kwargs: Any):
        return ServiceRequestFeed(
            items=[
                base.model_copy(
                    update={
                        "sequence": "00000000000000000043",
                        "status": "ticket_created",
                        "ticket_id": "tic_000000000043",
                        "updated_at": later,
                    }
                )
            ],
            high_watermark="43",
            next_cursor="page2",
            has_more=False,
        )

    connector.list_service_requests = ticketed  # type: ignore[method-assign]
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    unverified = _detail(ctx, case_id)
    # A feed item is not a receipt. 2b must keep the case explicitly pending
    # verification instead of claiming completion.
    assert unverified["state"] == "reconciliation_required"
    assert unverified["remote_status"] == "ticket_created"
    assert unverified["remote_ticket_id"] == "tic_000000000043"
    assert unverified["operations"] == []
    assert unverified["approvals"] == []
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0

    async def stale_pending(**kwargs: Any):
        return ServiceRequestFeed(
            items=[base],
            high_watermark="43",
            next_cursor=connector._encode_cursor("au", 43),
            has_more=False,
        )

    connector.list_service_requests = stale_pending  # type: ignore[method-assign]
    replayed = _sync(ctx)
    assert replayed["stale_ignored"] >= 1
    connector.list_service_requests = original  # type: ignore[method-assign]
    after = _detail(ctx, case_id)
    assert after["state"] == "reconciliation_required"
    assert after["remote_status"] == "ticket_created"
    assert after["remote_ticket_id"] == "tic_000000000043"


def test_new_remote_ticket_stays_explicitly_pending_verification(ctx: Ctx) -> None:
    _set_remote_ticket(ctx)
    synced = _sync(ctx)
    assert synced["created"] == 1

    listing = _cases(ctx, status="reconciliation_required")
    assert listing["total"] == 1
    case_id = listing["items"][0]["case_id"]
    detail = _detail(ctx, case_id)
    assert detail["state"] == "reconciliation_required"
    assert detail["remote_status"] == "ticket_created"
    assert detail["approvals"] == []
    assert detail["operations"] == []
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0

    # Product 7.2: a candidate version may be saved while the remote result is
    # unresolved, but it must not become current or be submitted as a write.
    candidate = _draft(ctx, case_id, detail["revision"])
    assert candidate.status_code == 200, candidate.text
    candidate_detail = candidate.json()
    assert candidate_detail["state"] == "reconciliation_required"
    assert candidate_detail["current_draft"] is None
    candidate_version = max(item["version"] for item in candidate_detail["drafts"])
    blocked = _submit(ctx, case_id, candidate_version, candidate_detail["revision"])
    assert blocked.status_code == 409
    assert blocked.json()["error_code"] == "STATE_CONFLICT"

    events = ctx.client.get(
        f"/api/v1/after-sales/au/cases/{case_id}/events",
        headers=_auth(ctx.tokens["staff_support_au"]),
    ).json()
    assert any(
        item["event_type"] == "case_received" and item["reason"] == "remote_ticket_unverified"
        for item in events["items"]
    )


def test_prepared_approval_survives_remote_ticket_sync(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_submitted(ctx)
    approved = _approve(ctx, case_id, approval_id, detail["revision"])
    assert approved.status_code == 200, approved.text
    approved_detail = approved.json()
    operation_id = approved_detail["operations"][0]["operation_id"]

    _set_remote_ticket(ctx)
    _sync(ctx)

    after = _detail(ctx, case_id)
    assert after["state"] == "reconciliation_required"
    assert after["remote_ticket_id"] == "tic_000000000043"
    operation = next(item for item in after["operations"] if item["operation_id"] == operation_id)
    assert operation["status"] == "prepared"
    approval = next(item for item in after["approvals"] if item["approval_id"] == approval_id)
    assert approval["status"] == "approved"
    assert after["current_draft"]["status"] == "approved"
    assert len(after["operations"]) == 1
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0

    # Pending verification allows an audit candidate but blocks submission and
    # leaves the approved current draft/operation untouched.
    candidate = _draft(ctx, case_id, after["revision"])
    assert candidate.status_code == 200, candidate.text
    candidate_detail = candidate.json()
    assert candidate_detail["current_draft"]["draft_id"] == after["current_draft"]["draft_id"]
    assert candidate_detail["current_draft"]["status"] == "approved"
    candidate_version = max(item["version"] for item in candidate_detail["drafts"])
    blocked = _submit(ctx, case_id, candidate_version, candidate_detail["revision"])
    assert blocked.status_code == 409
    assert blocked.json()["error_code"] == "STATE_CONFLICT"
    assert len(candidate_detail["operations"]) == 1
    assert candidate_detail["operations"][0]["operation_id"] == operation_id


def test_pending_approval_is_preserved_but_blocks_after_remote_ticket(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_submitted(ctx)
    _set_remote_ticket(ctx)
    _sync(ctx)

    after = _detail(ctx, case_id)
    assert after["state"] == "reconciliation_required"
    approval = next(item for item in after["approvals"] if item["approval_id"] == approval_id)
    assert approval["status"] == "pending"
    assert after["current_draft"]["status"] == "pending_approval"
    assert after["operations"] == []

    blocked = _approve(ctx, case_id, approval_id, after["revision"])
    assert blocked.status_code == 409
    assert _detail(ctx, case_id)["operations"] == []
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0


def test_unknown_operation_context_is_not_invalidated_by_remote_ticket(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_submitted(ctx)
    approved = _approve(ctx, case_id, approval_id, detail["revision"])
    assert approved.status_code == 200, approved.text
    operation_id = approved.json()["operations"][0]["operation_id"]
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesOperationRow,
            operation_id,
            status="dispatching",
            dispatched_at=utcnow(),
        )
    )

    _set_remote_ticket(ctx)
    _sync(ctx)

    after = _detail(ctx, case_id)
    assert after["state"] == "reconciliation_required"
    operation = next(item for item in after["operations"] if item["operation_id"] == operation_id)
    assert operation["status"] == "dispatching"
    assert operation["dispatched_at"] is not None
    approval = next(item for item in after["approvals"] if item["approval_id"] == approval_id)
    assert approval["status"] == "approved"
    assert after["current_draft"]["status"] == "approved"
    assert len(after["operations"]) == 1
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0


def test_submit_refuses_remote_ticket_without_creating_an_operation(ctx: Ctx) -> None:
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    created = _draft(ctx, case_id, _detail(ctx, case_id)["revision"])
    assert created.status_code == 200, created.text
    detail = created.json()
    version = detail["current_draft"]["version"]

    _set_remote_ticket(ctx)
    submitted = _submit(ctx, case_id, version, detail["revision"])
    assert submitted.status_code == 409
    assert submitted.json()["error_code"] == "STATE_CONFLICT"

    after = _detail(ctx, case_id)
    assert after["state"] == "reconciliation_required"
    assert after["remote_ticket_id"] == "tic_000000000043"
    assert after["approvals"] == []
    assert after["operations"] == []
    assert after["drafts"][-1]["status"] == "editing"
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0


def test_sync_batch_failure_rolls_back_cases_and_cursor(ctx: Ctx, monkeypatch) -> None:
    connector = ctx.app.state.container.business_connector
    original_list = connector.list_service_requests
    base = connector._state.requests[("au", "asr_000000000042")].request

    async def two_items(**kwargs: Any):
        return ServiceRequestFeed(
            items=[
                base.model_copy(
                    update={"request_id": "asr_000000000043", "sequence": "00000000000000000043"}
                ),
                base.model_copy(
                    update={"request_id": "asr_000000000044", "sequence": "00000000000000000044"}
                ),
            ],
            high_watermark="44",
            next_cursor="page2",
            has_more=False,
        )

    connector.list_service_requests = two_items  # type: ignore[method-assign]
    original_append = AfterSalesRepository.append_event
    calls = {"count": 0}

    async def failing_append(self, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise SQLAlchemyError("synthetic batch failure")
        return await original_append(self, **kwargs)

    monkeypatch.setattr(AfterSalesRepository, "append_event", failing_append)
    response = ctx.client.post(
        "/api/v1/after-sales/au/cases/sync",
        headers=_auth(ctx.tokens["staff_support_au"]),
    )
    assert response.status_code == 503
    assert asyncio.run(_count(ctx.database_url, AfterSalesCaseRow)) == 0
    assert _cursor_row(ctx.database_url) is None

    monkeypatch.setattr(AfterSalesRepository, "append_event", original_append)
    connector.list_service_requests = original_list  # type: ignore[method-assign]
    synced = _sync(ctx)
    assert synced["created"] == 1
    assert _cursor_row(ctx.database_url) is not None


def test_large_decimal_sequence_and_replayed_page_do_not_error(ctx: Ctx) -> None:
    connector = ctx.app.state.container.business_connector
    original = connector.list_service_requests
    state = connector._state.requests[("au", "asr_000000000042")]
    huge_sequence = "99999999999999999999999999999999"
    state.request = state.request.model_copy(update={"sequence": huge_sequence})

    async def full_page(**kwargs: Any):
        return ServiceRequestFeed(
            items=[state.request],
            high_watermark=huge_sequence,
            next_cursor="page2",
            has_more=False,
        )

    connector.list_service_requests = full_page  # type: ignore[method-assign]
    first = _sync(ctx)
    second = _sync(ctx)
    connector.list_service_requests = original  # type: ignore[assignment]
    assert first["created"] == 1
    assert first["high_watermark"] == huge_sequence
    assert second["created"] == 0 and second["updated"] == 0
    detail = _detail(ctx, _cases(ctx)["items"][0]["case_id"])
    assert detail["state"] == "processing"


@pytest.mark.parametrize(
    "dates",
    [
        {"effective_from": datetime(2099, 1, 1, tzinfo=UTC)},
        {"effective_to": datetime(2000, 1, 1, tzinfo=UTC)},
    ],
)
def test_future_or_expired_policy_evidence_cannot_be_submitted(
    ctx: Ctx, dates: dict[str, datetime]
) -> None:
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    chunk_id = _evidence_id_for(ctx, "faults-missing")
    chunks = ctx.app.state.container.retriever._bm25._chunks
    for index, chunk in enumerate(chunks):
        if chunk.chunk_id == chunk_id:
            chunks[index] = chunk.model_copy(
                update={key: value.date() for key, value in dates.items()}
            )
            break
    created = _draft(ctx, case_id, _detail(ctx, case_id)["revision"], [chunk_id])
    assert created.status_code == 200, created.text
    submitted = _submit(
        ctx, case_id, created.json()["current_draft"]["version"], created.json()["revision"]
    )
    assert submitted.status_code == 409
    assert "conflicting_policy" in submitted.json()["message"]
    detail = _detail(ctx, case_id)
    assert detail["state"] == "needs_info"
    assert detail["approvals"] == []
    assert detail["operations"] == []


def test_concurrent_same_revision_only_one_writer_wins(ctx: Ctx) -> None:
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    revision = _detail(ctx, case_id)["revision"]

    async def race() -> tuple[bool, bool]:
        engine = create_async_engine(ctx.database_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

            async def writer(state: str) -> bool:
                async with factory() as session:
                    won = await AfterSalesRepository(session).cas_case(
                        market="au",
                        case_id=case_id,
                        expected_revision=revision,
                        values={"state": state},
                    )
                    await session.commit()
                    return won

            first, second = await asyncio.gather(writer("processing"), writer("needs_info"))
            return first, second
        finally:
            await engine.dispose()

    results = asyncio.run(race())
    assert sorted(results) == [False, True]
    assert _detail(ctx, case_id)["revision"] == revision + 1


def _race_empty_sync(ctx: Ctx, monkeypatch, next_cursor: str) -> list[str]:
    connector = ctx.app.state.container.business_connector

    async def empty_feed(**kwargs: Any) -> ServiceRequestFeed:
        return ServiceRequestFeed(
            items=[],
            high_watermark="43",
            next_cursor=next_cursor,
            has_more=False,
        )

    monkeypatch.setattr(connector, "list_service_requests", empty_feed)
    workflow = ctx.app.state.container.after_sales_workflow
    actor = StaffActor.model_validate(
        {
            "staff_id": "staff_support_au",
            "display_name": "Synthetic support",
            "roles": ["support"],
            "allowed_markets": ["au"],
        }
    )
    original_get_cursor = AfterSalesRepository.get_cursor

    async def race() -> list[str]:
        # Both syncs must read the same starting cursor before either commits.
        barrier = asyncio.Barrier(2)

        async def synchronized_get_cursor(self, market):
            row = await original_get_cursor(self, market)
            await barrier.wait()
            return row

        monkeypatch.setattr(AfterSalesRepository, "get_cursor", synchronized_get_cursor)

        async def one() -> str:
            try:
                await workflow.sync(actor, "au")
                return "advanced"
            except (RevisionConflictError, StateConflictError):
                return "conflict"

        return list(await asyncio.gather(one(), one()))

    # Run on the TestClient's event loop: the workflow shares the application's
    # async engine/pool, whose connections are bound to that loop.
    portal = ctx.client.portal
    assert portal is not None
    return portal.call(race)


def test_concurrent_empty_sync_advances_cursor_exactly_once(ctx: Ctx, monkeypatch) -> None:
    _sync(ctx)
    start = _cursor_row(ctx.database_url)
    assert start is not None
    events_before = asyncio.run(_count(ctx.database_url, AfterSalesEventRow))

    results = _race_empty_sync(ctx, monkeypatch, "empty-page")
    assert sorted(results) == ["advanced", "conflict"]

    cursor = _cursor_row(ctx.database_url)
    assert cursor is not None
    assert cursor.revision == start.revision + 1
    assert cursor.cursor == "empty-page"
    assert cursor.high_watermark == "43"
    assert asyncio.run(_count(ctx.database_url, AfterSalesEventRow)) == events_before


def test_concurrent_first_empty_sync_inserts_cursor_once(ctx: Ctx, monkeypatch) -> None:
    results = _race_empty_sync(ctx, monkeypatch, "first-page")
    assert sorted(results) == ["advanced", "conflict"]

    cursor = _cursor_row(ctx.database_url)
    assert cursor is not None
    assert cursor.revision == 1
    assert cursor.cursor == "first-page"
    assert asyncio.run(_count(ctx.database_url, AfterSalesCaseRow)) == 0
    assert asyncio.run(_count(ctx.database_url, AfterSalesEventRow)) == 0


def test_mutation_requests_require_expected_revision(ctx: Ctx) -> None:
    _sync(ctx)
    case_id = _cases(ctx)["items"][0]["case_id"]
    no_revision = ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/materials",
        headers=_auth(ctx.tokens["staff_support_au"]),
        json={
            "material_type": "manual_verification",
            "source": "Synthetic source",
            "content": "content",
        },
    )
    assert no_revision.status_code == 422
