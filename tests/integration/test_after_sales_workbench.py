from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.after_sales.service as after_sales_service
from app.config.settings import Settings
from app.connectors.models import ServiceTicket, TicketReceipt
from app.core.security import generate_staff_token
from app.core.time import utcnow
from app.main import create_app
from app.providers.fake import FakeLLMProvider
from app.repositories.db import init_schema
from app.repositories.models import (
    AfterSalesCaseRow,
    AfterSalesDraftRow,
    AfterSalesEventRow,
    AfterSalesStaffRow,
)
from app.repositories.staff_repository import StaffRepository
from app.retrieval.policy import chunk_matches_scope
from app.schemas.retrieval import PolicyScope

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


def _sync(ctx: Ctx) -> dict[str, Any]:
    response = ctx.client.post(
        "/api/v1/after-sales/au/cases/sync", headers=_auth(ctx.tokens["staff_support_au"])
    )
    assert response.status_code == 200, response.text
    return response.json()


def _case_id(ctx: Ctx) -> int:
    _sync(ctx)
    response = ctx.client.get(
        "/api/v1/after-sales/au/cases", headers=_auth(ctx.tokens["staff_support_au"])
    )
    assert response.status_code == 200, response.text
    return int(response.json()["items"][0]["case_id"])


def _detail(ctx: Ctx, case_id: int, staff: str = "staff_support_au") -> dict[str, Any]:
    response = ctx.client.get(
        f"/api/v1/after-sales/au/cases/{case_id}", headers=_auth(ctx.tokens[staff])
    )
    assert response.status_code == 200, response.text
    return response.json()


def _evidence(
    ctx: Ctx, case_id: int, staff: str = "staff_support_au", **params: Any
) -> dict[str, Any]:
    response = ctx.client.get(
        f"/api/v1/after-sales/au/cases/{case_id}/evidence",
        headers=_auth(ctx.tokens[staff]),
        params=params,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _faults_chunk(ctx: Ctx, case_id: int) -> str:
    body = _evidence(ctx, case_id)
    for chunk in body["chunks"]:
        if "faults-missing" in chunk["source_path"]:
            return str(chunk["chunk_id"])
    return str(body["chunks"][0]["chunk_id"])


def _inject_llm(ctx: Ctx, provider: Any) -> None:
    ctx.app.state.container.after_sales_workflow._llm = provider


def _valid_suggestion(chunk_id: str) -> str:
    return json.dumps(
        {
            "action": "reship",
            "internal_summary": "Synthetic AI suggestion for a damaged item.",
            "consumer_reply": "We will reship the damaged item and share tracking.",
            "evidence_chunk_ids": [chunk_id],
        }
    )


def _post_suggestion(ctx: Ctx, case_id: int, revision: int, *, staff: str = "staff_support_au"):
    return ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/suggestion",
        headers=_auth(ctx.tokens[staff]),
        json={"expected_revision": revision},
    )


def _create_draft(ctx: Ctx, case_id: int, revision: int, chunk_id: str):
    return ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/drafts",
        headers=_auth(ctx.tokens["staff_support_au"]),
        json={
            "expected_revision": revision,
            "action": "reship",
            "internal_summary": "Manual review of the synthetic damaged-item request.",
            "consumer_reply": "We will reship the item and share the new tracking number.",
            "evidence_chunk_ids": [chunk_id],
        },
    )


def _prepare_approved(ctx: Ctx) -> tuple[int, dict[str, Any], str]:
    case_id = _case_id(ctx)
    detail = _detail(ctx, case_id)
    created = _create_draft(ctx, case_id, detail["revision"], _faults_chunk(ctx, case_id))
    assert created.status_code == 200, created.text
    detail = created.json()
    submitted = ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/drafts/"
        f"{detail['current_draft']['version']}/submit",
        headers=_auth(ctx.tokens["staff_support_au"]),
        json={"expected_revision": detail["revision"]},
    )
    assert submitted.status_code == 200, submitted.text
    detail = submitted.json()
    approval_id = detail["approvals"][-1]["approval_id"]
    approved = ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/approvals/{approval_id}/approve",
        headers=_auth(ctx.tokens["staff_approver_au"]),
        json={"expected_revision": detail["revision"]},
    )
    assert approved.status_code == 200, approved.text
    return case_id, approved.json(), approval_id


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


