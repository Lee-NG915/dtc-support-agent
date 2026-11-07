from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import Settings
from app.connectors.errors import ConnectorError, ConnectorUnavailable
from app.connectors.models import CreateServiceTicketInput, TicketLookupFound
from app.core.security import generate_staff_token
from app.core.time import utcnow
from app.main import create_app
from app.repositories.after_sales_repository import AfterSalesRepository
from app.repositories.db import init_schema
from app.repositories.models import (
    AfterSalesApprovalRow,
    AfterSalesCaseRow,
    AfterSalesDraftRow,
    AfterSalesOperationRow,
    AfterSalesStaffRow,
    AfterSalesSyncCursorRow,
)
from app.repositories.staff_repository import StaffRepository
from app.services.after_sales.execution import (
    _OUTCOME_SKIPPED,
    AfterSalesExecution,
)
from app.worker.main import (
    ContainerWorkerRuntime,
    parse_markets,
    run_worker,
    run_worker_loop,
)

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Ctx:
    client: TestClient
    app: Any
    tokens: dict[str, str]
    database_url: str
    settings: Settings


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
        "after_sales_worker_retry_base_seconds": 0.1,
        "after_sales_worker_retry_max_seconds": 1.0,
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
        yield Ctx(
            client=client,
            app=app,
            tokens=tokens,
            database_url=settings.database_url,
            settings=settings,
        )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _sync(ctx: Ctx) -> None:
    response = ctx.client.post(
        "/api/v1/after-sales/au/cases/sync",
        headers=_auth(ctx.tokens["staff_support_au"]),
    )
    assert response.status_code == 200, response.text


def _detail(ctx: Ctx, case_id: int) -> dict[str, Any]:
    response = ctx.client.get(
        f"/api/v1/after-sales/au/cases/{case_id}",
        headers=_auth(ctx.tokens["staff_support_au"]),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _evidence_id(ctx: Ctx) -> str:
    for chunk in ctx.app.state.container.retriever.corpus:
        if (
            chunk.brand == "epig"
            and chunk.market == "au"
            and "homewares" in chunk.product_categories
            and chunk.source_path
            and chunk.source_path.rsplit("/", 1)[-1] == "faults-missing.md"
        ):
            return chunk.chunk_id
    raise AssertionError("missing synthetic faults-missing policy chunk")


def _prepare_approved(ctx: Ctx) -> tuple[int, dict[str, Any], str]:
    _sync(ctx)
    case_id = ctx.client.get(
        "/api/v1/after-sales/au/cases",
        headers=_auth(ctx.tokens["staff_support_au"]),
    ).json()["items"][0]["case_id"]
    created = ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/drafts",
        headers=_auth(ctx.tokens["staff_support_au"]),
        json={
            "expected_revision": _detail(ctx, case_id)["revision"],
            "action": "reship",
            "internal_summary": "Manual review of the synthetic damaged-item request.",
            "consumer_reply": "We will reship the item and share the new tracking number.",
            "evidence_chunk_ids": [_evidence_id(ctx)],
        },
    )
    assert created.status_code == 200, created.text
    draft = created.json()
    submitted = ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/drafts/"
        f"{draft['current_draft']['version']}/submit",
        headers=_auth(ctx.tokens["staff_support_au"]),
        json={"expected_revision": draft["revision"]},
    )
    assert submitted.status_code == 200, submitted.text
    submitted_detail = submitted.json()
    approval_id = submitted_detail["approvals"][-1]["approval_id"]
    approved = ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/approvals/{approval_id}/approve",
        headers=_auth(ctx.tokens["staff_approver_au"]),
        json={"expected_revision": submitted_detail["revision"]},
    )
    assert approved.status_code == 200, approved.text
    return case_id, approved.json(), approval_id


def _run_once(ctx: Ctx, execution: AfterSalesExecution | None = None):
    execution = execution or ctx.app.state.container.after_sales_execution
    portal = ctx.client.portal
    assert portal is not None
    return portal.call(execution.run_once, ["au"])


async def _update_row(database_url: str, model: Any, pk: Any, **values: Any) -> None:
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


async def _fetch_row(database_url: str, model: Any, pk: Any):
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with factory() as session:
            return await session.get(model, pk)
    finally:
        await engine.dispose()


async def _rows(database_url: str, model: Any) -> list[Any]:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with factory() as session:
            return list(await session.scalars(select(model)))
    finally:
        await engine.dispose()


async def _count(database_url: str, model: Any) -> int:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with factory() as session:
            return int(await session.scalar(select(func.count()).select_from(model)) or 0)
    finally:
        await engine.dispose()


def _operation_row(ctx: Ctx, operation_id: str) -> Any:
    return asyncio.run(_fetch_row(ctx.database_url, AfterSalesOperationRow, operation_id))


def _make_due(ctx: Ctx, operation_id: str, **extra: Any) -> None:
    values: dict[str, Any] = {
        "next_attempt_at": utcnow() - timedelta(seconds=1),
        "lease_owner": None,
        "lease_until": None,
    }
    values.update(extra)
    asyncio.run(_update_row(ctx.database_url, AfterSalesOperationRow, operation_id, **values))


def _operation_id(detail: dict[str, Any]) -> str:
    return detail["operations"][0]["operation_id"]


# ---------------------------------------------------------------- happy path


