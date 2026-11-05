"""Persistent after-sales workflow: sync, drafts, approvals and operations.

The service deliberately stops before external execution. Approval persists an
``approved`` record and exactly one ``prepared`` operation with a stable key
and fixed canonical payload. No route or service here calls ``create_ticket``.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any, NoReturn, cast

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.connectors.base import BusinessConnector
from app.connectors.errors import ConnectorNotFound
from app.connectors.models import (
    Attachment,
    ServiceAfterSalesRequest,
    ServiceOrder,
    ServiceShipment,
    TicketAction,
)
from app.core.exceptions import (
    ConnectorContractError,
    DatabaseError,
    InvalidRequestError,
    NotFoundError,
    RevisionConflictError,
    SelfApprovalForbiddenError,
    StateConflictError,
)
from app.core.time import as_utc, utcnow
from app.repositories.after_sales_repository import AfterSalesRepository
from app.repositories.models import (
    AfterSalesApprovalRow,
    AfterSalesCaseRow,
    AfterSalesDraftRow,
    AfterSalesEventRow,
    AfterSalesMaterialRow,
    AfterSalesOperationRow,
    AfterSalesSyncCursorRow,
)
from app.repositories.staff_repository import StaffRepository
from app.retrieval.hybrid import HybridRetriever
from app.schemas.after_sales import AFTER_SALES_MARKETS, StaffActor
from app.schemas.after_sales_workflow import (
    ApprovalView,
    ApproveRequest,
    AttachmentView,
    CaseDetail,
    CaseListResponse,
    CaseState,
    CaseSummary,
    DraftCreateRequest,
    DraftView,
    EventListResponse,
    EventView,
    EvidenceView,
    MaterialCreateRequest,
    MaterialView,
    OperationView,
    RejectRequest,
    SubmitRequest,
    SyncResponse,
)
from app.services.after_sales.evidence import (
    CategoryResolver,
    EvidenceResolver,
    EvidenceResult,
    TrustedCategory,
)
from app.services.after_sales.payload import (
    approval_snapshot_digest,
    canonical_json,
    operation_payload,
    sha256_json,
    sha256_text,
)

logger = logging.getLogger(__name__)

# DTC-2b has no receipt verifier. A remote ``ticket_created`` observation is
# therefore only a projection: keep the case explicitly non-terminal and let
# the 2c reconciler read back the ticket and receipt before completion.
_REMOTE_VERIFICATION_STATE = "reconciliation_required"

# Policy documents (by file stem) that must back each consequential action.
_ACTION_EVIDENCE_TOPICS: dict[str, frozenset[str]] = {
    "return": frozenset({"returns"}),
    "refund": frozenset({"refunds"}),
    "reship": frozenset({"faults-missing"}),
}
_REVIEW_EVIDENCE_TOPICS = frozenset(
    {"returns", "refunds", "faults-missing", "cancellation", "delivery", "warranty"}
)
_STRUCTURED_DELIVERY_MATERIALS = frozenset({"manual_verification", "delivery_verification"})


class AfterSalesWorkflow:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        connector: BusinessConnector,
        retriever: HybridRetriever | None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._connector = connector
        self._categories = CategoryResolver(settings)
        self._evidence = EvidenceResolver(retriever)

    # --------------------------------------------------------------- reading

    async def list_cases(
        self,
        market: str,
        *,
        status: str | None,
        request_type: str | None,
        updated_from: datetime | None,
        updated_to: datetime | None,
        limit: int,
        offset: int,
    ) -> CaseListResponse:
        filters = [AfterSalesCaseRow.market == market]
        if status:
            filters.append(AfterSalesCaseRow.state == status)
        if request_type:
            filters.append(AfterSalesCaseRow.request_type == request_type)
        if updated_from is not None:
            filters.append(AfterSalesCaseRow.updated_at >= as_utc(updated_from))
        if updated_to is not None:
            filters.append(AfterSalesCaseRow.updated_at <= as_utc(updated_to))
        async with self._session_factory() as session:
            repository = AfterSalesRepository(session)
            total = await session.scalar(
                select(func.count()).select_from(AfterSalesCaseRow).where(*filters)
            )
            rows = list(
                await session.scalars(
                    select(AfterSalesCaseRow)
                    .where(*filters)
                    .order_by(
                        AfterSalesCaseRow.updated_at.desc(),
                        AfterSalesCaseRow.case_id.desc(),
                    )
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            case_ids = [row.case_id for row in rows]
            pending = await repository.pending_case_ids(case_ids)
            prepared = await repository.prepared_case_ids(case_ids)
            cursor = await repository.get_cursor(market)
            last_success_at = (
                as_utc(cursor.last_success_at)
                if cursor is not None and cursor.last_success_at is not None
                else None
            )
        return CaseListResponse(
            items=[
                CaseSummary(
                    case_id=row.case_id,
                    market=cast(Any, row.market),
                    request_id=row.request_id,
                    order_id=row.order_id,
                    request_type=cast(Any, row.request_type),
                    state=cast(CaseState, row.state),
                    revision=row.revision,
                    attachment_count=_attachment_count(row.attachments_json),
                    remote_status=cast(Any, row.remote_status),
                    remote_ticket_id=row.remote_ticket_id,
                    updated_at=as_utc(row.updated_at),
                    has_pending_approval=row.case_id in pending,
                    has_prepared_operation=row.case_id in prepared,
                )
                for row in rows
            ],
            total=int(total or 0),
            limit=limit,
            offset=offset,
            has_more=has_more,
            last_success_at=last_success_at,
        )

    async def get_case_detail(self, market: str, case_id: int) -> CaseDetail:
        async with self._session_factory() as session:
            repository = AfterSalesRepository(session)
            case = await repository.get_case(market, case_id)
            if case is None:
                raise NotFoundError()
            materials = await repository.list_materials(case_id)
            drafts = await repository.list_drafts(case_id)
            approvals = await repository.list_approvals(case_id)
            operations = await repository.list_operations(case_id)
        return _detail(case, materials, drafts, approvals, operations)

    async def list_events(
        self, market: str, case_id: int, *, after_seq: int, limit: int
    ) -> EventListResponse:
        async with self._session_factory() as session:
            repository = AfterSalesRepository(session)
            case = await repository.get_case(market, case_id)
            if case is None:
                raise NotFoundError()
            rows = list(await repository.list_events(case_id, after_seq=after_seq, limit=limit))
        has_more = len(rows) > limit
        rows = rows[:limit]
        return EventListResponse(
            items=[_event_view(row) for row in rows],
            after_seq=after_seq,
            limit=limit,
            has_more=has_more,
        )

    # ------------------------------------------------------------------ sync

    async def sync(self, actor: StaffActor, market: str) -> SyncResponse:
        """Staff-triggered supplement to the background service poll."""
        return await self._sync_page(market, actor_staff_id=actor.staff_id, service_poll=False)

    async def sync_service(self, market: str) -> SyncResponse:
        """Background service poll.

        This entry never invents a staff identity or an approval. Events carry
        ``actor_staff_id=None`` and a ``service_poll`` reason so operator
        auditing can distinguish an E-pig observation from a human action.
        """
        return await self._sync_page(market, actor_staff_id=None, service_poll=True)

    async def _sync_page(
        self, market: str, *, actor_staff_id: str | None, service_poll: bool
    ) -> SyncResponse:
        if market not in AFTER_SALES_MARKETS:
            raise ValueError(f"unsupported after-sales market: {market}")
        async with self._session_factory() as session:
            cursor = await AfterSalesRepository(session).get_cursor(market)
            cursor_exists = cursor is not None
            start_cursor = cursor.cursor if cursor is not None else None
            start_revision = cursor.revision if cursor is not None else 0
            start_high_watermark = cursor.high_watermark if cursor is not None else "0"

        # Network reads happen before the database transaction. Holding a
        # transaction open across this await would pin a pool connection and,
        # on PostgreSQL, keep locks while talking to E-pig.
        feed = await self._connector.list_service_requests(
            market=market,
            cursor=start_cursor,
            limit=self._settings.after_sales_sync_batch_limit,
        )
        orders: dict[str, ServiceOrder | None] = {}
        for item in feed.items:
            if item.market != market:
                raise ConnectorContractError("feed item market does not match the request")
            try:
                order = await self._connector.get_service_order(
                    market=market, order_id=item.order_id
                )
            except ConnectorNotFound:
                order = None
            if order is not None and (order.market != market or order.order_id != item.order_id):
                raise ConnectorContractError("order identity does not match the feed item")
            orders[item.request_id] = order

        incoming_high_watermark = _parse_watermark(feed.high_watermark)
        high_watermark = str(max(_parse_watermark(start_high_watermark), incoming_high_watermark))
        now = utcnow()
        created = updated = stale_ignored = 0

        try:
            # Case upserts and cursor advancement share one transaction. If any
            # case fails, the whole batch rolls back and the cursor never moves.
            async with self._session_factory() as session, session.begin():
                repository = AfterSalesRepository(session)
                for item in feed.items:
                    existing = await repository.get_case_by_request(market, item.request_id)
                    if existing is None:
                        case = _new_case(market, item, orders.get(item.request_id), now)
                        session.add(case)
                        await session.flush()
                        await repository.append_event(
                            case_id=case.case_id,
                            event_type="case_received",
                            actor_staff_id=actor_staff_id,
                            old_state=None,
                            new_state=case.state,
                            entity_type="case",
                            entity_id=str(case.case_id),
                            reason=_sync_event_reason(
                                "remote_ticket_unverified"
                                if case.state == _REMOTE_VERIFICATION_STATE
                                else None,
                                service_poll=service_poll,
                            ),
                        )
                        created += 1
                        continue

                    if _is_stale_remote(existing, item):
                        stale_ignored += 1
                        continue
                    values = _remote_projection(item, existing, orders.get(item.request_id), now)
                    state_changed = False
                    old_state = existing.state
                    # A feed item is not proof that a DTC-approved write was
                    # dispatched or that its receipt was read back. Never mark
                    # the case completed, and never retire the approval,
                    # draft, or operation that produced an unknown result.
                    if item.status == "ticket_created" and old_state not in {
                        "completed",
                        _REMOTE_VERIFICATION_STATE,
                    }:
                        values["state"] = _REMOTE_VERIFICATION_STATE
                        state_changed = True
                    if not values and not state_changed:
                        continue
                    if not await repository.cas_case(
                        market=market,
                        case_id=existing.case_id,
                        expected_revision=existing.revision,
                        values=values,
                    ):
                        raise RevisionConflictError()
                    await session.refresh(existing)
                    await repository.append_event(
                        case_id=existing.case_id,
                        event_type=(
                            "remote_ticket_unverified" if state_changed else "remote_updated"
                        ),
                        actor_staff_id=actor_staff_id,
                        old_state=old_state,
                        new_state=existing.state,
                        entity_type="case",
                        entity_id=str(existing.case_id),
                        reason=_sync_event_reason(
                            "remote_ticket_observed_without_receipt"
                            if state_changed
                            else "remote_projection",
                            service_poll=service_poll,
                        ),
                    )
                    updated += 1

                if not cursor_exists:
                    # First writer inserts revision 1. A concurrent first
                    # insert fails on the primary key and rolls back safely.
                    session.add(
                        AfterSalesSyncCursorRow(
                            market=market,
                            cursor=feed.next_cursor,
                            high_watermark=high_watermark,
                            revision=1,
                            last_success_at=now,
                            updated_at=now,
                        )
                    )
                elif not await repository.advance_cursor(
                    market=market,
                    expected_revision=start_revision,
                    cursor=feed.next_cursor,
                    high_watermark=high_watermark,
                    last_success_at=now,
                ):
                    # The cursor UPDATE is the batch commit point: if another
                    # sync advanced it, no case writes from this batch survive.
                    raise RevisionConflictError()
        except IntegrityError as exc:
            raise StateConflictError("synchronisation raced with another writer") from exc
        except SQLAlchemyError as exc:
            raise DatabaseError("failed to persist a synchronisation batch") from exc

        return SyncResponse(
            market=cast(Any, market),
            processed=len(feed.items),
            created=created,
            updated=updated,
            stale_ignored=stale_ignored,
            has_more=feed.has_more,
            high_watermark=high_watermark,
            last_success_at=now,
        )

    # -------------------------------------------------------------- materials

    async def add_material(
        self,
        actor: StaffActor,
        market: str,
        case_id: int,
        request: MaterialCreateRequest,
    ) -> CaseDetail:
        now = utcnow()
        try:
            async with self._session_factory() as session, session.begin():
                repository = AfterSalesRepository(session)
                case = await repository.get_case(market, case_id)
                if case is None:
                    raise NotFoundError()
                _ensure_writable(case)
                active = await repository.active_operation(case_id)
                blocking = _is_blocking_operation(active)
                old_state = case.state
                new_state = old_state
                if not blocking and old_state in {"pending", "needs_info", "waiting_approval"}:
                    new_state = "processing"
                if not await repository.cas_case(
                    market=market,
                    case_id=case_id,
                    expected_revision=request.expected_revision,
                    values={
                        "state": new_state,
                        "material_revision": case.material_revision + 1,
                    },
                ):
                    raise RevisionConflictError()
                material = AfterSalesMaterialRow(
                    material_id=_new_id("mat"),
                    case_id=case_id,
                    material_type=request.material_type,
                    source=request.source,
                    content=request.content,
                    applies_to_sku=request.applies_to_sku,
                    delivered_at=request.delivered_at,
                    created_by_staff_id=actor.staff_id,
                    created_at=now,
                )
                session.add(material)
                await session.flush()
                if not blocking:
                    await _invalidate_context(
                        repository,
                        case,
                        actor_staff_id=actor.staff_id,
                        reason="materials_changed",
                        now=now,
                    )
                await repository.append_event(
                    case_id=case_id,
                    event_type="material_added",
                    actor_staff_id=actor.staff_id,
                    old_state=old_state,
                    new_state=new_state,
                    entity_type="material",
                    entity_id=material.material_id,
                    reason="materials_changed",
                )
        except IntegrityError as exc:
            raise StateConflictError("the case state changed concurrently") from exc
        except SQLAlchemyError as exc:
            raise DatabaseError("failed to persist the material record") from exc
        return await self.get_case_detail(market, case_id)

    # ----------------------------------------------------------------- drafts

    async def create_draft(
        self,
        actor: StaffActor,
        market: str,
        case_id: int,
        request: DraftCreateRequest,
    ) -> CaseDetail:
        now = utcnow()
        async with self._session_factory() as session:
            repository = AfterSalesRepository(session)
            case = await repository.get_case(market, case_id)
            if case is None:
                raise NotFoundError()
            _ensure_draft_creatable(case)
        order = await self._order_or_none(market, case.order_id)
        categories, missing = _trusted_categories(order, self._categories)
        evidence = self._evidence.resolve(
            market=market,
            chunk_ids=request.evidence_chunk_ids,
            trusted_categories={item.category for item in categories},
            require_category=False,
        )
        draft_id = _new_id("drf")
        try:
            async with self._session_factory() as session, session.begin():
                repository = AfterSalesRepository(session)
                case = await repository.get_case(market, case_id)
                if case is None:
                    raise NotFoundError()
                _ensure_draft_creatable(case)
                # Product 7.2: an unresolved execution context (including an
                # unknown remote result) may gain a new candidate version, but
                # the old current draft/approval/operation must stay intact and
                # the candidate cannot be submitted as a second write.
                active = await repository.active_operation(case_id)
                blocking = _blocks_new_draft(case, active)
                version = await repository.next_draft_version(case_id)
                old_state = case.state
                new_state = old_state
                old_current = (
                    await repository.get_draft_by_id(case.current_draft_id)
                    if case.current_draft_id
                    else None
                )
                if not blocking:
                    if old_state == "pending":
                        new_state = "processing"
                    if old_state == "waiting_approval":
                        new_state = "processing"
                values: dict[str, object] = {"state": new_state}
                if not blocking:
                    values["current_draft_id"] = draft_id
                if not await repository.cas_case(
                    market=market,
                    case_id=case_id,
                    expected_revision=request.expected_revision,
                    values=values,
                ):
                    raise RevisionConflictError()
                draft = AfterSalesDraftRow(
                    draft_id=draft_id,
                    case_id=case_id,
                    version=version,
                    action=request.action,
                    internal_summary=request.internal_summary,
                    consumer_reply=request.consumer_reply,
                    evidence_json=canonical_json(evidence.chunks),
                    evidence_sha256=evidence.evidence_sha256,
                    policy_version=evidence.policy_version,
                    order_version=order.order_version if order is not None else None,
                    material_revision=case.material_revision,
                    category_snapshot_json=canonical_json([item.as_dict() for item in categories]),
                    status="editing",
                    author_staff_id=actor.staff_id,
                    created_at=now,
                )
                session.add(draft)
                await session.flush()
                if not blocking:
                    await _invalidate_context(
                        repository,
                        case,
                        actor_staff_id=actor.staff_id,
                        reason="superseded_by_new_draft",
                        now=now,
                        supersede_draft=old_current,
                    )
                await repository.append_event(
                    case_id=case_id,
                    event_type=("candidate_draft_created" if blocking else "draft_created"),
                    actor_staff_id=actor.staff_id,
                    old_state=old_state,
                    new_state=new_state,
                    entity_type="draft",
                    entity_id=draft_id,
                    reason="active_operation_unresolved" if blocking else None,
                )
        except IntegrityError as exc:
            raise StateConflictError("a draft version already exists") from exc
        except SQLAlchemyError as exc:
            raise DatabaseError("failed to persist the draft") from exc
        return await self.get_case_detail(market, case_id)

    async def submit_draft(
        self,
        actor: StaffActor,
        market: str,
        case_id: int,
        version: int,
        request: SubmitRequest,
    ) -> CaseDetail:
        now = utcnow()
        async with self._session_factory() as session:
            repository = AfterSalesRepository(session)
            case = await repository.get_case(market, case_id)
            if case is None:
                raise NotFoundError()
            _ensure_writable(case)
            draft = await repository.get_draft(case_id, version)
            if draft is None:
                raise NotFoundError()
            if case.current_draft_id != draft.draft_id:
                raise StateConflictError("only the current draft can be submitted")
            if draft.status != "editing":
                raise StateConflictError("only an editing draft can be submitted")
            if case.state not in {"pending", "processing"}:
                raise StateConflictError("the case state does not allow submission")
            active = await repository.active_operation(case_id)
            if active is not None:
                raise StateConflictError("an unresolved operation blocks a new submission")
            materials = await repository.list_materials(case_id)
            material_revision = case.material_revision
            order_id = case.order_id

        try:
            order = await self._connector.get_service_order(market=market, order_id=order_id)
        except ConnectorNotFound:
            order = None
        if order is not None and (order.market != market or order.order_id != order_id):
            raise ConnectorContractError("order identity does not match the case")
        try:
            remote = await self._connector.get_service_request(
                market=market, request_id=case.request_id
            )
        except ConnectorNotFound:
            remote = None
        if remote is not None and (remote.market != market or remote.request_id != case.request_id):
            raise ConnectorContractError("request identity does not match the case")

        shipment: ServiceShipment | None = None
        if draft.action == "return" and order is not None:
            try:
                shipment = await self._connector.get_service_order_shipment(
                    market=market, order_id=order_id
                )
            except ConnectorNotFound:
                shipment = None
            if shipment is not None and shipment.order_id != order_id:
                raise ConnectorContractError("shipment identity does not match the case")

        if remote is not None and remote.status == "ticket_created":
            await self._record_remote_ticket_unverified(
                market,
                case_id,
                request.expected_revision,
                remote=remote,
                order=order,
            )
            raise StateConflictError("E-pig reports a ticket that DTC has not verified")
        if order is None or remote is None:
            await self._mark_needs_info(
                actor, market, case_id, request.expected_revision, "missing_order_or_request"
            )
        categories, missing = _trusted_categories(order, self._categories)
        if missing:
            await self._mark_needs_info(
                actor, market, case_id, request.expected_revision, "missing_sku_category"
            )
        try:
            evidence = self._evidence.resolve(
                market=market,
                chunk_ids=_draft_chunk_ids(draft),
                trusted_categories={item.category for item in categories},
                require_category=True,
                as_of=utcnow().date(),
            )
        except StateConflictError:
            await self._mark_needs_info(
                actor, market, case_id, request.expected_revision, "conflicting_policy"
            )
        if evidence.is_empty:
            await self._mark_needs_info(
                actor, market, case_id, request.expected_revision, "missing_policy_evidence"
            )
        if evidence.policy_version is None:
            await self._mark_needs_info(
                actor, market, case_id, request.expected_revision, "missing_policy_version"
            )
        if _attachments_expired(remote.attachments, now):
            await self._mark_needs_info(
                actor, market, case_id, request.expected_revision, "attachment_expired"
            )

        guard_problems, delivery_proof = _evaluate_action_guard(
            action=draft.action,
            evidence=evidence,
            materials=materials,
            order=order,
            remote=remote,
            shipment=shipment,
            now=now,
        )
        if guard_problems:
            await self._mark_needs_info(
                actor,
                market,
                case_id,
                request.expected_revision,
                ",".join(guard_problems)[:128],
            )

        expires_at = now + timedelta(seconds=self._settings.after_sales_approval_ttl_seconds)
        snapshot = _build_snapshot(
            case=case,
            draft=draft,
            order=order,
            remote=remote,
            shipment=shipment,
            delivery_proof=delivery_proof,
            materials=materials,
            material_revision=material_revision,
            evidence=evidence,
            categories=categories,
            requester=actor,
            expires_at=expires_at,
            now=now,
        )
        snapshot_json = canonical_json(snapshot)
        approval_id = _new_id("apr")
        try:
            async with self._session_factory() as session, session.begin():
                repository = AfterSalesRepository(session)
                case = await repository.get_case(market, case_id)
                if case is None:
                    raise NotFoundError()
                if case.current_draft_id != draft.draft_id:
                    raise StateConflictError("the current draft changed")
                draft = await repository.get_draft(case_id, version)
                if draft is None or draft.status != "editing":
                    raise StateConflictError("the draft is no longer editable")
                if not await repository.cas_case(
                    market=market,
                    case_id=case_id,
                    expected_revision=request.expected_revision,
                    values={"state": "waiting_approval"},
                ):
                    raise RevisionConflictError()
                await repository.update_draft_status(draft, "pending_approval", now=now)
                approval = AfterSalesApprovalRow(
                    approval_id=approval_id,
                    case_id=case_id,
                    draft_id=draft.draft_id,
                    status="pending",
                    requester_staff_id=actor.staff_id,
                    decided_by_staff_id=None,
                    expires_at=expires_at,
                    snapshot_json=snapshot_json,
                    snapshot_sha256=approval_snapshot_digest(snapshot),
                    decision_reason=None,
                    created_at=now,
                    decided_at=None,
                )
                session.add(approval)
                await session.flush()
                await repository.append_event(
                    case_id=case_id,
                    event_type="draft_submitted",
                    actor_staff_id=actor.staff_id,
                    old_state="processing",
                    new_state="waiting_approval",
                    entity_type="approval",
                    entity_id=approval_id,
                    reason="submitted_for_approval",
                )
        except IntegrityError as exc:
            raise StateConflictError("the draft changed while submitting") from exc
        except SQLAlchemyError as exc:
            raise DatabaseError("failed to persist the approval") from exc
        return await self.get_case_detail(market, case_id)

    # --------------------------------------------------------------- approvals

    async def approve(
        self,
        actor: StaffActor,
        market: str,
        case_id: int,
        approval_id: str,
        request: ApproveRequest,
    ) -> CaseDetail:
        now = utcnow()
        async with self._session_factory() as session:
            repository = AfterSalesRepository(session)
            case = await repository.get_case(market, case_id)
            if case is None:
                raise NotFoundError()
            approval = await repository.get_approval(case_id, approval_id)
            if approval is None:
                raise NotFoundError()
            if approval.requester_staff_id == actor.staff_id:
                raise SelfApprovalForbiddenError()
            if approval.status != "pending":
                raise StateConflictError("the approval is no longer pending")
            if case.state != "waiting_approval":
                raise StateConflictError("the case state does not allow approval")
            draft = await repository.get_draft_by_id(approval.draft_id)
            if draft is None:
                raise NotFoundError()
            snapshot = _load_snapshot(approval)
            materials = await repository.list_materials(case_id)
            active = await repository.active_operation(case_id)
            if active is not None:
                raise StateConflictError("the case already has an unresolved operation")
            requester_actor = await StaffRepository(session).get_current_actor(
                approval.requester_staff_id
            )

        if as_utc(approval.expires_at) <= now:
            await self._expire_approval(
                actor, market, case_id, approval_id, request.expected_revision
            )
            raise StateConflictError("the approval has expired")

        problems = await self._approval_problems(
            market=market,
            case=case,
            snapshot=snapshot,
            materials=materials,
            requester_actor=requester_actor,
        )
        if problems:
            await self._invalidate_approval(
                actor,
                market,
                case_id,
                approval_id,
                request.expected_revision,
                reason=",".join(problems)[:128],
            )
            raise StateConflictError("the approval context changed")

        payload = operation_payload(snapshot, approval_id=approval_id)
        payload_json = canonical_json(payload)
        try:
            async with self._session_factory() as session, session.begin():
                repository = AfterSalesRepository(session)
                case = await repository.get_case(market, case_id)
                if case is None:
                    raise NotFoundError()
                approval = await repository.get_approval(case_id, approval_id)
                if approval is None or approval.status != "pending":
                    raise StateConflictError("the approval is no longer pending")
                if approval.requester_staff_id == actor.staff_id:
                    raise SelfApprovalForbiddenError()
                draft = await repository.get_draft_by_id(approval.draft_id)
                if draft is None or draft.status != "pending_approval":
                    raise StateConflictError("the draft is no longer awaiting approval")
                if not await repository.cas_case(
                    market=market,
                    case_id=case_id,
                    expected_revision=request.expected_revision,
                    values={"state": "waiting_approval"},
                ):
                    raise RevisionConflictError()
                await repository.update_approval(
                    approval,
                    status="approved",
                    decided_by_staff_id=actor.staff_id,
                    reason=None,
                    now=now,
                )
                await repository.update_draft_status(draft, "approved", now=now)
                operation = AfterSalesOperationRow(
                    operation_id=_new_id("op"),
                    case_id=case_id,
                    approval_id=approval_id,
                    draft_id=draft.draft_id,
                    idempotency_key=_new_id("dtc"),
                    payload_json=payload_json,
                    payload_sha256=sha256_text(payload_json),
                    status="prepared",
                    attempt=0,
                    dispatched_at=None,
                    lease_epoch=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(operation)
                await session.flush()
                await repository.append_event(
                    case_id=case_id,
                    event_type="approval_granted",
                    actor_staff_id=actor.staff_id,
                    old_state="waiting_approval",
                    new_state="waiting_approval",
                    entity_type="approval",
                    entity_id=approval_id,
                    reason="approved",
                )
                await repository.append_event(
                    case_id=case_id,
                    event_type="operation_prepared",
                    actor_staff_id=actor.staff_id,
                    old_state=None,
                    new_state=None,
                    entity_type="operation",
                    entity_id=operation.operation_id,
                    reason="awaiting_worker",
                )
        except IntegrityError as exc:
            raise StateConflictError("another operation was prepared concurrently") from exc
        except SQLAlchemyError as exc:
            raise DatabaseError("failed to persist the approved operation") from exc
        return await self.get_case_detail(market, case_id)

    async def reject(
        self,
        actor: StaffActor,
        market: str,
        case_id: int,
        approval_id: str,
        request: RejectRequest,
    ) -> CaseDetail:
        now = utcnow()
        async with self._session_factory() as session:
            repository = AfterSalesRepository(session)
            case = await repository.get_case(market, case_id)
            if case is None:
                raise NotFoundError()
            approval = await repository.get_approval(case_id, approval_id)
            if approval is None:
                raise NotFoundError()
            if approval.requester_staff_id == actor.staff_id:
                raise SelfApprovalForbiddenError()
            if approval.status != "pending":
                raise StateConflictError("the approval is no longer pending")
            if case.state != "waiting_approval":
                raise StateConflictError("the case state does not allow rejection")
        if approval is None or as_utc(approval.expires_at) <= now:
            await self._expire_approval(
                actor, market, case_id, approval_id, request.expected_revision
            )
            raise StateConflictError("the approval has expired")
        try:
            async with self._session_factory() as session, session.begin():
                repository = AfterSalesRepository(session)
                case = await repository.get_case(market, case_id)
                approval = await repository.get_approval(case_id, approval_id)
                if case is None or approval is None:
                    raise NotFoundError()
                if approval.status != "pending":
                    raise StateConflictError("the approval is no longer pending")
                if approval.requester_staff_id == actor.staff_id:
                    raise SelfApprovalForbiddenError()
                draft = await repository.get_draft_by_id(approval.draft_id)
                if not await repository.cas_case(
                    market=market,
                    case_id=case_id,
                    expected_revision=request.expected_revision,
                    values={"state": "processing"},
                ):
                    raise RevisionConflictError()
                await repository.update_approval(
                    approval,
                    status="rejected",
                    decided_by_staff_id=actor.staff_id,
                    reason=request.reason,
                    now=now,
                )
                if draft is not None and draft.status == "pending_approval":
                    await repository.update_draft_status(draft, "rejected", now=now)
                await repository.append_event(
                    case_id=case_id,
                    event_type="approval_rejected",
                    actor_staff_id=actor.staff_id,
                    old_state="waiting_approval",
                    new_state="processing",
                    entity_type="approval",
                    entity_id=approval_id,
                    reason="internal_rejection",
                )
        except IntegrityError as exc:
            raise StateConflictError("the approval changed concurrently") from exc
        except SQLAlchemyError as exc:
            raise DatabaseError("failed to persist the rejection") from exc
        return await self.get_case_detail(market, case_id)

    # --------------------------------------------------------- execution guard

    @staticmethod
    def _requester_problems(
        *,
        market: str,
        requester_snapshot: object,
        requester_actor: StaffActor | None,
    ) -> list[str]:
        if requester_actor is None or not isinstance(requester_snapshot, dict):
            return ["requester_authorization_changed"]
        if (
            sorted(requester_actor.roles) != requester_snapshot.get("roles")
            or sorted(requester_actor.allowed_markets) != requester_snapshot.get("allowed_markets")
            or not requester_actor.has_role("support")
            or not requester_actor.may_read_market(market)
        ):
            return ["requester_authorization_changed"]
        return []

    @staticmethod
    def _decider_problems(
        *,
        market: str,
        approval: AfterSalesApprovalRow,
        decider_actor: StaffActor | None,
    ) -> list[str]:
        if (
            decider_actor is None
            or approval.decided_by_staff_id is None
            or decider_actor.staff_id == approval.requester_staff_id
            or not decider_actor.has_role("approver")
            or not decider_actor.may_read_market(market)
        ):
            return ["decider_authorization_changed"]
        return []

    def final_execution_problems(
        self,
        *,
        market: str,
        approval: AfterSalesApprovalRow,
        snapshot: dict[str, Any],
        requester_actor: StaffActor | None,
        decider_actor: StaffActor | None,
        now: datetime,
    ) -> list[str]:
        """Local-only fence re-run inside the dispatch transaction.

        It deliberately performs no network reads, so it can run while the
        operation row is locked by the dispatching write. Any problem means
        the network validation that happened just before is stale.
        """
        problems: list[str] = []
        if approval.status not in {"approved", "executing"}:
            problems.append("approval_not_executable")
        if as_utc(approval.expires_at) <= now:
            problems.append("approval_expired")
        problems.extend(
            self._requester_problems(
                market=market,
                requester_snapshot=snapshot.get("requester"),
                requester_actor=requester_actor,
            )
        )
        problems.extend(
            self._decider_problems(
                market=market,
                approval=approval,
                decider_actor=decider_actor,
            )
        )
        return list(dict.fromkeys(problems))

    async def revalidate_execution_context(
        self,
        *,
        market: str,
        case: AfterSalesCaseRow,
        approval: AfterSalesApprovalRow,
        draft: AfterSalesDraftRow,
        snapshot: dict[str, Any],
        materials: list[AfterSalesMaterialRow],
        requester_actor: StaffActor | None,
        decider_actor: StaffActor | None,
        now: datetime,
    ) -> list[str]:
        """Re-run the 2b approval checks immediately before a dispatch.

        The immutable approval snapshot is authoritative for market/order
        identity. The current case is only compared against it; it never
        substitutes for the approved values. ``_approval_problems`` remains
        the single business-context validator.
        """
        problems: list[str] = []
        snapshot_case = snapshot.get("case")
        snapshot_order = snapshot.get("order")
        snapshot_draft = snapshot.get("draft")
        snapshot_evidence = snapshot.get("evidence")
        snapshot_requester = snapshot.get("requester")
        if not all(
            isinstance(item, dict)
            for item in (
                snapshot_case,
                snapshot_order,
                snapshot_draft,
                snapshot_evidence,
                snapshot_requester,
            )
        ):
            return ["snapshot_invalid"]
        case_snapshot = cast(dict[str, Any], snapshot_case)
        order_snapshot = cast(dict[str, Any], snapshot_order)
        draft_snapshot = cast(dict[str, Any], snapshot_draft)
        evidence_snapshot = cast(dict[str, Any], snapshot_evidence)
        requester_snapshot = cast(dict[str, Any], snapshot_requester)

        problems.extend(
            self.final_execution_problems(
                market=market,
                approval=approval,
                snapshot=snapshot,
                requester_actor=requester_actor,
                decider_actor=decider_actor,
                now=now,
            )
        )
        if approval.snapshot_sha256 != approval_snapshot_digest(snapshot):
            problems.append("snapshot_digest_mismatch")
        if approval.requester_staff_id != requester_snapshot.get("staff_id"):
            problems.append("requester_authorization_changed")

        identity_mismatch = (
            case_snapshot.get("case_id") != case.case_id
            or case_snapshot.get("market") != case.market
            or case_snapshot.get("request_id") != case.request_id
            or case_snapshot.get("order_id") != case.order_id
            or order_snapshot.get("order_id") != case_snapshot.get("order_id")
            or case_snapshot.get("market") != market
        )
        if identity_mismatch:
            problems.append("case_context_changed")
            return list(dict.fromkeys(problems))

        if (
            draft_snapshot.get("draft_id") != draft.draft_id
            or str(draft_snapshot.get("version")) != str(draft.version)
            or draft_snapshot.get("action") != draft.action
            or draft_snapshot.get("consumer_reply") != draft.consumer_reply
            or draft.status not in {"approved", "executing"}
        ):
            problems.append("draft_context_changed")
        if not evidence_snapshot.get("policy_version"):
            problems.append("policy_changed")

        problems.extend(
            await self._approval_problems(
                market=market,
                case=case,
                snapshot=snapshot,
                materials=materials,
                requester_actor=requester_actor,
            )
        )
        return list(dict.fromkeys(problems))

    # --------------------------------------------------------------- internal

    async def _order_or_none(self, market: str, order_id: str) -> ServiceOrder | None:
        try:
            order = await self._connector.get_service_order(market=market, order_id=order_id)
        except ConnectorNotFound:
            return None
        if order.market != market or order.order_id != order_id:
            raise ConnectorContractError("order identity does not match the case")
        return order

    async def _mark_needs_info(
        self,
        actor: StaffActor,
        market: str,
        case_id: int,
        expected_revision: int,
        reason: str,
    ) -> NoReturn:
        async with self._session_factory() as session, session.begin():
            repository = AfterSalesRepository(session)
            case = await repository.get_case(market, case_id)
            if case is None:
                raise NotFoundError()
            old_state = case.state
            values: dict[str, object] = (
                {"state": "needs_info"} if case.state in {"pending", "processing"} else {}
            )
            if values and not await repository.cas_case(
                market=market,
                case_id=case_id,
                expected_revision=expected_revision,
                values=values,
            ):
                raise RevisionConflictError()
            if not values:
                # Verify the revision still matches even when only the event is
                # appended, so a concurrent writer cannot interleave silently.
                if case.revision != expected_revision:
                    raise RevisionConflictError()
            await repository.append_event(
                case_id=case_id,
                event_type="case_needs_info",
                actor_staff_id=actor.staff_id,
                old_state=old_state,
                new_state="needs_info" if values else old_state,
                entity_type="case",
                entity_id=str(case_id),
                reason=reason,
            )
        raise StateConflictError(reason)

    async def _record_remote_ticket_unverified(
        self,
        market: str,
        case_id: int,
        expected_revision: int,
        *,
        remote: ServiceAfterSalesRequest,
        order: ServiceOrder | None,
    ) -> None:
        """Persist what E-pig reports without claiming a verified completion.

        The 2b slice has no receipt verifier. It may update the remote
        projection and flag the case for verification, but it must not retire
        an approval/draft/operation that may already have reached E-pig.
        """
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            repository = AfterSalesRepository(session)
            case = await repository.get_case(market, case_id)
            if case is None:
                raise NotFoundError()
            old_state = case.state
            values = _remote_projection(remote, case, order, now)
            if old_state not in {"completed", _REMOTE_VERIFICATION_STATE}:
                values["state"] = _REMOTE_VERIFICATION_STATE
            if values and not await repository.cas_case(
                market=market,
                case_id=case_id,
                expected_revision=expected_revision,
                values=values,
            ):
                raise RevisionConflictError()
            if not values and case.revision != expected_revision:
                raise RevisionConflictError()
            await session.refresh(case)
            await repository.append_event(
                case_id=case_id,
                event_type="remote_ticket_unverified",
                actor_staff_id=None,
                old_state=old_state,
                new_state=case.state,
                entity_type="case",
                entity_id=str(case_id),
                reason="remote_ticket_observed_without_receipt",
            )

    async def _expire_approval(
        self,
        actor: StaffActor,
        market: str,
        case_id: int,
        approval_id: str,
        expected_revision: int,
    ) -> None:
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            repository = AfterSalesRepository(session)
            case = await repository.get_case(market, case_id)
            approval = await repository.get_approval(case_id, approval_id)
            if case is None or approval is None:
                raise NotFoundError()
            if approval.status != "pending":
                return
            if not await repository.cas_case(
                market=market,
                case_id=case_id,
                expected_revision=expected_revision,
                values={"state": "processing" if case.state == "waiting_approval" else case.state},
            ):
                raise RevisionConflictError()
            await repository.update_approval(
                approval,
                status="expired",
                decided_by_staff_id=None,
                reason="approval_expired",
                now=now,
            )
            draft = await repository.get_draft_by_id(approval.draft_id)
            if draft is not None and draft.status == "pending_approval":
                await repository.update_draft_status(draft, "invalidated", now=now)
            await _fail_prepared_operation(repository, case_id, "approval_expired", now)
            await repository.append_event(
                case_id=case_id,
                event_type="approval_expired",
                actor_staff_id=actor.staff_id,
                old_state="waiting_approval",
                new_state="processing",
                entity_type="approval",
                entity_id=approval_id,
                reason="approval_expired",
            )

    async def _invalidate_approval(
        self,
        actor: StaffActor,
        market: str,
        case_id: int,
        approval_id: str,
        expected_revision: int,
        *,
        reason: str,
    ) -> None:
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            repository = AfterSalesRepository(session)
            case = await repository.get_case(market, case_id)
            approval = await repository.get_approval(case_id, approval_id)
            if case is None or approval is None:
                raise NotFoundError()
            if approval.status != "pending":
                return
            if not await repository.cas_case(
                market=market,
                case_id=case_id,
                expected_revision=expected_revision,
                values={"state": "processing" if case.state == "waiting_approval" else case.state},
            ):
                raise RevisionConflictError()
            await repository.update_approval(
                approval,
                status="invalidated",
                decided_by_staff_id=None,
                reason=reason,
                now=now,
            )
            draft = await repository.get_draft_by_id(approval.draft_id)
            if draft is not None and draft.status in {"pending_approval", "approved"}:
                await repository.update_draft_status(draft, "invalidated", now=now)
            await _fail_prepared_operation(repository, case_id, reason, now)
            await repository.append_event(
                case_id=case_id,
                event_type="approval_invalidated",
                actor_staff_id=actor.staff_id,
                old_state="waiting_approval",
                new_state="processing",
                entity_type="approval",
                entity_id=approval_id,
                reason=reason,
            )

    async def _approval_problems(
        self,
        *,
        market: str,
        case: AfterSalesCaseRow,
        snapshot: dict[str, Any],
        materials: list[AfterSalesMaterialRow],
        requester_actor: StaffActor | None,
    ) -> list[str]:
        problems: list[str] = []
        try:
            order = await self._connector.get_service_order(market=market, order_id=case.order_id)
        except ConnectorNotFound:
            order = None
        if order is None or order.market != market or order.order_id != case.order_id:
            problems.append("order_unavailable")
            order = None
        else:
            if order.order_version != snapshot["order"]["order_version"]:
                problems.append("order_version_changed")
            if _order_fingerprint(order) != _order_fingerprint(
                ServiceOrder.model_validate(snapshot["order"])
            ):
                problems.append("order_content_changed")

        try:
            remote = await self._connector.get_service_request(
                market=market, request_id=case.request_id
            )
        except ConnectorNotFound:
            remote = None
        if remote is None:
            problems.append("request_unavailable")
        else:
            expected = snapshot["request"]
            if (
                remote.status != expected["status"]
                or remote.ticket_id != expected["ticket_id"]
                or remote.updated_at != _parse_iso(expected["updated_at"])
                or remote.type != expected["type"]
                or sha256_text(remote.description) != expected["description_sha256"]
            ):
                problems.append("request_changed")
            if _attachments_fingerprint(remote.attachments) != expected["attachments"]:
                problems.append("attachments_changed")
            if _attachments_expired(remote.attachments, utcnow()):
                problems.append("attachment_expired")

        material_items = [_material_snapshot(item) for item in materials]
        if snapshot["materials"]["revision"] != case.material_revision:
            problems.append("materials_changed")
        if _canonical(material_items) != _canonical(snapshot["materials"]["items"]):
            problems.append("materials_changed")

        action = snapshot["draft"]["action"]
        if _ACTION_EVIDENCE_TOPICS.get(action) and not (
            _evidence_topics(snapshot["evidence"]["chunks"]) & _ACTION_EVIDENCE_TOPICS[action]
        ):
            problems.append("action_policy_mismatch")
        if action in {"reject", "other"}:
            if not (_evidence_topics(snapshot["evidence"]["chunks"]) & _REVIEW_EVIDENCE_TOPICS):
                problems.append("action_policy_mismatch")
            if not _manual_verification_items(materials):
                problems.append("manual_verification_missing")
        if (
            action in {"return", "reship"}
            and remote is not None
            and not _has_condition_evidence(remote, materials, utcnow())
        ):
            problems.append("condition_evidence_changed")

        if action == "return":
            shipment: ServiceShipment | None = None
            if order is not None:
                try:
                    shipment = await self._connector.get_service_order_shipment(
                        market=market, order_id=case.order_id
                    )
                except ConnectorNotFound:
                    shipment = None
                if shipment is not None and shipment.order_id != case.order_id:
                    shipment = None
            current_proof, _ = _delivery_proof(
                order=order, materials=materials, shipment=shipment, now=utcnow()
            )
            if snapshot.get("delivery_proof") is None:
                problems.append("delivery_proof_missing")
            elif current_proof is None:
                problems.append("delivery_proof_changed")
            elif _canonical(current_proof) != _canonical(snapshot["delivery_proof"]):
                problems.append("delivery_proof_changed")

        problems.extend(
            self._requester_problems(
                market=market,
                requester_snapshot=snapshot.get("requester"),
                requester_actor=requester_actor,
            )
        )

        if order is not None:
            categories, missing = _trusted_categories(order, self._categories)
            if missing or _canonical([item.as_dict() for item in categories]) != _canonical(
                snapshot["trusted_categories"]
            ):
                problems.append("sku_category_changed")
            else:
                try:
                    evidence = self._evidence.resolve(
                        market=market,
                        chunk_ids=[item["chunk_id"] for item in snapshot["evidence"]["chunks"]],
                        trusted_categories={item.category for item in categories},
                        require_category=True,
                        as_of=utcnow().date(),
                    )
                except (InvalidRequestError, StateConflictError):
                    problems.append("policy_changed")
                else:
                    if evidence.evidence_sha256 != snapshot["evidence"]["evidence_sha256"]:
                        problems.append("policy_changed")
                    if evidence.policy_version != snapshot["evidence"]["policy_version"]:
                        problems.append("policy_changed")
        return list(dict.fromkeys(problems))


# --------------------------------------------------------------------- helpers


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def _attachment_count(raw: str) -> int:
    """Queue views expose counts only, never consumer free text or file names."""
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return 0
    return len(parsed) if isinstance(parsed, list) else 0


def _parse_watermark(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConnectorContractError("feed high watermark is not numeric") from exc


def _attachments_json(attachments: Iterable[Attachment]) -> str:
    return canonical_json([item.model_dump(mode="json") for item in attachments])


def _order_json(order: ServiceOrder | None) -> str | None:
    return order.model_dump_json(exclude_none=True) if order is not None else None


def _new_case(
    market: str,
    item: ServiceAfterSalesRequest,
    order: ServiceOrder | None,
    now: datetime,
) -> AfterSalesCaseRow:
    state = _REMOTE_VERIFICATION_STATE if item.status == "ticket_created" else "processing"
    return AfterSalesCaseRow(
        market=market,
        request_id=item.request_id,
        order_id=item.order_id,
        request_type=item.type,
        description=item.description,
        state=state,
        revision=1,
        material_revision=0,
        current_draft_id=None,
        remote_status=item.status,
        remote_ticket_id=item.ticket_id,
        remote_sequence=item.sequence,
        remote_updated_at=item.updated_at,
        request_created_at=item.created_at,
        request_updated_at=item.updated_at,
        attachments_json=_attachments_json(item.attachments),
        order_snapshot_json=_order_json(order),
        order_version=order.order_version if order is not None else None,
        order_queried_at=now if order is not None else None,
        created_at=now,
        updated_at=now,
    )


def _is_stale_remote(case: AfterSalesCaseRow, item: ServiceAfterSalesRequest) -> bool:
    """Reject older pages and status regression; never unset a ticket id."""
    incoming_sequence = int(item.sequence)
    stored_sequence = int(case.remote_sequence)
    if incoming_sequence < stored_sequence:
        return True
    if incoming_sequence == stored_sequence and item.updated_at < _as_utc(case.remote_updated_at):
        return True
    return case.remote_status == "ticket_created" and item.status == "pending"


def _remote_projection(
    item: ServiceAfterSalesRequest,
    case: AfterSalesCaseRow,
    order: ServiceOrder | None,
    now: datetime,
) -> dict[str, object]:
    values: dict[str, object] = {
        "remote_status": item.status,
        "remote_ticket_id": case.remote_ticket_id or item.ticket_id,
        "remote_sequence": item.sequence,
        "remote_updated_at": item.updated_at,
        "request_updated_at": item.updated_at,
        "attachments_json": _attachments_json(item.attachments),
        "description": item.description,
        "request_type": item.type,
        "order_id": item.order_id,
    }
    changed = (
        any(
            getattr(case, key) != value
            for key, value in values.items()
            if key not in {"remote_updated_at", "request_updated_at"}
        )
        or _as_utc(case.remote_updated_at) != item.updated_at
    )
    if order is not None:
        if case.order_version != order.order_version:
            values["order_snapshot_json"] = _order_json(order)
            values["order_version"] = order.order_version
            values["order_queried_at"] = now
            changed = True
    return values if changed else {}


def _as_utc(value: datetime) -> datetime:
    try:
        return as_utc(value)
    except (AttributeError, ValueError):
        return value


async def _invalidate_context(
    repository: AfterSalesRepository,
    case: AfterSalesCaseRow,
    *,
    actor_staff_id: str | None,
    reason: str,
    now: datetime,
    supersede_draft: AfterSalesDraftRow | None = None,
) -> None:
    """Invalidate pending/approved context and fail only never-dispatched work."""
    approval = await repository.latest_unresolved_approval(case.case_id)
    if approval is not None:
        await repository.update_approval(
            approval,
            status="invalidated",
            decided_by_staff_id=actor_staff_id,
            reason=reason[:128],
            now=now,
        )
        draft = await repository.get_draft_by_id(approval.draft_id)
        if draft is not None and draft.status in {"pending_approval", "approved"}:
            await repository.update_draft_status(draft, "invalidated", now=now)
    if supersede_draft is not None and supersede_draft.status in {
        "editing",
        "pending_approval",
        "approved",
        "rejected",
    }:
        await repository.update_draft_status(supersede_draft, "superseded", now=now)
    await _fail_prepared_operation(repository, case.case_id, reason, now)


async def _fail_prepared_operation(
    repository: AfterSalesRepository, case_id: int, reason: str, now: datetime
) -> None:
    operation = await repository.prepared_operation(case_id)
    if operation is not None:
        await repository.fail_prepared_operation(operation, reason=reason[:128], now=now)


def _is_blocking_operation(operation: AfterSalesOperationRow | None) -> bool:
    if operation is None:
        return False
    return not (operation.status == "prepared" and operation.dispatched_at is None)


def _ensure_writable(case: AfterSalesCaseRow) -> None:
    if case.state == "completed":
        raise StateConflictError("the case is completed")
    if case.state in {"executing", "reconciliation_required"}:
        raise StateConflictError("the case is executing or awaiting reconciliation")


def _ensure_draft_creatable(case: AfterSalesCaseRow) -> None:
    """Drafts may be saved for audit during an unknown result, never after completion."""
    if case.state == "completed":
        raise StateConflictError("the case is completed")


def _blocks_new_draft(case: AfterSalesCaseRow, operation: AfterSalesOperationRow | None) -> bool:
    """Treat an executing/unknown case as a candidate-only context."""
    return case.state in {"executing", _REMOTE_VERIFICATION_STATE} or _is_blocking_operation(
        operation
    )


def _trusted_categories(
    order: ServiceOrder | None, resolver: CategoryResolver
) -> tuple[list[TrustedCategory], list[str]]:
    if order is None:
        return [], []
    categories: list[TrustedCategory] = []
    missing: list[str] = []
    seen: set[str] = set()
    for item in order.items:
        if item.sku in seen:
            continue
        seen.add(item.sku)
        resolved = resolver.resolve(item.sku)
        if resolved is None:
            missing.append(item.sku)
        else:
            categories.append(resolved)
    return categories, missing


def _draft_chunk_ids(draft: AfterSalesDraftRow) -> list[str]:
    try:
        evidence = json.loads(draft.evidence_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(evidence, list):
        return []
    return [
        str(item.get("chunk_id"))
        for item in evidence
        if isinstance(item, dict) and item.get("chunk_id")
    ]


def _attachments_expired(attachments: Iterable[Attachment], now: datetime) -> bool:
    return any(item.status == "expired" or as_utc(item.expires_at) <= now for item in attachments)


def _attachments_fingerprint(attachments: Iterable[Attachment]) -> list[dict[str, Any]]:
    return [
        {
            "attachment_id": item.attachment_id,
            "status": item.status,
            "media_type": item.media_type,
            "size_bytes": item.size_bytes,
            "expires_at": item.expires_at.isoformat(),
        }
        for item in attachments
    ]


def _order_fingerprint(order: ServiceOrder) -> str:
    return sha256_json(
        {
            "order_id": order.order_id,
            "order_version": order.order_version,
            "status": order.status,
            "currency": order.currency,
            "items": [
                {
                    "order_item_id": item.order_item_id,
                    "sku": item.sku,
                    "quantity": item.quantity,
                    "unit_price_minor": item.unit_price_minor,
                    "line_total_minor": item.line_total_minor,
                }
                for item in order.items
            ],
            "subtotal_minor": order.subtotal_minor,
            "shipping_minor": order.shipping_minor,
            "tax_minor": order.tax_minor,
            "discount_minor": order.discount_minor,
            "total_minor": order.total_minor,
        }
    )


def _material_snapshot(item: AfterSalesMaterialRow) -> dict[str, Any]:
    return {
        "material_id": item.material_id,
        "material_type": item.material_type,
        "source": item.source,
        "content": item.content,
        "content_sha256": sha256_text(item.content),
        "applies_to_sku": item.applies_to_sku,
        "delivered_at": (
            _as_utc(item.delivered_at).isoformat() if item.delivered_at is not None else None
        ),
        "created_by_staff_id": item.created_by_staff_id,
        "created_at": as_utc(item.created_at).isoformat(),
    }


def _evidence_topics(chunks: Iterable[dict[str, Any]]) -> set[str]:
    """Derive the policy document topic from trusted, server-built evidence."""
    topics: set[str] = set()
    for chunk in chunks:
        source_path = chunk.get("source_path")
        if isinstance(source_path, str) and source_path:
            name = source_path.rsplit("/", 1)[-1]
            if name.endswith(".md"):
                name = name[:-3]
            topics.add(name)
            continue
        document_id = str(chunk.get("document_id") or "")
        if document_id:
            topics.add(document_id.rsplit("_", 1)[0])
    return topics


def _manual_verification_items(
    materials: Iterable[AfterSalesMaterialRow],
) -> list[AfterSalesMaterialRow]:
    return [
        item
        for item in materials
        if item.material_type == "manual_verification"
        and item.source.strip()
        and item.content.strip()
    ]


def _delivery_proof(
    *,
    order: ServiceOrder | None,
    materials: Iterable[AfterSalesMaterialRow],
    shipment: ServiceShipment | None,
    now: datetime,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return delivery proof for return actions, never free-text assertions.

    A shipment ``delivered`` status alone is not proof: there must be a real
    delivered event with an actual, non-future timestamp. Otherwise only a
    sourced, SKU-linked manual/delivery verification with an actual past
    delivery date qualifies; ``internal_note`` and consumer self-report records
    are ignored. The returned proof is limited to the relevant delivery fact so
    unrelated logistics notes do not change approval revalidation.
    """
    future_delivery = False
    if shipment is not None:
        delivered_events = [
            event
            for event in shipment.events
            if event.status == "delivered" and as_utc(event.occurred_at) <= now
        ]
        future_delivery = any(
            event.status == "delivered" and as_utc(event.occurred_at) > now
            for event in shipment.events
        )
        if delivered_events:
            latest = max(delivered_events, key=lambda event: event.occurred_at)
            return (
                {
                    "kind": "shipment",
                    "shipment_id": shipment.shipment_id,
                    "event_id": latest.event_id,
                    "delivered_at": as_utc(latest.occurred_at).isoformat(),
                },
                None,
            )

    if order is None:
        return None, "missing_delivery_proof"
    skus = {item.sku for item in order.items}
    structured = [
        item
        for item in materials
        if item.material_type in _STRUCTURED_DELIVERY_MATERIALS
        and (item.applies_to_sku is not None or item.delivered_at is not None)
    ]
    candidates: list[tuple[datetime, AfterSalesMaterialRow]] = []
    for item in structured:
        if not item.source.strip() or not item.content.strip():
            continue
        if item.applies_to_sku not in skus or item.delivered_at is None:
            continue
        delivered_at = _as_utc(item.delivered_at)
        if delivered_at > now:
            future_delivery = True
            continue
        candidates.append((delivered_at, item))
    if candidates:
        delivered_at, item = max(candidates, key=lambda pair: pair[0])
        return (
            {
                "kind": "manual_verification",
                "material_id": item.material_id,
                "material_type": item.material_type,
                "applies_to_sku": item.applies_to_sku,
                "delivered_at": delivered_at.isoformat(),
                "source_description": item.source,
                "content_sha256": sha256_text(item.content),
                "recorded_by_staff_id": item.created_by_staff_id,
            },
            None,
        )
    if future_delivery:
        return None, "delivery_proof_in_future"
    if structured:
        return None, "delivery_proof_not_applicable"
    return None, "missing_delivery_proof"