async def _count(database_url: str, model: Any) -> int:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with factory() as session:
            return int(await session.scalar(select(func.count()).select_from(model)) or 0)
    finally:
        await engine.dispose()


class _MutatingLLM(FakeLLMProvider):
    def __init__(self, mutate: Any, content: str) -> None:
        super().__init__({"": content})
        self._mutate = mutate

    async def generate(self, messages: list[dict[str, str]], **kwargs: Any):
        await self._mutate()
        return await super().generate(messages, **kwargs)


def _noon(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, tzinfo=UTC)


class _MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _SequenceClock:
    def __init__(self, values: list[datetime]) -> None:
        self._values = values
        self._index = 0

    def __call__(self) -> datetime:
        value = self._values[min(self._index, len(self._values) - 1)]
        self._index += 1
        return value


def _all_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key))
            keys |= _all_keys(item)
    elif isinstance(value, list):
        for item in value:
            keys |= _all_keys(item)
    return keys


# ----------------------------------------------------------------- evidence


def test_evidence_is_market_scope_date_and_role_bounded(ctx: Ctx) -> None:
    case_id = _case_id(ctx)
    body = _evidence(ctx, case_id)
    assert body["case_id"] == case_id
    assert body["market"] == "au"
    assert body["missing_reasons"] == []
    assert 0 < len(body["chunks"]) <= 12
    assert body["queried_at"]
    assert body["trusted_categories"] == [
        {"sku": "SYNTH-MUG-001", "category": "homewares", "source": "synthetic_demo"}
    ]
    as_of = date.fromisoformat(body["as_of"])
    corpus = {chunk.chunk_id: chunk for chunk in ctx.app.state.container.retriever.corpus}
    for view in body["chunks"]:
        chunk = corpus[view["chunk_id"]]
        assert chunk.brand == "epig"
        assert chunk.market == "au"
        assert "homewares" in chunk.product_categories
        assert chunk_matches_scope(
            chunk,
            PolicyScope(brand="epig", market="au", product_category="homewares", as_of=as_of),
        )
        assert view["policy_version"] == chunk.policy_version

    # Client-supplied overrides are not part of the route and cannot expand scope.
    overridden = _evidence(
        ctx,
        case_id,
        product_category="mattress",
        policy_version="forged",
        as_of="2000-01-01",
    )
    assert [item["chunk_id"] for item in overridden["chunks"]] == [
        item["chunk_id"] for item in body["chunks"]
    ]

    for staff in ("staff_approver_au", "staff_auditor"):
        assert _evidence(ctx, case_id, staff)["chunks"]
    for staff in ("staff_operator", "staff_support_us"):
        response = ctx.client.get(
            f"/api/v1/after-sales/au/cases/{case_id}/evidence",
            headers=_auth(ctx.tokens[staff]),
        )
        assert response.status_code == 403
    missing = ctx.client.get(
        "/api/v1/after-sales/au/cases/999999/evidence",
        headers=_auth(ctx.tokens["staff_support_au"]),
    )
    assert missing.status_code == 404


def test_evidence_without_trusted_category_explains_why(ctx: Ctx) -> None:
    case_id = _case_id(ctx)
    connector = ctx.app.state.container.business_connector
    state = connector._state.orders[("au", "ord_au_00000042")]
    item = state.items[0].model_copy(update={"sku": "UNMAPPED-SKU"})
    connector._state.orders[("au", "ord_au_00000042")] = state.model_copy(update={"items": [item]})
    body = _evidence(ctx, case_id)
    assert body["chunks"] == []
    assert "missing_sku_category" in body["missing_reasons"]
    assert body["trusted_categories"] == []


# --------------------------------------------------------- approval snapshot