def test_worker_creates_exactly_one_verified_ticket(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    connector = ctx.app.state.container.business_connector
    readback_calls = 0
    original_readback = connector.get_service_ticket

    async def counted_readback(**kwargs: Any):
        nonlocal readback_calls
        readback_calls += 1
        return await original_readback(**kwargs)

    connector.get_service_ticket = counted_readback  # type: ignore[method-assign]
    report = _run_once(ctx)
    connector.get_service_ticket = original_readback  # type: ignore[method-assign]

    assert report.claimed == 1
    assert report.succeeded == 1
    assert connector.create_ticket_calls == 1
    assert readback_calls == 1

    after = _detail(ctx, case_id)
    assert after["state"] == "completed"
    assert after["remote_status"] == "ticket_created"
    assert after["remote_ticket_id"]
    operation = asyncio.run(_fetch_row(ctx.database_url, AfterSalesOperationRow, operation_id))
    approval = asyncio.run(_fetch_row(ctx.database_url, AfterSalesApprovalRow, approval_id))
    draft = asyncio.run(
        _fetch_row(ctx.database_url, AfterSalesDraftRow, detail["current_draft"]["draft_id"])
    )
    assert operation.status == "succeeded"
    assert operation.receipt_json
    assert operation.lease_owner is None
    assert approval.status == "consumed"
    assert draft.status == "consumed"
    assert after["operations"][0]["receipt"]["ticket_id"] == after["remote_ticket_id"]

    # A second worker cycle must not create a second ticket.
    second = _run_once(ctx)
    assert second.claimed == 0
    assert connector.create_ticket_calls == 1


def test_unknown_outcome_is_looked_up_after_approval_expiry(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    connector = ctx.app.state.container.business_connector
    original_create = connector.create_ticket

    async def success_then_lost(**kwargs: Any):
        await original_create(**kwargs)
        raise ConnectorUnavailable(create_operation=True)

    connector.create_ticket = success_then_lost  # type: ignore[method-assign]
    first = _run_once(ctx)
    assert first.reconciling == 1
    assert connector.create_ticket_calls == 1
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "reconciliation_required"
    assert _detail(ctx, case_id)["state"] == "reconciliation_required"

    connector.create_ticket = original_create  # type: ignore[method-assign]
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesApprovalRow,
            approval_id,
            expires_at=utcnow() - timedelta(seconds=1),
        )
    )
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesStaffRow,
            "staff_support_au",
            roles="auditor",
        )
    )
    _make_due(ctx, operation_id)
    second = _run_once(ctx)

    assert second.succeeded == 1
    assert connector.create_ticket_calls == 1
    after = _detail(ctx, case_id)
    assert after["state"] == "completed"
    assert after["remote_ticket_id"]
    approval = asyncio.run(_fetch_row(ctx.database_url, AfterSalesApprovalRow, approval_id))
    assert approval.status == "consumed"
    operation = _operation_row(ctx, operation_id)
    assert operation.lookup_count == 1
    assert operation.reconciliation_state == "matched"


def test_prepared_operation_with_remote_ticket_looks_up_without_posting(ctx: Ctx) -> None:
    case_id, detail, _ = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    connector = ctx.app.state.container.business_connector
    state = connector._state.requests[("au", "asr_000000000042")]
    state.request = state.request.model_copy(
        update={
            "status": "ticket_created",
            "ticket_id": "tic_000000000043",
            "sequence": "00000000000000000043",
            "updated_at": state.request.updated_at + timedelta(minutes=10),
        }
    )
    _sync(ctx)
    assert _detail(ctx, case_id)["state"] == "reconciliation_required"
    assert _operation_row(ctx, operation_id).status == "prepared"

    report = _run_once(ctx)
    assert report.manual_review == 1
    assert connector.create_ticket_calls == 0
    after = _detail(ctx, case_id)
    assert after["state"] == "reconciliation_required"
    assert after["operations"][0]["status"] == "reconciliation_required"
    assert after["operations"][0]["reconciliation_state"] == "manual_review"


# ----------------------------------------------------- revalidation failures


def test_expired_prepared_approval_fails_without_posting(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesApprovalRow,
            approval_id,
            expires_at=utcnow() - timedelta(seconds=1),
        )
    )
    report = _run_once(ctx)
    assert report.failed == 1
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0
    after = _detail(ctx, case_id)
    assert after["state"] == "processing"
    assert after["approvals"][-1]["status"] == "expired"
    assert after["current_draft"]["status"] == "invalidated"
    assert _operation_row(ctx, operation_id).status == "failed"


@pytest.mark.parametrize(
    ("staff_id", "values"),
    [
        ("staff_support_au", {"roles": "auditor"}),
        ("staff_approver_au", {"enabled": False}),
    ],
)
def test_revoked_requester_or_decider_fails_without_posting(
    ctx: Ctx, staff_id: str, values: dict[str, Any]
) -> None:
    _case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    asyncio.run(_update_row(ctx.database_url, AfterSalesStaffRow, staff_id, **values))
    report = _run_once(ctx)
    assert report.failed == 1
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "failed"
    assert "authorization_changed" in (operation.failure_reason or "")


def test_snapshot_digest_corruption_is_manual_review_and_never_posts(ctx: Ctx) -> None:
    _case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesOperationRow,
            operation_id,
            payload_json=json.dumps({"tampered": True}),
        )
    )
    report = _run_once(ctx)
    assert report.manual_review == 1
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "reconciliation_required"
    assert operation.reconciliation_state == "manual_review"
    assert operation.last_error_code in {
        "payload_digest_mismatch",
        "payload_invalid",
        "payload_mismatch",
    }


def test_self_approval_context_fails_without_posting(ctx: Ctx) -> None:
    _case_id, detail, approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesApprovalRow,
            approval_id,
            decided_by_staff_id="staff_support_au",
        )
    )
    report = _run_once(ctx)
    assert report.failed == 1
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "failed"
    assert "decider_authorization_changed" in (operation.failure_reason or "")