def _has_condition_evidence(
    remote: ServiceAfterSalesRequest,
    materials: Iterable[AfterSalesMaterialRow],
    now: datetime,
) -> bool:
    """Photos are a human-recorded fact, never something this code judges."""
    active_attachment = any(
        item.status == "active" and as_utc(item.expires_at) > now for item in remote.attachments
    )
    return active_attachment or bool(_manual_verification_items(materials))


def _evaluate_action_guard(
    *,
    action: str,
    evidence: EvidenceResult,
    materials: list[AfterSalesMaterialRow],
    order: ServiceOrder | None,
    remote: ServiceAfterSalesRequest,
    shipment: ServiceShipment | None,
    now: datetime,
) -> tuple[list[str], dict[str, Any] | None]:
    problems: list[str] = []
    topics = _evidence_topics(evidence.chunks)
    required = _ACTION_EVIDENCE_TOPICS.get(action)
    if required is not None and not topics & required:
        problems.append("missing_action_policy")
    if action in {"reject", "other"}:
        if not topics & _REVIEW_EVIDENCE_TOPICS:
            problems.append("missing_review_policy")
        if not _manual_verification_items(materials):
            problems.append("missing_manual_verification")
    if action in {"return", "reship"} and not _has_condition_evidence(remote, materials, now):
        problems.append("missing_condition_evidence")

    delivery_proof: dict[str, Any] | None = None
    if action == "return":
        delivery_proof, problem = _delivery_proof(
            order=order,
            materials=materials,
            shipment=shipment,
            now=now,
        )
        if problem is not None:
            problems.append(problem)
    return problems, delivery_proof