def test_approval_snapshot_is_frozen_and_whitelisted(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_approved(ctx)
    before = next(item for item in detail["approvals"] if item["approval_id"] == approval_id)
    snapshot = before["snapshot"]
    assert snapshot is not None
    assert snapshot["draft"]["version"] == detail["current_draft"]["version"]
    assert snapshot["order"]["order_id"] == detail["order_id"]
    assert snapshot["evidence"]["chunks"]
    assert snapshot["requester"]["roles"] == ["support"]
    assert datetime.fromisoformat(snapshot["approval"]["expires_at"]).replace(
        tzinfo=None
    ) == datetime.fromisoformat(before["expires_at"].replace("Z", "+00:00")).replace(tzinfo=None)
    leaked = _all_keys(snapshot) & {
        "idempotency_key",
        "payload_json",
        "token",
        "token_sha256",
        "service_token",
        "authorization",
    }
    assert leaked == set()

    # A later current draft does not rewrite the stored submission basis.
    latest = _detail(ctx, case_id)
    created = _create_draft(ctx, case_id, latest["revision"], _faults_chunk(ctx, case_id))
    assert created.status_code == 200, created.text
    after = _detail(ctx, case_id)
    frozen = next(item for item in after["approvals"] if item["approval_id"] == approval_id)
    assert frozen["snapshot"]["draft"] == snapshot["draft"]
    assert frozen["snapshot"]["evidence"] == snapshot["evidence"]


# --------------------------------------------------------------- suggestion


def test_fake_suggestion_saves_only_an_editing_draft(ctx: Ctx) -> None:
    case_id = _case_id(ctx)
    chunk_id = _faults_chunk(ctx, case_id)
    provider = FakeLLMProvider(canned={"": _valid_suggestion(chunk_id)})
    _inject_llm(ctx, provider)
    before = _detail(ctx, case_id)
    response = _post_suggestion(ctx, case_id, before["revision"])
    assert response.status_code == 200, response.text
    detail = response.json()
    draft = detail["current_draft"]
    assert draft["status"] == "editing"
    assert draft["version"] == 1
    assert draft["action"] == "reship"
    assert [item["chunk_id"] for item in draft["evidence"]] == [chunk_id]
    assert detail["operations"] == []
    assert detail["state"] == "processing"
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0
    assert asyncio.run(_count(ctx.database_url, AfterSalesDraftRow)) == 1

    call = provider.calls[0]
    assert all("Bearer" not in message["content"] for message in call)
    # Only the case's server-derived AU policy context is supplied.
    assert all('"market":"au"' in message["content"] for message in call[1:])

    # The body cannot smuggle action, evidence or identity fields.
    smuggled = ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/suggestion",
        headers=_auth(ctx.tokens["staff_support_au"]),
        json={"expected_revision": detail["revision"], "action": "refund"},
    )
    assert smuggled.status_code == 422

    for staff in ("staff_approver_au", "staff_auditor", "staff_operator"):
        forbidden = _post_suggestion(ctx, case_id, detail["revision"], staff=staff)
        assert forbidden.status_code == 403


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps(
            {
                "action": "reship",
                "internal_summary": "summary",
                "consumer_reply": "reply",
                "evidence_chunk_ids": [],
            }
        ),
        json.dumps(
            {
                "action": "delete_everything",
                "internal_summary": "summary",
                "consumer_reply": "reply",
                "evidence_chunk_ids": ["forged_chunk"],
            }
        ),
        json.dumps(
            {
                "action": "reship",
                "internal_summary": "summary",
                "consumer_reply": "reply",
                "evidence_chunk_ids": ["forged_chunk"],
            }
        ),
        json.dumps(
            {
                "action": "reship",
                "internal_summary": "summary",
                "consumer_reply": "reply",
                "evidence_chunk_ids": ["placeholder"],
                "approval_id": "apr_forged",
            }
        ),
    ],
)
def test_invalid_model_output_never_saves_a_draft(ctx: Ctx, content: str) -> None:
    case_id = _case_id(ctx)
    chunk_id = _faults_chunk(ctx, case_id)
    # Replace the placeholder with a real chunk for the subset/unknown-field cases.
    content = content.replace('"placeholder"', json.dumps(chunk_id))
    _inject_llm(ctx, FakeLLMProvider(canned={"": content}))
    detail = _detail(ctx, case_id)
    response = _post_suggestion(ctx, case_id, detail["revision"])
    assert response.status_code == 502, response.text
    assert _detail(ctx, case_id)["current_draft"] is None
    assert asyncio.run(_count(ctx.database_url, AfterSalesDraftRow)) == 0
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0


def test_suggestion_requires_a_configured_generation_model(ctx: Ctx) -> None:
    case_id = _case_id(ctx)
    detail = _detail(ctx, case_id)
    response = _post_suggestion(ctx, case_id, detail["revision"])
    assert response.status_code == 503
    assert response.json()["error_code"] == "LLM_PROVIDER_UNAVAILABLE"
    assert _detail(ctx, case_id)["current_draft"] is None