def test_policy_change_fails_prepared_before_dispatch(ctx: Ctx) -> None:
    _case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    evidence_id = detail["current_draft"]["evidence"][0]["chunk_id"]
    chunks = ctx.app.state.container.retriever._bm25._chunks
    for index, chunk in enumerate(chunks):
        if chunk.chunk_id == evidence_id:
            chunks[index] = chunk.model_copy(update={"content": chunk.content + " updated"})
            break
    report = _run_once(ctx)
    assert report.failed == 1
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "failed"
    assert "policy_changed" in (operation.failure_reason or "")


def test_material_change_after_approval_fails_prepared_before_worker(ctx: Ctx) -> None:
    case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
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
    report = _run_once(ctx)
    assert report.claimed == 0
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "failed"
    assert operation.failure_reason == "materials_changed"


def test_sync_only_case_has_no_operation_to_dispatch(ctx: Ctx) -> None:
    _sync(ctx)
    report = _run_once(ctx)
    assert report.claimed == 0
    assert asyncio.run(_count(ctx.database_url, AfterSalesOperationRow)) == 0
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0


# --------------------------------------------------------------- reconciliation


def test_in_flight_then_temporary_404_exhausts_lookups_to_manual_review(ctx: Ctx) -> None:
    _case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    operation = _operation_row(ctx, operation_id)
    ctx.settings.after_sales_worker_lookup_budget = 2
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesOperationRow,
            operation_id,
            status="dispatching",
            dispatched_at=utcnow(),
        )
    )
    connector = ctx.app.state.container.business_connector
    connector.set_in_flight("au", operation.idempotency_key)
    first = _run_once(ctx)
    assert first.reconciling == 1
    operation = _operation_row(ctx, operation_id)
    assert operation.lookup_count == 1
    assert operation.reconciliation_state == "required"
    assert operation.status == "reconciliation_required"

    connector.clear_in_flight("au", operation.idempotency_key)
    _make_due(ctx, operation_id)
    second = _run_once(ctx)
    assert second.manual_review == 1
    operation = _operation_row(ctx, operation_id)
    assert operation.lookup_count == 2
    assert operation.reconciliation_state == "manual_review"
    assert operation.next_attempt_at is None
    assert operation.last_error_code == "NOT_VISIBLE"
    assert connector.create_ticket_calls == 0

    # Manual review stops automatic claiming.
    third = _run_once(ctx)
    assert third.claimed == 0


def test_temporary_404_schedules_same_key_retry_with_identical_payload(ctx: Ctx) -> None:
    case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesOperationRow,
            operation_id,
            status="dispatching",
            dispatched_at=utcnow(),
        )
    )
    connector = ctx.app.state.container.business_connector
    first = _run_once(ctx)
    assert first.retry_wait == 1
    operation = _operation_row(ctx, operation_id)
    original_key = operation.idempotency_key
    original_payload = operation.payload_json
    assert operation.lookup_count == 1
    assert operation.status == "retry_wait"
    assert operation.reconciliation_state == "same_key_retry_eligible"
    assert operation.attempt == 0
    assert connector.create_ticket_calls == 0

    _make_due(ctx, operation_id)
    second = _run_once(ctx)
    assert second.succeeded == 1
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "succeeded"
    assert operation.attempt == 1
    assert operation.idempotency_key == original_key
    assert operation.payload_json == original_payload
    assert connector.create_ticket_calls == 1
    after = _detail(ctx, case_id)
    assert after["state"] == "completed"
    assert after["remote_ticket_id"]


def test_late_original_ticket_is_consumed_without_a_second_operation(ctx: Ctx) -> None:
    case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesOperationRow,
            operation_id,
            status="dispatching",
            dispatched_at=utcnow(),
        )
    )
    connector = ctx.app.state.container.business_connector
    first = _run_once(ctx)
    assert first.retry_wait == 1
    operation = _operation_row(ctx, operation_id)
    original_key = operation.idempotency_key
    original_payload = operation.payload_json
    assert operation.status == "retry_wait"

    portal = ctx.client.portal
    assert portal is not None

    async def externally_create_original():
        return await connector.create_ticket(
            market="au",
            idempotency_key=original_key,
            payload=CreateServiceTicketInput.model_validate(json.loads(original_payload)),
        )

    creation = portal.call(externally_create_original)
    assert creation.ticket.approval_id == operation.approval_id

    # The retry cycle sees the changed remote request and must not POST; the
    # next cycle finds and consumes the late original ticket.
    _make_due(ctx, operation_id)
    second = _run_once(ctx)
    assert second.reconciling == 1
    assert connector.create_ticket_calls == 1
    _make_due(ctx, operation_id)
    third = _run_once(ctx)
    assert third.succeeded == 1
    assert connector.create_ticket_calls == 1
    after = _detail(ctx, case_id)
    assert after["state"] == "completed"
    assert after["remote_ticket_id"] == creation.ticket.ticket_id
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "succeeded"
    assert operation.idempotency_key == original_key
    assert operation.payload_json == original_payload
    assert asyncio.run(_count(ctx.database_url, AfterSalesOperationRow)) == 1