def _build_snapshot(
    *,
    case: AfterSalesCaseRow,
    draft: AfterSalesDraftRow,
    order: ServiceOrder,
    remote: ServiceAfterSalesRequest,
    shipment: ServiceShipment | None,
    delivery_proof: dict[str, Any] | None,
    materials: list[AfterSalesMaterialRow],
    material_revision: int,
    evidence: EvidenceResult,
    categories: list[TrustedCategory],
    requester: StaffActor,
    expires_at: datetime,
    now: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "case": {
            "case_id": case.case_id,
            "market": case.market,
            "request_id": case.request_id,
            "order_id": case.order_id,
            "request_type": case.request_type,
        },
        "order": order.model_dump(mode="json", exclude_none=True),
        "request": {
            "request_id": remote.request_id,
            "sequence": remote.sequence,
            "status": remote.status,
            "ticket_id": remote.ticket_id,
            "type": remote.type,
            "description_sha256": sha256_text(remote.description),
            "attachments": _attachments_fingerprint(remote.attachments),
            "updated_at": remote.updated_at.isoformat(),
        },
        "shipment": (
            shipment.model_dump(mode="json", exclude_none=True) if shipment is not None else None
        ),
        "delivery_proof": delivery_proof,
        "materials": {
            "revision": material_revision,
            "items": [_material_snapshot(item) for item in materials],
        },
        "draft": {
            "draft_id": draft.draft_id,
            "version": draft.version,
            "action": draft.action,
            "internal_summary": draft.internal_summary,
            "consumer_reply": draft.consumer_reply,
            "evidence_sha256": evidence.evidence_sha256,
        },
        "evidence": evidence.as_snapshot(),
        "trusted_categories": [item.as_dict() for item in categories],
        "requester": {
            "staff_id": requester.staff_id,
            "roles": sorted(requester.roles),
            "allowed_markets": sorted(requester.allowed_markets),
        },
        "approval": {
            "requested_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        },
    }


def _load_snapshot(approval: AfterSalesApprovalRow) -> dict[str, Any]:
    try:
        snapshot = json.loads(approval.snapshot_json)
    except json.JSONDecodeError as exc:
        raise DatabaseError("stored approval snapshot is invalid") from exc
    if not isinstance(snapshot, dict):
        raise DatabaseError("stored approval snapshot is invalid")
    return snapshot


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _canonical(value: Any) -> str:
    return canonical_json(value)


# -------------------------------------------------------------------- views


def _detail(
    case: AfterSalesCaseRow,
    materials: list[AfterSalesMaterialRow],
    drafts: list[AfterSalesDraftRow],
    approvals: list[AfterSalesApprovalRow],
    operations: list[AfterSalesOperationRow],
) -> CaseDetail:
    order: ServiceOrder | None = None
    if case.order_snapshot_json:
        try:
            order = ServiceOrder.model_validate_json(case.order_snapshot_json)
        except ValueError:
            order = None
    attachments = _load_attachments(case.attachments_json)
    current = next((item for item in drafts if item.draft_id == case.current_draft_id), None)
    return CaseDetail(
        case_id=case.case_id,
        market=cast(Any, case.market),
        request_id=case.request_id,
        order_id=case.order_id,
        request_type=cast(Any, case.request_type),
        description=case.description,
        state=cast(CaseState, case.state),
        revision=case.revision,
        material_revision=case.material_revision,
        remote_status=cast(Any, case.remote_status),
        remote_ticket_id=case.remote_ticket_id,
        request_created_at=as_utc(case.request_created_at),
        request_updated_at=as_utc(case.request_updated_at),
        attachments=attachments,
        order=order,
        order_queried_at=(
            as_utc(case.order_queried_at) if case.order_queried_at is not None else None
        ),
        current_draft=_draft_view(current) if current is not None else None,
        drafts=[_draft_view(item) for item in drafts],
        materials=[
            MaterialView(
                material_id=item.material_id,
                material_type=cast(Any, item.material_type),
                source=item.source,
                content=item.content,
                applies_to_sku=item.applies_to_sku,
                delivered_at=(as_utc(item.delivered_at) if item.delivered_at is not None else None),
                created_by_staff_id=item.created_by_staff_id,
                created_at=as_utc(item.created_at),
            )
            for item in materials
        ],
        approvals=[
            ApprovalView(
                approval_id=item.approval_id,
                draft_id=item.draft_id,
                status=cast(Any, item.status),
                requester_staff_id=item.requester_staff_id,
                decided_by_staff_id=item.decided_by_staff_id,
                expires_at=as_utc(item.expires_at),
                snapshot_sha256=item.snapshot_sha256,
                decision_reason=item.decision_reason,
                created_at=as_utc(item.created_at),
                decided_at=as_utc(item.decided_at) if item.decided_at is not None else None,
            )
            for item in approvals
        ],
        operations=[_operation_view(item) for item in operations],
        created_at=as_utc(case.created_at),
        updated_at=as_utc(case.updated_at),
    )


def _load_attachments(raw: str) -> list[AttachmentView]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    views: list[AttachmentView] = []
    for item in parsed:
        try:
            attachment = Attachment.model_validate(item)
        except ValueError:
            continue
        views.append(
            AttachmentView(
                attachment_id=attachment.attachment_id,
                file_name=attachment.file_name,
                media_type=attachment.media_type,
                size_bytes=attachment.size_bytes,
                status=attachment.status,
                created_at=as_utc(attachment.created_at),
                expires_at=as_utc(attachment.expires_at),
            )
        )
    return views


def _draft_view(draft: AfterSalesDraftRow) -> DraftView:
    try:
        raw_evidence = json.loads(draft.evidence_json)
    except json.JSONDecodeError:
        raw_evidence = []
    evidence = [
        EvidenceView(
            chunk_id=str(item.get("chunk_id", "")),
            document_id=str(item.get("document_id", "")),
            title=str(item.get("title", "")),
            section=item.get("section"),
            content=str(item.get("content", "")),
            policy_version=str(item.get("policy_version", "")),
            source_path=str(item.get("source_path", "")),
            source_label=str(item.get("source_label", "")),
            source_type=str(item.get("source_type", "")),
            policy_status=str(item.get("policy_status", "")),
            is_demo=bool(item.get("is_demo", False)),
            retrieved_at=item.get("retrieved_at"),
            effective_from=item.get("effective_from"),
            effective_to=item.get("effective_to"),
            content_sha256=str(item.get("content_sha256", "")),
        )
        for item in raw_evidence
        if isinstance(item, dict)
    ]
    return DraftView(
        draft_id=draft.draft_id,
        version=draft.version,
        action=cast(TicketAction, draft.action),
        internal_summary=draft.internal_summary,
        consumer_reply=draft.consumer_reply,
        evidence=evidence,
        evidence_sha256=draft.evidence_sha256,
        policy_version=draft.policy_version,
        order_version=draft.order_version,
        material_revision=draft.material_revision,
        status=cast(Any, draft.status),
        author_staff_id=draft.author_staff_id,
        created_at=as_utc(draft.created_at),
        submitted_at=as_utc(draft.submitted_at) if draft.submitted_at is not None else None,
        decided_at=as_utc(draft.decided_at) if draft.decided_at is not None else None,
    )


def _operation_view(operation: AfterSalesOperationRow) -> OperationView:
    try:
        payload = json.loads(operation.payload_json)
    except json.JSONDecodeError:
        payload = {}
    receipt = None
    if operation.receipt_json:
        try:
            receipt = json.loads(operation.receipt_json)
        except json.JSONDecodeError:
            receipt = None
    return OperationView(
        operation_id=operation.operation_id,
        approval_id=operation.approval_id,
        draft_id=operation.draft_id,
        idempotency_key=operation.idempotency_key,
        status=cast(Any, operation.status),
        attempt=operation.attempt,
        payload=payload if isinstance(payload, dict) else {},
        payload_sha256=operation.payload_sha256,
        dispatched_at=(
            as_utc(operation.dispatched_at) if operation.dispatched_at is not None else None
        ),
        lease_owner=operation.lease_owner,
        lease_epoch=operation.lease_epoch,
        lease_until=(as_utc(operation.lease_until) if operation.lease_until is not None else None),
        next_attempt_at=(
            as_utc(operation.next_attempt_at) if operation.next_attempt_at is not None else None
        ),
        receipt=receipt if isinstance(receipt, dict) else None,
        last_error_code=operation.last_error_code,
        failure_reason=operation.failure_reason,
        lookup_count=operation.lookup_count,
        reconciliation_state=operation.reconciliation_state,
        reconcile_started_at=(
            as_utc(operation.reconcile_started_at)
            if operation.reconcile_started_at is not None
            else None
        ),
        created_at=as_utc(operation.created_at),
        updated_at=as_utc(operation.updated_at),
    )


def _event_view(event: AfterSalesEventRow) -> EventView:
    return EventView(
        seq=event.seq,
        event_type=event.event_type,
        actor_staff_id=event.actor_staff_id,
        old_state=event.old_state,
        new_state=event.new_state,
        entity_type=event.entity_type,
        entity_id=event.entity_id,
        reason=event.reason,
        created_at=as_utc(event.created_at),
    )


def _sync_event_reason(base: str | None, *, service_poll: bool) -> str | None:
    """Keep the audit reason explicit without changing staff-triggered reasons."""
    if not service_poll:
        return base
    return f"service_poll:{base}"[:128] if base else "service_poll"