class _FailingLLM:
    async def generate(self, messages: list[dict[str, str]], **kwargs: Any):
        raise RuntimeError("upstream body with secret token should not leak")


def test_provider_failure_is_safe_and_saves_nothing(ctx: Ctx) -> None:
    case_id = _case_id(ctx)
    _inject_llm(ctx, _FailingLLM())
    detail = _detail(ctx, case_id)
    response = _post_suggestion(ctx, case_id, detail["revision"])
    assert response.status_code == 503
    assert "secret token" not in response.text
    assert _detail(ctx, case_id)["current_draft"] is None


# ---------------------------------------- post-await revalidation and CAS


def test_revision_change_during_generation_is_refused_without_overwrite(ctx: Ctx) -> None:
    case_id = _case_id(ctx)
    chunk_id = _faults_chunk(ctx, case_id)
    detail = _detail(ctx, case_id)

    async def mutate() -> None:
        # This separate write must not be blocked by a transaction held over
        # the model await; it also changes the CAS revision.
        await _update_row(
            ctx.database_url,
            AfterSalesCaseRow,
            case_id,
            revision=detail["revision"] + 1,
        )

    _inject_llm(ctx, _MutatingLLM(mutate, _valid_suggestion(chunk_id)))
    response = _post_suggestion(ctx, case_id, detail["revision"])
    assert response.status_code == 409, response.text
    assert _detail(ctx, case_id)["current_draft"] is None
    assert asyncio.run(_count(ctx.database_url, AfterSalesDraftRow)) == 0


def test_order_change_without_revision_change_is_refused(ctx: Ctx) -> None:
    case_id = _case_id(ctx)
    chunk_id = _faults_chunk(ctx, case_id)
    detail = _detail(ctx, case_id)
    connector = ctx.app.state.container.business_connector
    original = connector._state.orders[("au", "ord_au_00000042")]

    async def mutate() -> None:
        connector._state.orders[("au", "ord_au_00000042")] = original.model_copy(
            update={"order_version": "ov_changed", "total_minor": original.total_minor + 1}
        )

    _inject_llm(ctx, _MutatingLLM(mutate, _valid_suggestion(chunk_id)))
    response = _post_suggestion(ctx, case_id, detail["revision"])
    assert response.status_code == 409, response.text
    assert _detail(ctx, case_id)["current_draft"] is None


def test_attachment_change_without_revision_change_is_refused(ctx: Ctx) -> None:
    case_id = _case_id(ctx)
    chunk_id = _faults_chunk(ctx, case_id)
    detail = _detail(ctx, case_id)
    connector = ctx.app.state.container.business_connector
    request_state = connector._state.requests[("au", "asr_000000000042")]
    original = request_state.request

    async def mutate() -> None:
        attachment = original.attachments[0].model_copy(
            update={"size_bytes": original.attachments[0].size_bytes + 1}
        )
        request_state.request = original.model_copy(update={"attachments": [attachment]})

    _inject_llm(ctx, _MutatingLLM(mutate, _valid_suggestion(chunk_id)))
    response = _post_suggestion(ctx, case_id, detail["revision"])
    assert response.status_code == 409, response.text
    assert _detail(ctx, case_id)["current_draft"] is None


def test_policy_change_without_revision_change_is_refused(ctx: Ctx) -> None:
    case_id = _case_id(ctx)
    chunk_id = _faults_chunk(ctx, case_id)
    detail = _detail(ctx, case_id)

    async def mutate() -> None:
        for chunk in ctx.app.state.container.retriever.corpus:
            chunk.content += " (changed after generation started)"

    _inject_llm(ctx, _MutatingLLM(mutate, _valid_suggestion(chunk_id)))
    response = _post_suggestion(ctx, case_id, detail["revision"])
    assert response.status_code == 409, response.text
    assert _detail(ctx, case_id)["current_draft"] is None


def test_permission_change_without_revision_change_is_forbidden(ctx: Ctx) -> None:
    case_id = _case_id(ctx)
    chunk_id = _faults_chunk(ctx, case_id)
    detail = _detail(ctx, case_id)

    async def mutate() -> None:
        await _update_row(
            ctx.database_url,
            AfterSalesStaffRow,
            "staff_support_au",
            roles="approver",
        )

    _inject_llm(ctx, _MutatingLLM(mutate, _valid_suggestion(chunk_id)))
    response = _post_suggestion(ctx, case_id, detail["revision"])
    assert response.status_code == 403, response.text
    assert _detail(ctx, case_id, "staff_auditor")["current_draft"] is None