def test_later_401_does_not_erase_unknown_attempt_or_release_guard(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    connector = ctx.app.state.container.business_connector

    # 1. The first POST times out after it may have reached E-pig.
    connector.queue_failure("create_ticket", ConnectorUnavailable(create_operation=True))
    first = _run_once(ctx)
    assert first.reconciling == 1
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "reconciliation_required"
    assert operation.unknown_attempts == 1
    assert operation.attempt == 1
    original_key = operation.idempotency_key
    original_payload = operation.payload_json

    # 2. The receipt is temporarily not visible; the contract permits an
    #    original-key retry after revalidation.
    _make_due(ctx, operation_id)
    second = _run_once(ctx)
    assert second.retry_wait == 1
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "retry_wait"
    assert operation.reconciliation_state == "same_key_retry_eligible"
    assert connector.create_ticket_calls == 1

    # 3. The second POST is explicitly rejected with 401. That only proves
    #    this attempt wrote nothing; the earlier unknown attempt stays open.
    connector.queue_failure(
        "create_ticket",
        ConnectorError(
            code="UNAUTHORIZED",
            message="synthetic expired service credential",
            retryable=False,
            recovery_action="NONE",
            outcome_unknown=False,
        ),
    )
    _make_due(ctx, operation_id)
    third = _run_once(ctx)
    assert third.reconciling == 1
    assert third.failed == 0
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "reconciliation_required"
    assert operation.reconciliation_state == "required"
    assert operation.attempt == 2
    assert operation.unknown_attempts == 1
    assert operation.idempotency_key == original_key
    assert operation.payload_json == original_payload
    approval = asyncio.run(_fetch_row(ctx.database_url, AfterSalesApprovalRow, approval_id))
    assert approval.status not in {"invalidated", "expired", "consumed"}
    assert _detail(ctx, case_id)["state"] == "reconciliation_required"
    assert connector.create_ticket_calls == 2

    # 4. The late original ticket is still matched and consumed exactly once.
    portal = ctx.client.portal
    assert portal is not None

    async def externally_create_original():
        return await connector.create_ticket(
            market="au",
            idempotency_key=original_key,
            payload=CreateServiceTicketInput.model_validate(json.loads(original_payload)),
        )

    creation = portal.call(externally_create_original)
    _make_due(ctx, operation_id)
    fourth = _run_once(ctx)
    assert fourth.succeeded == 1
    after = _detail(ctx, case_id)
    assert after["state"] == "completed"
    assert after["remote_ticket_id"] == creation.ticket.ticket_id
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "succeeded"
    assert operation.idempotency_key == original_key
    assert operation.payload_json == original_payload
    assert asyncio.run(_count(ctx.database_url, AfterSalesOperationRow)) == 1


def test_context_invalid_after_unknown_fails_closed_without_failing_operation(
    ctx: Ctx,
) -> None:
    _case_id, detail, approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    connector = ctx.app.state.container.business_connector

    connector.queue_failure("create_ticket", ConnectorUnavailable(create_operation=True))
    assert _run_once(ctx).reconciling == 1
    _make_due(ctx, operation_id)
    assert _run_once(ctx).retry_wait == 1
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "retry_wait"
    calls_before = connector.create_ticket_calls

    # Approval expires and the requester loses the role before the retry.
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesApprovalRow,
            approval_id,
            expires_at=utcnow() - timedelta(seconds=1),
        )
    )
    asyncio.run(
        _update_row(ctx.database_url, AfterSalesStaffRow, "staff_support_au", roles="auditor")
    )
    _make_due(ctx, operation_id)
    report = _run_once(ctx)
    assert report.failed == 0
    assert connector.create_ticket_calls == calls_before
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "reconciliation_required"
    assert operation.reconciliation_state == "required"
    approval = asyncio.run(_fetch_row(ctx.database_url, AfterSalesApprovalRow, approval_id))
    assert approval.status not in {"invalidated", "expired", "consumed"}

    # The next lookup still reads by the original key; invalid context turns
    # this into manual review, never a failed operation with a released guard.
    _make_due(ctx, operation_id)
    assert _run_once(ctx).manual_review == 1
    assert connector.create_ticket_calls == calls_before
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "reconciliation_required"
    assert operation.reconciliation_state == "manual_review"


def test_same_key_retry_keeps_key_and_stops_at_write_budget(ctx: Ctx) -> None:
    _case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    operation = _operation_row(ctx, operation_id)
    ctx.settings.after_sales_worker_write_budget = 2
    connector = ctx.app.state.container.business_connector
    for _ in range(2):
        connector.queue_failure(
            "create_ticket",
            ConnectorError(
                code="RATE_LIMITED",
                message="synthetic safe same-key rejection",
                retryable=True,
                recovery_action="RETRY_CREATE_WITH_SAME_KEY",
                outcome_unknown=False,
            ),
        )

    first = _run_once(ctx)
    assert first.retry_wait == 1
    operation = _operation_row(ctx, operation_id)
    original_key = operation.idempotency_key
    original_payload = operation.payload_json
    assert operation.status == "retry_wait"
    assert operation.attempt == 1

    _make_due(ctx, operation_id)
    second = _run_once(ctx)
    assert second.manual_review == 1
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "reconciliation_required"
    assert operation.reconciliation_state == "manual_review"
    assert operation.attempt == 2
    assert operation.idempotency_key == original_key
    assert operation.payload_json == original_payload
    assert connector.create_ticket_calls == 2
    assert asyncio.run(_count(ctx.database_url, AfterSalesOperationRow)) == 1