@pytest.mark.parametrize(
    "revocation",
    [{"roles": "approver"}, {"allowed_markets": "us"}],
    ids=["role", "market"],
)
def test_revocation_during_post_generation_source_read_is_forbidden(
    ctx: Ctx, revocation: dict[str, str]
) -> None:
    case_id, _approved_detail, approval_id = _prepare_approved(ctx)
    chunk_id = _faults_chunk(ctx, case_id)
    before = _detail(ctx, case_id, "staff_auditor")
    before_drafts = asyncio.run(_count(ctx.database_url, AfterSalesDraftRow))
    before_events = asyncio.run(_count(ctx.database_url, AfterSalesEventRow))
    approval_before = next(
        item for item in before["approvals"] if item["approval_id"] == approval_id
    )
    operation_before = before["operations"][0]
    assert approval_before["status"] == "approved"
    assert operation_before["status"] == "prepared"

    connector = ctx.app.state.container.business_connector
    original_get_service_order = connector.get_service_order
    order_reads = 0

    async def get_service_order(*, market: str, order_id: str):
        nonlocal order_reads
        order_reads += 1
        order = await original_get_service_order(market=market, order_id=order_id)
        if order_reads == 2:
            # The first read is before the model call; this one is inside the
            # post-await revalidation. Revoke while that read is in flight.
            await _update_row(
                ctx.database_url,
                AfterSalesStaffRow,
                "staff_support_au",
                **revocation,
            )
        return order

    connector.get_service_order = get_service_order
    _inject_llm(ctx, FakeLLMProvider(canned={"": _valid_suggestion(chunk_id)}))
    response = _post_suggestion(ctx, case_id, before["revision"])
    assert response.status_code == 403, response.text
    assert order_reads == 2

    after = _detail(ctx, case_id, "staff_auditor")
    assert after["revision"] == before["revision"]
    assert after["current_draft"] == before["current_draft"]
    assert asyncio.run(_count(ctx.database_url, AfterSalesDraftRow)) == before_drafts
    assert asyncio.run(_count(ctx.database_url, AfterSalesEventRow)) == before_events
    approval_after = next(item for item in after["approvals"] if item["approval_id"] == approval_id)
    operation_after = next(
        item
        for item in after["operations"]
        if item["operation_id"] == operation_before["operation_id"]
    )
    assert approval_after["status"] == "approved"
    assert operation_after["status"] == "prepared"
    assert operation_after["dispatched_at"] is None