def test_mismatched_readback_is_manual_review(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_approved(ctx)
    connector = ctx.app.state.container.business_connector
    original_readback = connector.get_service_ticket

    async def tampered_readback(**kwargs: Any):
        ticket = await original_readback(**kwargs)
        return ticket.model_copy(update={"reply": "A different consumer reply."})

    connector.get_service_ticket = tampered_readback  # type: ignore[method-assign]
    report = _run_once(ctx)
    connector.get_service_ticket = original_readback  # type: ignore[method-assign]

    assert report.manual_review == 1
    after = _detail(ctx, case_id)
    assert after["state"] == "reconciliation_required"
    assert after["approvals"][-1]["status"] != "consumed"
    operation = _operation_row(ctx, _operation_id(detail))
    assert operation.status == "reconciliation_required"
    assert operation.reconciliation_state == "manual_review"
    assert operation.last_error_code == "RECEIPT_MISMATCH"
    assert connector.create_ticket_calls == 1


def test_external_success_with_local_commit_failure_recovers(ctx: Ctx, monkeypatch) -> None:
    case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    connector = ctx.app.state.container.business_connector
    original_cas = AfterSalesRepository.cas_case
    state = {"failed_once": False}

    async def flaky_cas(self, **kwargs):
        if not state["failed_once"]:
            state["failed_once"] = True
            raise SQLAlchemyError("synthetic local commit failure")
        return await original_cas(self, **kwargs)

    monkeypatch.setattr(AfterSalesRepository, "cas_case", flaky_cas)
    first = _run_once(ctx)
    assert first.reconciling == 1
    assert connector.create_ticket_calls == 1
    assert _detail(ctx, case_id)["state"] == "reconciliation_required"
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "reconciliation_required"

    _make_due(ctx, operation_id)
    second = _run_once(ctx)
    assert second.succeeded == 1
    assert connector.create_ticket_calls == 1
    after = _detail(ctx, case_id)
    assert after["state"] == "completed"
    assert after["remote_ticket_id"]
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "succeeded"
    assert operation.lookup_count == 1


# ------------------------------------------------------- completion fencing


def test_new_candidate_does_not_change_original_result(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    original_draft_id = detail["current_draft"]["draft_id"]
    connector = ctx.app.state.container.business_connector
    original_create = connector.create_ticket

    async def success_then_lost(**kwargs: Any):
        await original_create(**kwargs)
        raise ConnectorUnavailable(create_operation=True)

    connector.create_ticket = success_then_lost  # type: ignore[method-assign]
    assert _run_once(ctx).reconciling == 1
    connector.create_ticket = original_create  # type: ignore[method-assign]

    current = _detail(ctx, case_id)
    candidate = ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/drafts",
        headers=_auth(ctx.tokens["staff_support_au"]),
        json={
            "expected_revision": current["revision"],
            "action": "refund",
            "internal_summary": "A later candidate saved while the receipt is unknown.",
            "consumer_reply": "A later candidate reply.",
            "evidence_chunk_ids": [_evidence_id(ctx)],
        },
    )
    assert candidate.status_code == 200, candidate.text
    candidate_detail = candidate.json()
    assert candidate_detail["current_draft"]["draft_id"] == original_draft_id
    candidate_version = max(item["version"] for item in candidate_detail["drafts"])
    assert candidate_detail["current_draft"]["version"] < candidate_version

    _make_due(ctx, operation_id)
    report = _run_once(ctx)
    assert report.succeeded == 1
    after = _detail(ctx, case_id)
    assert after["state"] == "completed"
    assert after["current_draft"]["draft_id"] == original_draft_id
    original_draft = next(item for item in after["drafts"] if item["draft_id"] == original_draft_id)
    candidate_draft = next(item for item in after["drafts"] if item["version"] == candidate_version)
    assert original_draft["status"] == "consumed"
    assert candidate_draft["status"] == "editing"
    approval = asyncio.run(_fetch_row(ctx.database_url, AfterSalesApprovalRow, approval_id))
    assert approval.status == "consumed"
    assert connector.create_ticket_calls == 1


def test_stale_worker_cannot_complete_under_a_new_epoch(ctx: Ctx) -> None:
    case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    execution: AfterSalesExecution = ctx.app.state.container.after_sales_execution
    portal = ctx.client.portal
    assert portal is not None

    claims = portal.call(lambda: execution._claim_due(["au"], utcnow()))
    assert len(claims) == 1
    claim = claims[0]
    context = portal.call(execution._load_context, claim)
    connector = ctx.app.state.container.business_connector

    async def create_directly():
        return await connector.create_ticket(
            market="au",
            idempotency_key=context.operation.idempotency_key,
            payload=CreateServiceTicketInput.model_validate(context.payload),
        )

    creation = portal.call(create_directly)
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesOperationRow,
            operation_id,
            lease_owner="new_owner",
            lease_epoch=claim.epoch + 1,
            status="dispatching",
            dispatched_at=utcnow(),
        )
    )

    outcome = portal.call(execution._complete, claim, context, creation.ticket)
    assert outcome == _OUTCOME_SKIPPED
    operation = _operation_row(ctx, operation_id)
    assert operation.lease_owner == "new_owner"
    assert operation.lease_epoch == claim.epoch + 1
    assert operation.status == "dispatching"
    after = _detail(ctx, case_id)
    assert after["state"] != "completed"
    assert after["approvals"][-1]["status"] != "consumed"

    # A late worker cannot even reload the context under its old epoch.
    with pytest.raises(Exception):
        portal.call(execution._load_context, claim)


def _inject_after_revalidation(
    ctx: Ctx,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Any,
) -> None:
    workflow = ctx.app.state.container.after_sales_workflow
    original = workflow.revalidate_execution_context

    async def wrapper(**kwargs: Any):
        problems = await original(**kwargs)
        await mutation()
        return problems

    monkeypatch.setattr(workflow, "revalidate_execution_context", wrapper)