def test_cross_day_revalidation_uses_fresh_date_for_expired_policy(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_id = _case_id(ctx)
    chunk_id = _faults_chunk(ctx, case_id)
    detail = _detail(ctx, case_id)
    events_before = asyncio.run(_count(ctx.database_url, AfterSalesEventRow))
    fresh_day = date.today()
    generation_day = fresh_day - timedelta(days=1)
    for chunk in ctx.app.state.container.retriever.corpus:
        if (
            chunk.brand == "epig"
            and chunk.market == "au"
            and "homewares" in chunk.product_categories
        ):
            chunk.effective_to = generation_day

    clock = _MutableClock(_noon(generation_day))
    monkeypatch.setattr(after_sales_service, "utcnow", clock)

    async def cross_midnight() -> None:
        clock.value = _noon(fresh_day)

    _inject_llm(ctx, _MutatingLLM(cross_midnight, _valid_suggestion(chunk_id)))
    response = _post_suggestion(ctx, case_id, detail["revision"])
    assert response.status_code == 409, response.text
    assert "missing_policy_evidence" in response.json()["message"]
    assert _detail(ctx, case_id)["current_draft"] is None
    assert asyncio.run(_count(ctx.database_url, AfterSalesDraftRow)) == 0
    assert asyncio.run(_count(ctx.database_url, AfterSalesEventRow)) == events_before


def test_cross_day_revalidation_refuses_even_when_policy_is_still_valid(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_id = _case_id(ctx)
    chunk_id = _faults_chunk(ctx, case_id)
    detail = _detail(ctx, case_id)
    fresh_day = date.today()
    generation_day = fresh_day - timedelta(days=1)

    clock = _MutableClock(_noon(generation_day))
    monkeypatch.setattr(after_sales_service, "utcnow", clock)

    async def cross_midnight() -> None:
        clock.value = _noon(fresh_day)

    _inject_llm(ctx, _MutatingLLM(cross_midnight, _valid_suggestion(chunk_id)))
    response = _post_suggestion(ctx, case_id, detail["revision"])
    assert response.status_code == 409, response.text
    assert response.json()["message"] == "the suggestion context changed while generating"
    assert _detail(ctx, case_id)["current_draft"] is None
    assert asyncio.run(_count(ctx.database_url, AfterSalesDraftRow)) == 0


def test_save_suggestion_refuses_a_utc_date_change_after_fresh_revalidation(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_id = _case_id(ctx)
    chunk_id = _faults_chunk(ctx, case_id)
    detail = _detail(ctx, case_id)
    events_before = asyncio.run(_count(ctx.database_url, AfterSalesEventRow))
    policy_day = date.today()
    save_day = policy_day + timedelta(days=1)

    # Generation and fresh revalidation agree on the policy day, so the
    # fingerprint still matches. Only the save entry crosses midnight.
    clock = _SequenceClock([_noon(policy_day), _noon(policy_day), _noon(save_day)])
    monkeypatch.setattr(after_sales_service, "utcnow", clock)

    _inject_llm(ctx, FakeLLMProvider(canned={"": _valid_suggestion(chunk_id)}))
    response = _post_suggestion(ctx, case_id, detail["revision"])
    assert response.status_code == 409, response.text
    assert response.json()["message"] == "the suggestion policy date changed before it was saved"
    assert _detail(ctx, case_id)["current_draft"] is None
    assert asyncio.run(_count(ctx.database_url, AfterSalesDraftRow)) == 0
    assert asyncio.run(_count(ctx.database_url, AfterSalesEventRow)) == events_before


def test_unknown_execution_context_allows_only_a_candidate_draft(ctx: Ctx) -> None:
    case_id, detail, _approval_id = _prepare_approved(ctx)
    chunk_id = _faults_chunk(ctx, case_id)
    asyncio.run(_update_row(ctx.database_url, AfterSalesCaseRow, case_id, state="executing"))
    _inject_llm(ctx, FakeLLMProvider(canned={"": _valid_suggestion(chunk_id)}))
    response = _post_suggestion(ctx, case_id, detail["revision"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "executing"
    assert body["current_draft"]["version"] == 1
    assert body["current_draft"]["status"] == "approved"
    candidate = next(item for item in body["drafts"] if item["version"] == 2)
    assert candidate["status"] == "editing"

    blocked = ctx.client.post(
        f"/api/v1/after-sales/au/cases/{case_id}/drafts/2/submit",
        headers=_auth(ctx.tokens["staff_support_au"]),
        json={"expected_revision": body["revision"]},
    )
    assert blocked.status_code == 409
    assert ctx.app.state.container.business_connector.create_ticket_calls == 0


# ------------------------------------------------------------------- ticket


def _external_ticket(market: str = "au") -> ServiceTicket:
    return ServiceTicket(
        ticket_id="tic_external_0001",
        request_id="asr_000000000042",
        order_id=f"ord_{market}_00000042",
        market=market,
        action="reship",
        reply="An earlier external ticket exists for this request.",
        status="open",
        draft_version="1",
        approval_id="apr_external",
        policy_version="demo-2026-11-05",
        order_version="ov_000000000007",
        receipt=TicketReceipt(
            receipt_id="rcp_external_0001",
            ticket_id="tic_external_0001",
            request_id="asr_000000000042",
            action="reship",
            request_status="ticket_created",
            created_at=datetime(2026, 1, 15, 4, 0, tzinfo=UTC),
        ),
        created_at=datetime(2026, 1, 15, 4, 0, tzinfo=UTC),
        updated_at=datetime(2026, 1, 15, 4, 0, tzinfo=UTC),
    )


def test_ticket_requires_an_association_and_reads_external_only(ctx: Ctx) -> None:
    case_id = _case_id(ctx)
    response = ctx.client.get(
        f"/api/v1/after-sales/au/cases/{case_id}/ticket",
        headers=_auth(ctx.tokens["staff_support_au"]),
    )
    assert response.status_code == 404

    connector = ctx.app.state.container.business_connector
    ticket = _external_ticket()
    connector._state.tickets[("au", ticket.ticket_id)] = ticket
    request_state = connector._state.requests[("au", "asr_000000000042")]
    request_state.request = request_state.request.model_copy(
        update={
            "status": "ticket_created",
            "ticket_id": ticket.ticket_id,
            "sequence": "00000000000000000043",
            "updated_at": ticket.updated_at,
        }
    )
    _sync(ctx)
    body = ctx.client.get(
        f"/api/v1/after-sales/au/cases/{case_id}/ticket",
        headers=_auth(ctx.tokens["staff_auditor"]),
    )
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["verification_state"] == "external_only"
    assert payload["ticket"]["ticket_id"] == ticket.ticket_id
    assert payload["ticket"]["receipt"]["receipt_id"] == "rcp_external_0001"
    assert payload["queried_at"]
    assert _detail(ctx, case_id)["state"] == "reconciliation_required"

    for staff in ("staff_operator", "staff_support_us"):
        denied = ctx.client.get(
            f"/api/v1/after-sales/au/cases/{case_id}/ticket",
            headers=_auth(ctx.tokens[staff]),
        )
        assert denied.status_code == 403


def test_ticket_matches_original_operation_not_the_current_case(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_approved(ctx)
    portal = ctx.client.portal
    assert portal is not None
    report = portal.call(ctx.app.state.container.after_sales_execution.run_once, ["au"])
    assert report.succeeded == 1
    connector = ctx.app.state.container.business_connector
    ticket_id = _detail(ctx, case_id)["remote_ticket_id"]
    assert ticket_id

    # Mutating mutable case fields must not invalidate the frozen approval basis.
    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesCaseRow,
            case_id,
            description="A later description that is not the approved one.",
            revision=detail["revision"] + 10,
        )
    )
    body = ctx.client.get(
        f"/api/v1/after-sales/au/cases/{case_id}/ticket",
        headers=_auth(ctx.tokens["staff_approver_au"]),
    )
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["verification_state"] == "matched_operation"
    assert payload["operation_id"] == detail["operations"][0]["operation_id"]

    original = connector._state.tickets[("au", ticket_id)]
    connector._state.tickets[("au", ticket_id)] = original.model_copy(
        update={"reply": "A different reply than the approved payload."}
    )
    mismatch = ctx.client.get(
        f"/api/v1/after-sales/au/cases/{case_id}/ticket",
        headers=_auth(ctx.tokens["staff_support_au"]),
    )
    assert mismatch.status_code == 502
    assert "different reply" not in mismatch.text


def test_ticket_receipt_mismatch_is_rejected(ctx: Ctx) -> None:
    case_id, _detail_body, _approval_id = _prepare_approved(ctx)
    portal = ctx.client.portal
    assert portal is not None
    assert (
        portal.call(ctx.app.state.container.after_sales_execution.run_once, ["au"]).succeeded == 1
    )
    connector = ctx.app.state.container.business_connector
    ticket_id = _detail(ctx, case_id)["remote_ticket_id"]
    original = connector._state.tickets[("au", ticket_id)]
    connector._state.tickets[("au", ticket_id)] = original.model_copy(
        update={"receipt": original.receipt.model_copy(update={"action": "refund"})}
    )
    response = ctx.client.get(
        f"/api/v1/after-sales/au/cases/{case_id}/ticket",
        headers=_auth(ctx.tokens["staff_support_au"]),
    )
    assert response.status_code == 502
    assert "refund" not in response.text


def test_ticket_read_does_not_advance_state(ctx: Ctx) -> None:
    case_id, detail, approval_id = _prepare_approved(ctx)
    portal = ctx.client.portal
    assert portal is not None
    assert (
        portal.call(ctx.app.state.container.after_sales_execution.run_once, ["au"]).succeeded == 1
    )
    before = _detail(ctx, case_id)
    for _ in range(2):
        response = ctx.client.get(
            f"/api/v1/after-sales/au/cases/{case_id}/ticket",
            headers=_auth(ctx.tokens["staff_support_au"]),
        )
        assert response.status_code == 200
    after = _detail(ctx, case_id)
    assert after["state"] == before["state"]
    assert after["revision"] == before["revision"]
    assert after["operations"] == before["operations"]
    assert detail["operations"]
    assert approval_id