def test_expiry_between_validation_and_dispatch_blocks_post(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_id, detail, approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    connector = ctx.app.state.container.business_connector

    async def expire() -> None:
        async with ctx.app.state.container.session_factory() as session, session.begin():
            row = await session.get(AfterSalesApprovalRow, approval_id)
            assert row is not None
            row.expires_at = utcnow() - timedelta(seconds=1)

    _inject_after_revalidation(ctx, monkeypatch, expire)
    report = _run_once(ctx)
    assert report.failed == 0
    assert connector.create_ticket_calls == 0
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "prepared"
    assert operation.last_error_code == "approval_expired"
    assert operation.next_attempt_at is not None
    approval = asyncio.run(_fetch_row(ctx.database_url, AfterSalesApprovalRow, approval_id))
    assert approval.status == "approved"
    assert _detail(ctx, case_id)["state"] == "waiting_approval"


def test_revocation_between_validation_and_dispatch_blocks_post(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    _case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    connector = ctx.app.state.container.business_connector

    async def revoke() -> None:
        async with ctx.app.state.container.session_factory() as session, session.begin():
            row = await session.get(AfterSalesStaffRow, "staff_support_au")
            assert row is not None
            row.roles = "auditor"

    _inject_after_revalidation(ctx, monkeypatch, revoke)
    report = _run_once(ctx)
    assert report.failed == 0
    assert connector.create_ticket_calls == 0
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "prepared"
    assert operation.last_error_code == "requester_authorization_changed"


def test_case_revision_change_during_validation_blocks_post(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    _case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    connector = ctx.app.state.container.business_connector

    async def bump_revision() -> None:
        async with ctx.app.state.container.session_factory() as session, session.begin():
            row = await session.get(AfterSalesCaseRow, detail["case_id"])
            assert row is not None
            row.revision += 1

    _inject_after_revalidation(ctx, monkeypatch, bump_revision)
    report = _run_once(ctx)
    assert report.failed == 0
    assert connector.create_ticket_calls == 0
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "prepared"
    assert operation.last_error_code == "case_revision_changed"


def test_draft_guard_rowcount_failure_rolls_back_dispatch(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    _case_id, detail, approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    draft_id = detail["current_draft"]["draft_id"]
    connector = ctx.app.state.container.business_connector

    async def invalidate_draft() -> None:
        async with ctx.app.state.container.session_factory() as session, session.begin():
            row = await session.get(AfterSalesDraftRow, draft_id)
            assert row is not None
            row.status = "invalidated"

    _inject_after_revalidation(ctx, monkeypatch, invalidate_draft)
    report = _run_once(ctx)
    assert report.failed == 0
    assert connector.create_ticket_calls == 0
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "prepared"
    assert operation.last_error_code == "draft_changed"
    approval = asyncio.run(_fetch_row(ctx.database_url, AfterSalesApprovalRow, approval_id))
    assert approval.status == "approved"
    case = asyncio.run(_fetch_row(ctx.database_url, AfterSalesCaseRow, detail["case_id"]))
    assert case.state == "waiting_approval"


def test_completion_consume_conflict_never_fabricates_success(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    draft_id = detail["current_draft"]["draft_id"]
    connector = ctx.app.state.container.business_connector
    original_create = connector.create_ticket

    async def create_then_invalidate_draft(**kwargs: Any):
        creation = await original_create(**kwargs)
        async with ctx.app.state.container.session_factory() as session, session.begin():
            row = await session.get(AfterSalesDraftRow, draft_id)
            assert row is not None
            row.status = "superseded"
        return creation

    connector.create_ticket = create_then_invalidate_draft  # type: ignore[method-assign]
    report = _run_once(ctx)
    connector.create_ticket = original_create  # type: ignore[method-assign]

    assert report.manual_review == 1
    assert report.succeeded == 0
    assert connector.create_ticket_calls == 1
    after = _detail(ctx, case_id)
    assert after["state"] == "reconciliation_required"
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "reconciliation_required"
    assert operation.reconciliation_state == "manual_review"
    assert operation.receipt_json is None
    approval = asyncio.run(_fetch_row(ctx.database_url, AfterSalesApprovalRow, approval_id))
    assert approval.status in {"approved", "executing"}


def test_lookup_receipt_without_local_approval_chain_is_manual(ctx: Ctx) -> None:
    _case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    connector = ctx.app.state.container.business_connector
    original_lookup = connector.get_ticket_by_idempotency_key

    async def forged_lookup(**kwargs: Any):
        result = await original_lookup(**kwargs)
        assert isinstance(result, TicketLookupFound)
        return TicketLookupFound(
            ticket=result.ticket.model_copy(update={"approval_id": "apr_00000000000000000000ff"})
        )

    # First create a real ticket, then hide it behind a forged approval id.
    assert _run_once(ctx).succeeded == 1
    connector.get_ticket_by_idempotency_key = forged_lookup  # type: ignore[method-assign]
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesOperationRow,
            operation_id,
            status="reconciliation_required",
            reconciliation_state="required",
            lease_owner=None,
            lease_until=None,
            next_attempt_at=utcnow() - timedelta(seconds=1),
        )
    )
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesCaseRow,
            detail["case_id"],
            state="reconciliation_required",
        )
    )
    connector.get_ticket_by_idempotency_key = forged_lookup  # type: ignore[method-assign]
    report = _run_once(ctx)
    connector.get_ticket_by_idempotency_key = original_lookup  # type: ignore[method-assign]
    assert report.manual_review == 1
    operation = _operation_row(ctx, operation_id)
    assert operation.status == "reconciliation_required"
    assert operation.reconciliation_state == "manual_review"
    assert operation.last_error_code == "RECEIPT_MISMATCH"


# ------------------------------------------------------------------- operator


def test_operator_view_is_sanitised_and_cannot_forge_success(ctx: Ctx) -> None:
    _case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    connector = ctx.app.state.container.business_connector
    state = connector._state.requests[("au", "asr_000000000042")]
    marker = "PII-MARKER-7f2a"
    state.request = state.request.model_copy(
        update={"description": f"{marker} synthetic consumer description"}
    )
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesOperationRow,
            operation_id,
            status="dispatching",
            dispatched_at=utcnow(),
            lease_owner=None,
            lease_until=None,
        )
    )

    for token in ("staff_support_au", "staff_approver_au", "staff_auditor"):
        response = ctx.client.get(
            "/api/v1/after-sales/au/operations",
            headers=_auth(ctx.tokens[token]),
        )
        assert response.status_code == 403, (token, response.text)

    listing = ctx.client.get(
        "/api/v1/after-sales/au/operations",
        headers=_auth(ctx.tokens["staff_operator"]),
    )
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 1
    assert marker not in listing.text
    assert "payload" not in body["items"][0]
    assert "receipt" not in body["items"][0]
    assert "consumer_reply" not in body["items"][0]
    assert "idempotency_key" not in body["items"][0]

    detail_response = ctx.client.get(
        f"/api/v1/after-sales/au/operations/{operation_id}",
        headers=_auth(ctx.tokens["staff_operator"]),
    )
    assert detail_response.status_code == 200, detail_response.text
    assert marker not in detail_response.text
    assert "payload" not in detail_response.text
    assert "receipt" not in detail_response.text
    assert "consumer_reply" not in detail_response.text

    cross_market = ctx.client.get(
        "/api/v1/after-sales/sg/operations",
        headers=_auth(ctx.tokens["staff_operator"]),
    )
    assert cross_market.status_code == 403

    forged = ctx.client.post(
        f"/api/v1/after-sales/au/operations/{operation_id}/lookup",
        headers=_auth(ctx.tokens["staff_operator"]),
        json={
            "success": True,
            "receipt": {"ticket_id": "tic_forged"},
            "payload": {"request_id": "asr_000000000042"},
        },
    )
    assert forged.status_code == 200, forged.text
    result = forged.json()
    assert result["status"] == "reconciliation_required"
    assert result["reconciliation_state"] == "manual_review"
    assert "success" not in result
    assert connector.create_ticket_calls == 0

    # A completed operation cannot be "continued" into a new write.
    _sync(ctx)
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesOperationRow,
            operation_id,
            status="succeeded",
            lease_owner=None,
            lease_until=None,
        )
    )
    closed = ctx.client.post(
        f"/api/v1/after-sales/au/operations/{operation_id}/lookup",
        headers=_auth(ctx.tokens["staff_operator"]),
        json={"success": True},
    )
    assert closed.status_code == 409
    assert connector.create_ticket_calls == 0


def test_operator_lookup_reads_once_and_cannot_schedule_a_post(ctx: Ctx) -> None:
    _case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesOperationRow,
            operation_id,
            status="dispatching",
            dispatched_at=utcnow(),
            lease_owner=None,
            lease_until=None,
        )
    )
    connector = ctx.app.state.container.business_connector
    reads = 0
    original_lookup = connector.get_ticket_by_idempotency_key

    async def counted_lookup(**kwargs: Any):
        nonlocal reads
        reads += 1
        return await original_lookup(**kwargs)

    connector.get_ticket_by_idempotency_key = counted_lookup  # type: ignore[method-assign]
    for expected_lookups in (1, 2):
        response = ctx.client.post(
            f"/api/v1/after-sales/au/operations/{operation_id}/lookup",
            headers=_auth(ctx.tokens["staff_operator"]),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "reconciliation_required"
        assert body["reconciliation_state"] == "manual_review"
        assert reads == expected_lookups
        operation = _operation_row(ctx, operation_id)
        assert operation.attempt == 0
        assert operation.lookup_count == expected_lookups
        assert operation.status == "reconciliation_required"
        assert operation.next_attempt_at is None
        assert connector.create_ticket_calls == 0
    connector.get_ticket_by_idempotency_key = original_lookup  # type: ignore[method-assign]


def test_operator_in_flight_lookup_stays_manual_and_never_posts(ctx: Ctx) -> None:
    _case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    operation = _operation_row(ctx, operation_id)
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesOperationRow,
            operation_id,
            status="dispatching",
            dispatched_at=utcnow(),
            lease_owner=None,
            lease_until=None,
        )
    )
    connector = ctx.app.state.container.business_connector
    connector.set_in_flight("au", operation.idempotency_key)

    response = ctx.client.post(
        f"/api/v1/after-sales/au/operations/{operation_id}/lookup",
        headers=_auth(ctx.tokens["staff_operator"]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "reconciliation_required"
    assert body["reconciliation_state"] == "manual_review"
    assert body["attempt"] == 0
    assert body["lookup_count"] == 1
    assert connector.create_ticket_calls == 0

    row = _operation_row(ctx, operation_id)
    assert row.next_attempt_at is None
    assert row.last_error_code == "IN_FLIGHT"

    # Once in-flight clears, the same key is temporarily NotVisible. Even
    # with the row made due, manual review must keep the background worker
    # from claiming it and scheduling a same-key POST.
    connector.clear_in_flight("au", operation.idempotency_key)
    _make_due(ctx, operation_id)
    report = _run_once(ctx)
    assert report.claimed == 0
    assert connector.create_ticket_calls == 0
    row = _operation_row(ctx, operation_id)
    assert row.status == "reconciliation_required"
    assert row.reconciliation_state == "manual_review"
    assert row.lookup_count == 1
    assert row.attempt == 0


# --------------------------------------------------------------- worker loop


@pytest.mark.asyncio
async def test_worker_loop_once_and_cancellation_close_runtime() -> None:
    class StubRuntime:
        def __init__(self) -> None:
            self.cycles = 0
            self.closed = False
            self.started = asyncio.Event()
            self.block = False

        async def run_cycle(self, markets: list[str]):
            self.cycles += 1
            self.started.set()
            if self.block:
                await asyncio.sleep(3600)

        async def aclose(self) -> None:
            self.closed = True

    once = StubRuntime()
    cycles = await run_worker_loop(
        once,
        ["au"],
        once=True,
        max_cycles=0,
        poll_interval=0.01,
    )
    assert cycles == 1
    assert once.cycles == 1
    assert once.closed is True

    bounded = StubRuntime()
    cycles = await run_worker_loop(
        bounded,
        ["au"],
        once=False,
        max_cycles=3,
        poll_interval=0.01,
    )
    assert cycles == 3
    assert bounded.closed is True

    cancelled = StubRuntime()
    cancelled.block = True
    task = asyncio.create_task(
        run_worker_loop(
            cancelled,
            ["au"],
            once=False,
            max_cycles=0,
            poll_interval=0.01,
        )
    )
    await cancelled.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.closed is True


def test_cli_once_completes_one_operation_and_closes(ctx: Ctx) -> None:
    case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    cycles = asyncio.run(
        run_worker(
            ctx.settings,
            markets=["au"],
            once=True,
            max_cycles=0,
            poll_interval=0.01,
        )
    )
    assert cycles == 1
    after = _detail(ctx, case_id)
    assert after["state"] == "completed"
    assert _operation_row(ctx, operation_id).status == "succeeded"


# --------------------------------------------------------- service feed poll


def _runtime(ctx: Ctx) -> ContainerWorkerRuntime:
    container = ctx.app.state.container
    return ContainerWorkerRuntime(container, container.after_sales_execution)


def _case_list(ctx: Ctx, market: str = "au") -> dict[str, Any]:
    response = ctx.client.get(
        f"/api/v1/after-sales/{market}/cases",
        headers=_auth(ctx.tokens[f"staff_support_{market}"]),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_worker_cycle_receives_feed_without_staff_sync(ctx: Ctx) -> None:
    report = ctx.client.portal.call(_runtime(ctx).run_cycle, ["au"])

    assert report.polled == 1
    assert report.poll_failed == 0
    assert report.poll_error_codes == ()
    assert report.claimed == 0
    listing = _case_list(ctx)
    assert listing["total"] == 1
    case_id = listing["items"][0]["case_id"]
    events = ctx.client.get(
        f"/api/v1/after-sales/au/cases/{case_id}/events",
        headers=_auth(ctx.tokens["staff_support_au"]),
    )
    assert events.status_code == 200, events.text
    received = next(
        item for item in events.json()["items"] if item["event_type"] == "case_received"
    )
    assert received["actor_staff_id"] is None
    assert received["reason"].startswith("service_poll")


def test_failed_market_poll_keeps_cursor_and_does_not_block_execution(ctx: Ctx) -> None:
    case_id, detail, _approval_id = _prepare_approved(ctx)
    cursor_before = asyncio.run(_fetch_row(ctx.database_url, AfterSalesSyncCursorRow, "au"))
    connector = ctx.app.state.container.business_connector
    connector.queue_failure("list_service_requests", ConnectorUnavailable())

    report = ctx.client.portal.call(_runtime(ctx).run_cycle, ["au", "sg"])

    assert report.polled == 1
    assert report.poll_failed == 1
    assert report.poll_error_codes == ("au:UPSTREAM_UNAVAILABLE",)
    assert report.succeeded == 1
    assert connector.create_ticket_calls == 1

    cursor_after = asyncio.run(_fetch_row(ctx.database_url, AfterSalesSyncCursorRow, "au"))
    assert cursor_after is not None and cursor_before is not None
    assert cursor_after.revision == cursor_before.revision
    assert cursor_after.cursor == cursor_before.cursor
    assert _detail(ctx, case_id)["state"] == "completed"

    # The other market still polled and persisted its own cursor.
    assert asyncio.run(_fetch_row(ctx.database_url, AfterSalesSyncCursorRow, "sg")) is not None


def test_repeated_service_poll_does_not_duplicate_cases(ctx: Ctx) -> None:
    runtime = _runtime(ctx)
    first = ctx.client.portal.call(runtime.run_cycle, ["au"])
    second = ctx.client.portal.call(runtime.run_cycle, ["au"])

    assert first.polled == 1 and second.polled == 1
    assert first.poll_failed == 0 and second.poll_failed == 0
    assert _case_list(ctx)["total"] == 1
    assert asyncio.run(_count(ctx.database_url, AfterSalesCaseRow)) == 1
    cursor = asyncio.run(_fetch_row(ctx.database_url, AfterSalesSyncCursorRow, "au"))
    assert cursor is not None
    assert cursor.revision == 2


def test_service_market_scope_is_rejected_outside_connector_config(
    ctx: Ctx, tmp_path: Path
) -> None:
    workflow = ctx.app.state.container.after_sales_workflow
    with pytest.raises(ValueError, match="unsupported after-sales market"):
        ctx.client.portal.call(workflow.sync_service, "zz")
    with pytest.raises(ValueError, match="unsupported worker market"):
        ctx.client.portal.call(_runtime(ctx).run_cycle, ["zz"])
    assert _case_list(ctx)["total"] == 0

    epig = _settings(
        tmp_path,
        business_connector="epig",
        epig_allowed_markets="sg",
    )
    assert parse_markets(None, settings=epig) == ["sg"]
    with pytest.raises(ValueError, match="outside connector scope"):
        parse_markets("au", settings=epig)


def test_cli_once_polls_external_demand_without_staff_click(ctx: Ctx) -> None:
    cycles = asyncio.run(
        run_worker(
            ctx.settings,
            markets=["au"],
            once=True,
            max_cycles=0,
            poll_interval=0.01,
        )
    )

    assert cycles == 1
    listing = _case_list(ctx)
    assert listing["total"] == 1
    assert listing["items"][0]["request_id"] == "asr_000000000042"
    cursor = asyncio.run(_fetch_row(ctx.database_url, AfterSalesSyncCursorRow, "au"))
    assert cursor is not None
