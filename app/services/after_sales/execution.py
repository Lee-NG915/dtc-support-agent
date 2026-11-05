"""DTC-2c execution worker: leases, one-shot dispatch and receipt recovery.

The approval service persists a stable operation. This module is the only
place that may call ``create_ticket``: it claims one operation through an
atomic owner/epoch lease, re-runs the 2b checks immediately before the first
POST, and always verifies the ticket by ID before completing the case.

Unknown outcomes are never converted into "not sent". They are reconciled by
the original idempotency key, and only a contract-safe same-key retry may send
the original payload again. All state transitions are fenced by owner + epoch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.connectors.base import BusinessConnector
from app.connectors.errors import (
    ConnectorError,
    ConnectorPermissionError,
    ConnectorValidationError,
)
from app.connectors.models import (
    CreateServiceTicketInput,
    ServiceTicket,
    TicketLookupFound,
    TicketLookupInFlight,
    TicketLookupNotVisible,
    TicketLookupResult,
    dump_wire_model,
)
from app.core.exceptions import DatabaseError, NotFoundError, StateConflictError
from app.core.time import as_utc, utcnow
from app.repositories.after_sales_repository import AfterSalesRepository
from app.repositories.models import (
    ACTIVE_OPERATION_STATUSES,
    AfterSalesApprovalRow,
    AfterSalesCaseRow,
    AfterSalesDraftRow,
    AfterSalesEventRow,
    AfterSalesMaterialRow,
    AfterSalesOperationRow,
)
from app.repositories.staff_repository import StaffRepository
from app.schemas.after_sales import AFTER_SALES_MARKETS, StaffActor
from app.schemas.after_sales_worker import (
    WorkerOperationDetail,
    WorkerOperationEvent,
    WorkerOperationListResponse,
    WorkerOperationSummary,
)
from app.services.after_sales.payload import (
    approval_snapshot_digest,
    canonical_json,
    operation_payload,
    sha256_text,
)
from app.services.after_sales.service import AfterSalesWorkflow

logger = logging.getLogger(__name__)

_UNKNOWN_STATUSES = ("dispatching", "awaiting_receipt", "reconciliation_required")
_MANUAL_REVIEW = "manual_review"
_OUTCOME_DISPATCHED = "dispatched"
_OUTCOME_SUCCEEDED = "succeeded"
_OUTCOME_RETRY = "retry_wait"
_OUTCOME_RECONCILING = "reconciling"
_OUTCOME_FAILED = "failed"
_OUTCOME_MANUAL = "manual_review"
_OUTCOME_DEFERRED = "deferred"
_OUTCOME_SKIPPED = "skipped"


class _LeaseLost(Exception):
    """Another owner/epoch already won; never write a late result."""


class _CaseRace(Exception):
    """A concurrent case writer changed the revision; retry the fence."""


class _CorruptOperation(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ConsumeConflict(Exception):
    """The original approval/draft could not be consumed atomically."""


@dataclass(frozen=True)
class RunReport:
    claimed: int = 0
    dispatched: int = 0
    succeeded: int = 0
    retry_wait: int = 0
    reconciling: int = 0
    failed: int = 0
    manual_review: int = 0
    deferred: int = 0
    skipped: int = 0
    # Service feed polls run before operation processing. A failed market is
    # reported by its safe code and never stops the other markets.
    polled: int = 0
    poll_failed: int = 0
    poll_error_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Claim:
    operation_id: str
    case_id: int
    market: str
    owner: str
    epoch: int
    status: str


@dataclass
class _Context:
    operation: AfterSalesOperationRow
    case: AfterSalesCaseRow
    approval: AfterSalesApprovalRow
    draft: AfterSalesDraftRow
    snapshot: dict[str, Any]
    payload: dict[str, Any]
    materials: list[AfterSalesMaterialRow]
    requester: StaffActor | None
    decider: StaffActor | None


class AfterSalesExecution:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        connector: BusinessConnector,
        workflow: AfterSalesWorkflow,
        owner: str | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._connector = connector
        self._workflow = workflow
        self._owner = (owner or f"worker_{secrets.token_hex(8)}")[:128]

    @property
    def owner(self) -> str:
        return self._owner

    # ------------------------------------------------------------- one cycle

    async def run_once(self, markets: list[str] | tuple[str, ...] | set[str]) -> RunReport:
        selected = sorted({market for market in markets})
        if not selected:
            return RunReport()
        invalid = [market for market in selected if market not in AFTER_SALES_MARKETS]
        if invalid:
            raise ValueError(f"unsupported worker market: {invalid[0]}")
        now = utcnow()
        claims = await self._claim_due(selected, now)
        counts = {
            _OUTCOME_DISPATCHED: 0,
            _OUTCOME_SUCCEEDED: 0,
            _OUTCOME_RETRY: 0,
            _OUTCOME_RECONCILING: 0,
            _OUTCOME_FAILED: 0,
            _OUTCOME_MANUAL: 0,
            _OUTCOME_DEFERRED: 0,
            _OUTCOME_SKIPPED: 0,
        }
        for claim in claims:
            outcome = _OUTCOME_SKIPPED
            try:
                async with asyncio.timeout(
                    self._settings.after_sales_worker_operation_timeout_seconds
                ):
                    outcome = await self._process(claim)
            except TimeoutError:
                outcome = await self._handle_interrupted(claim, "OPERATION_TIMEOUT")
            except _CorruptOperation as exc:
                outcome = await self._mark_manual(claim, exc.code, "stored_operation_invalid")
            except (SQLAlchemyError, DatabaseError):
                logger.warning(
                    "after_sales worker persistence error operation_id=%s", claim.operation_id
                )
                outcome = await self._handle_interrupted(claim, "LOCAL_COMMIT_FAILED")
            except ConnectorError as exc:
                logger.warning(
                    "after_sales worker connector error operation_id=%s code=%s",
                    claim.operation_id,
                    exc.code,
                )
                outcome = await self._handle_interrupted(claim, exc.code)
            except Exception as exc:  # noqa: BLE001 - one bad operation must not stop the batch
                logger.error(
                    "after_sales worker unexpected error operation_id=%s error=%s",
                    claim.operation_id,
                    type(exc).__name__,
                )
                outcome = await self._handle_interrupted(claim, "WORKER_ERROR")
            counts[outcome] = counts.get(outcome, 0) + 1
        return RunReport(
            claimed=len(claims),
            dispatched=counts[_OUTCOME_DISPATCHED],
            succeeded=counts[_OUTCOME_SUCCEEDED],
            retry_wait=counts[_OUTCOME_RETRY],
            reconciling=counts[_OUTCOME_RECONCILING],
            failed=counts[_OUTCOME_FAILED],
            manual_review=counts[_OUTCOME_MANUAL],
            deferred=counts[_OUTCOME_DEFERRED],
            skipped=counts[_OUTCOME_SKIPPED],
        )

    async def _claim_due(self, markets: list[str], now: datetime) -> list[_Claim]:
        lease_until = now + timedelta(seconds=self._settings.after_sales_worker_lease_seconds)
        async with self._session_factory() as session, session.begin():
            statement = (
                select(
                    AfterSalesOperationRow.operation_id,
                    AfterSalesOperationRow.case_id,
                    AfterSalesOperationRow.lease_epoch,
                    AfterSalesOperationRow.status,
                    AfterSalesOperationRow.unknown_attempts,
                    AfterSalesCaseRow.market,
                )
                .join(
                    AfterSalesCaseRow, AfterSalesCaseRow.case_id == AfterSalesOperationRow.case_id
                )
                .where(
                    AfterSalesCaseRow.market.in_(markets),
                    AfterSalesCaseRow.state.not_in(("completed", "failed")),
                    self._auto_claimable(now),
                )
                .order_by(
                    AfterSalesOperationRow.created_at,
                    AfterSalesOperationRow.operation_id,
                )
                .limit(self._settings.after_sales_worker_batch_limit)
            )
            rows = list((await session.execute(statement)).all())
            claimed: list[_Claim] = []
            for operation_id, case_id, epoch, status, unknown_attempts, market in rows:
                values: dict[str, Any] = {
                    "lease_owner": self._owner,
                    "lease_epoch": epoch + 1,
                    "lease_until": lease_until,
                    "updated_at": now,
                }
                if status in _UNKNOWN_STATUSES:
                    values["reconciliation_state"] = "checking"
                    if unknown_attempts < 1:
                        # A dispatching/unknown row is itself evidence that an
                        # earlier effect was never disproven.
                        values["unknown_attempts"] = 1
                result = await session.execute(
                    update(AfterSalesOperationRow)
                    .where(
                        AfterSalesOperationRow.operation_id == operation_id,
                        AfterSalesOperationRow.lease_epoch == epoch,
                        self._auto_claimable(now),
                    )
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
                if getattr(result, "rowcount", 0) == 1:
                    claimed.append(
                        _Claim(
                            operation_id=operation_id,
                            case_id=case_id,
                            market=market,
                            owner=self._owner,
                            epoch=epoch + 1,
                            status=status,
                        )
                    )
        return claimed

    def _auto_claimable(self, now: datetime) -> Any:
        operation = AfterSalesOperationRow
        lease_free = or_(operation.lease_until.is_(None), operation.lease_until <= now)
        prepared = and_(
            operation.status == "prepared",
            operation.dispatched_at.is_(None),
            or_(operation.next_attempt_at.is_(None), operation.next_attempt_at <= now),
            lease_free,
        )
        retry = and_(
            operation.status == "retry_wait",
            or_(operation.next_attempt_at.is_(None), operation.next_attempt_at <= now),
            lease_free,
        )
        unknown = and_(
            operation.status.in_(_UNKNOWN_STATUSES),
            lease_free,
            or_(operation.next_attempt_at.is_(None), operation.next_attempt_at <= now),
            or_(
                operation.reconciliation_state.is_(None),
                operation.reconciliation_state != _MANUAL_REVIEW,
            ),
        )
        return or_(prepared, retry, unknown)

    # ------------------------------------------------------------- dispatch

    async def _process(self, claim: _Claim) -> str:
        if claim.status in _UNKNOWN_STATUSES:
            context = await self._load_context(claim)
            self._require_contract(context)
            return await self._lookup(context, claim, manual=False)
        if claim.status == "prepared":
            context = await self._load_context(claim)
            self._require_contract(context)
            # A feed observation of an existing ticket (or an already
            # reconciliation_required case) must be read back, never posted.
            if context.case.remote_ticket_id is not None or context.case.state == (
                "reconciliation_required"
            ):
                return await self._lookup(context, claim, manual=False)
            if context.case.state != "waiting_approval":
                return await self._fail_prepared(claim, ["case_state_changed"])
            return await self._revalidate_and_dispatch(claim, context)
        if claim.status == "retry_wait":
            context = await self._load_context(claim)
            self._require_contract(context)
            if context.case.remote_ticket_id is not None or context.case.state == (
                "reconciliation_required"
            ):
                return await self._lookup(context, claim, manual=False)
            if context.case.state not in {"executing", "waiting_approval"}:
                return await self._fail_retry(claim, "case_state_changed")
            return await self._revalidate_and_dispatch(claim, context)
        return _OUTCOME_SKIPPED

    async def _revalidate_and_dispatch(self, claim: _Claim, context: _Context) -> str:
        market = self._snapshot_market(context.snapshot)
        try:
            problems = await self._workflow.revalidate_execution_context(
                market=market,
                case=context.case,
                approval=context.approval,
                draft=context.draft,
                snapshot=context.snapshot,
                materials=context.materials,
                requester_actor=context.requester,
                decider_actor=context.decider,
                now=utcnow(),
            )
        except ConnectorError as exc:
            if isinstance(exc, ConnectorValidationError | ConnectorPermissionError):
                if claim.status == "prepared":
                    return await self._fail_prepared(claim, [exc.code])
                return await self._fail_retry(claim, exc.code)
            return await self._release_with_backoff(claim, exc.code)
        if problems:
            if claim.status == "prepared":
                return await self._fail_prepared(claim, problems)
            return await self._fail_retry(claim, ",".join(problems)[:128])
        return await self._dispatch(claim, context)

    async def _dispatch(self, claim: _Claim, context: _Context) -> str:
        now = utcnow()
        market = market_of(context)
        try:
            wire = CreateServiceTicketInput.model_validate(context.payload)
        except ValidationError:
            return await self._mark_manual(claim, "PAYLOAD_INVALID", "payload_invalid")
        if canonical_json(dump_wire_model(wire)) != canonical_json(context.payload):
            return await self._mark_manual(claim, "PAYLOAD_INVALID", "payload_invalid")
        expected_states = (
            ("waiting_approval",)
            if claim.status == "prepared"
            else ("waiting_approval", "executing")
        )

        try:
            async with self._session_factory() as session, session.begin():
                operation = await session.get(
                    AfterSalesOperationRow, context.operation.operation_id
                )
                if operation is None:
                    raise _LeaseLost()
                if not self._owns(operation, claim) or operation.status not in {
                    "prepared",
                    "retry_wait",
                }:
                    raise _LeaseLost()
                if operation.attempt >= self._settings.after_sales_worker_write_budget:
                    raise _BudgetReached()

                # The remote revalidation is deliberately outside this
                # transaction. Everything it could have observed must still be
                # true at the instant the dispatching row is committed,
                # otherwise abort before the POST.
                case = await session.get(AfterSalesCaseRow, claim.case_id)
                if (
                    case is None
                    or case.revision != context.case.revision
                    or case.state not in expected_states
                ):
                    raise _ContextChanged("case_revision_changed")

                approval = await session.get(AfterSalesApprovalRow, context.approval.approval_id)
                if (
                    approval is None
                    or approval.case_id != claim.case_id
                    or approval.draft_id != operation.draft_id
                    or approval.snapshot_sha256 != approval_snapshot_digest(context.snapshot)
                ):
                    raise _ContextChanged("approval_changed")
                requester = await StaffRepository(session).get_current_actor(
                    approval.requester_staff_id
                )
                decider = (
                    await StaffRepository(session).get_current_actor(approval.decided_by_staff_id)
                    if approval.decided_by_staff_id is not None
                    else None
                )
                # Use a fresh clock inside the transaction: the approval must
                # still be unexpired at the moment the dispatch is committed,
                # not merely when the remote validation started.
                fence_now = utcnow()
                problems = self._workflow.final_execution_problems(
                    market=market,
                    approval=approval,
                    snapshot=context.snapshot,
                    requester_actor=requester,
                    decider_actor=decider,
                    now=fence_now,
                )
                if problems:
                    raise _ContextChanged(problems[0])

                result = await session.execute(
                    update(AfterSalesOperationRow)
                    .where(
                        AfterSalesOperationRow.operation_id == claim.operation_id,
                        AfterSalesOperationRow.lease_owner == claim.owner,
                        AfterSalesOperationRow.lease_epoch == claim.epoch,
                        AfterSalesOperationRow.status.in_(("prepared", "retry_wait")),
                        AfterSalesOperationRow.attempt
                        < self._settings.after_sales_worker_write_budget,
                        or_(
                            AfterSalesOperationRow.status == "retry_wait",
                            AfterSalesOperationRow.dispatched_at.is_(None),
                        ),
                    )
                    .values(
                        status="dispatching",
                        attempt=AfterSalesOperationRow.attempt + 1,
                        dispatched_at=func.coalesce(AfterSalesOperationRow.dispatched_at, now),
                        next_attempt_at=None,
                        last_error_code=None,
                        failure_reason=None,
                        reconciliation_state=None,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if getattr(result, "rowcount", 0) != 1:
                    raise _LeaseLost()
                case_result = await session.execute(
                    update(AfterSalesCaseRow)
                    .where(
                        AfterSalesCaseRow.case_id == claim.case_id,
                        AfterSalesCaseRow.revision == context.case.revision,
                        AfterSalesCaseRow.state.in_(expected_states),
                    )
                    .values(
                        state="executing",
                        revision=AfterSalesCaseRow.revision + 1,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if getattr(case_result, "rowcount", 0) != 1:
                    raise _ContextChanged("case_revision_changed")
                approval_result = await session.execute(
                    update(AfterSalesApprovalRow)
                    .where(
                        AfterSalesApprovalRow.approval_id == approval.approval_id,
                        AfterSalesApprovalRow.status.in_(("approved", "executing")),
                    )
                    .values(status="executing")
                    .execution_options(synchronize_session=False)
                )
                if getattr(approval_result, "rowcount", 0) != 1:
                    raise _ContextChanged("approval_changed")
                draft_result = await session.execute(
                    update(AfterSalesDraftRow)
                    .where(
                        AfterSalesDraftRow.draft_id == operation.draft_id,
                        AfterSalesDraftRow.case_id == claim.case_id,
                        AfterSalesDraftRow.status.in_(("approved", "executing")),
                    )
                    .values(status="executing")
                    .execution_options(synchronize_session=False)
                )
                if getattr(draft_result, "rowcount", 0) != 1:
                    raise _ContextChanged("draft_changed")
                await AfterSalesRepository(session).append_event(
                    case_id=claim.case_id,
                    event_type="operation_dispatched",
                    actor_staff_id=None,
                    old_state=None,
                    new_state="dispatching",
                    entity_type="operation",
                    entity_id=claim.operation_id,
                    reason="worker_dispatch",
                )
        except _BudgetReached:
            return await self._mark_manual(
                claim, "WRITE_BUDGET_EXHAUSTED", "write_budget_exhausted"
            )
        except _ContextChanged as exc:
            return await self._release_with_backoff(claim, str(exc.args[0]))
        except _LeaseLost:
            return _OUTCOME_SKIPPED

        try:
            creation = await self._connector.create_ticket(
                market=market,
                idempotency_key=context.operation.idempotency_key,
                payload=wire,
            )
        except ConnectorError as exc:
            return await self._classify_write_error(claim, exc)
        return await self._verify_created(claim, context, creation.ticket)

    async def _classify_write_error(self, claim: _Claim, exc: ConnectorError) -> str:
        if exc.recovery_action == "RETRY_CREATE_WITH_SAME_KEY" and exc.retryable:
            if not exc.outcome_unknown:
                return await self._schedule_retry(claim, exc.code)
        if exc.outcome_unknown or exc.recovery_action == "LOOKUP_FIRST":
            return await self._schedule_reconciliation_outcome(
                claim,
                exc.code,
                delay=self._retry_delay(claim),
                increment_lookup=False,
                mark_unknown=True,
            )
        if isinstance(exc, ConnectorValidationError) or exc.code in {
            "VALIDATION_ERROR",
            "UNSUPPORTED_CONTRACT_VERSION",
            "INVALID_CURSOR",
            "ATTACHMENT_TOO_LARGE",
            "UNSUPPORTED_MEDIA_TYPE",
            "UNAUTHORIZED",
            "FORBIDDEN_MARKET",
            "NOT_FOUND",
            "ATTACHMENT_EXPIRED",
        }:
            return await self._fail_external_rejection(claim, exc.code)
        # Conflicts can mean the key/request already produced a ticket, so
        # they are reconciled instead of being declared "never sent".
        return await self._schedule_reconciliation_outcome(
            claim,
            exc.code,
            delay=self._retry_delay(claim),
            increment_lookup=False,
            mark_unknown=True,
        )

    async def _verify_created(self, claim: _Claim, context: _Context, ticket: ServiceTicket) -> str:
        problems = self._ticket_problems(
            ticket, context.snapshot, context.payload, context.operation.approval_id
        )
        if problems:
            return await self._mark_manual(claim, "RECEIPT_MISMATCH", ",".join(problems)[:128])
        market = market_of(context)
        try:
            readback = await self._connector.get_service_ticket(
                market=market, ticket_id=ticket.ticket_id
            )
        except ConnectorError as exc:
            if self._is_permanent_read_error(exc):
                return await self._mark_manual(claim, exc.code, "readback_rejected")
            return await self._schedule_reconciliation_outcome(
                claim,
                exc.code,
                delay=self._retry_delay(claim),
                increment_lookup=False,
            )
        if (
            readback.ticket_id != ticket.ticket_id
            or readback.receipt.receipt_id != ticket.receipt.receipt_id
            or self._ticket_problems(
                readback, context.snapshot, context.payload, context.operation.approval_id
            )
        ):
            return await self._mark_manual(claim, "RECEIPT_MISMATCH", "readback_mismatch")
        return await self._complete(claim, context, readback)

    # -------------------------------------------------------------- recovery

    async def _lookup(self, context: _Context, claim: _Claim, *, manual: bool) -> str:
        operation = context.operation
        if not manual:
            if operation.lookup_count >= self._settings.after_sales_worker_lookup_budget:
                return await self._mark_manual(
                    claim, "LOOKUP_BUDGET_EXHAUSTED", "lookup_budget_exhausted"
                )
            if self._reconciliation_expired(operation):
                return await self._mark_manual(
                    claim, "RECONCILIATION_TIMEOUT", "reconciliation_timeout"
                )
        market = market_of(context)
        try:
            result = await self._connector.get_ticket_by_idempotency_key(
                market=market,
                idempotency_key=operation.idempotency_key,
            )
        except ConnectorError as exc:
            if manual or self._is_permanent_read_error(exc):
                return await self._mark_manual(
                    claim, exc.code, "lookup_rejected", increment_lookup=True
                )
            return await self._schedule_reconciliation_outcome(
                claim,
                exc.code,
                delay=self._retry_delay(claim, lookup_count=operation.lookup_count + 1),
                increment_lookup=True,
            )
        return await self._after_lookup(claim, context, result, manual=manual)

    async def _after_lookup(
        self,
        claim: _Claim,
        context: _Context,
        result: TicketLookupResult,
        *,
        manual: bool,
    ) -> str:
        operation = context.operation
        if isinstance(result, TicketLookupInFlight):
            if manual:
                # An operator read is a hard manual boundary. In-flight is
                # still unknown, so it stays manual and only consumes the
                # bounded lookup budget: re-arming automatic reconciliation
                # would let a later automatic NotVisible schedule another POST.
                return await self._mark_manual(
                    claim, "IN_FLIGHT", "ticket_lookup_in_flight", increment_lookup=True
                )
            delay = max(
                self._retry_delay(claim, lookup_count=operation.lookup_count + 1),
                min(result.retry_after_ms / 1000.0, 60.0),
            )
            # In-flight is still unknown. Keep the bounded automatic loop
            # running; an exhausted budget becomes manual review, never failed.
            return await self._schedule_reconciliation_outcome(
                claim,
                "IN_FLIGHT",
                delay=delay,
                increment_lookup=True,
            )
        if isinstance(result, TicketLookupNotVisible):
            if manual:
                # One explicit read for the operator. It must not schedule a
                # POST or reset the write/lookup budgets.
                return await self._mark_manual(
                    claim, "NOT_VISIBLE", "ticket_not_visible", increment_lookup=True
                )
            return await self._recover_not_visible(claim, context)
        if not isinstance(result, TicketLookupFound):
            return await self._mark_manual(
                claim, "LOOKUP_INVALID", "lookup_invalid", increment_lookup=True
            )
        ticket = result.ticket
        problems = self._ticket_problems(
            ticket, context.snapshot, context.payload, context.operation.approval_id
        )
        if problems:
            return await self._mark_manual(
                claim,
                "RECEIPT_MISMATCH",
                ",".join(problems)[:128],
                increment_lookup=True,
            )
        try:
            readback = await self._connector.get_service_ticket(
                market=market_of(context), ticket_id=ticket.ticket_id
            )
        except ConnectorError as exc:
            if manual or self._is_permanent_read_error(exc):
                return await self._mark_manual(
                    claim, exc.code, "readback_rejected", increment_lookup=True
                )
            return await self._schedule_reconciliation_outcome(
                claim,
                exc.code,
                delay=self._retry_delay(claim, lookup_count=operation.lookup_count + 1),
                increment_lookup=True,
            )
        if (
            readback.ticket_id != ticket.ticket_id
            or readback.receipt.receipt_id != ticket.receipt.receipt_id
            or self._ticket_problems(
                readback, context.snapshot, context.payload, context.operation.approval_id
            )
        ):
            return await self._mark_manual(
                claim,
                "RECEIPT_MISMATCH",
                "readback_mismatch",
                increment_lookup=True,
            )
        return await self._complete(claim, context, readback, lookup_counted=True)

    async def _recover_not_visible(self, claim: _Claim, context: _Context) -> str:
        """Temporary 404: retry the same key only after fresh revalidation.

        The connector has already validated that the source contract answers
        this 404 with ``RETRY_CREATE_WITH_SAME_KEY``. Eligibility is still
        bounded by the original approval, current permissions, business
        version, write budget, lookup budget and reconciliation deadline.
        """
        operation = context.operation
        next_lookup = operation.lookup_count + 1
        if (
            next_lookup >= self._settings.after_sales_worker_lookup_budget
            or self._reconciliation_expired(operation)
            or operation.attempt >= self._settings.after_sales_worker_write_budget
        ):
            return await self._mark_manual(
                claim,
                "NOT_VISIBLE",
                "same_key_retry_budget_exhausted",
                increment_lookup=True,
            )
        market = market_of(context)
        try:
            problems = await self._workflow.revalidate_execution_context(
                market=market,
                case=context.case,
                approval=context.approval,
                draft=context.draft,
                snapshot=context.snapshot,
                materials=context.materials,
                requester_actor=context.requester,
                decider_actor=context.decider,
                now=utcnow(),
            )
        except ConnectorError as exc:
            if isinstance(exc, ConnectorValidationError | ConnectorPermissionError):
                return await self._mark_manual(
                    claim,
                    exc.code,
                    "same_key_retry_context_invalid",
                    increment_lookup=True,
                )
            return await self._schedule_reconciliation_outcome(
                claim,
                exc.code,
                delay=self._retry_delay(claim, lookup_count=next_lookup),
                increment_lookup=True,
            )
        if problems:
            # The earlier unknown attempt is not erased by a now-invalid
            # context; keep the active guard and require a human.
            return await self._mark_manual(
                claim,
                "RETRY_CONTEXT_INVALID",
                ",".join(problems)[:128],
                increment_lookup=True,
            )
        return await self._schedule_same_key_retry(claim, next_lookup=next_lookup)

    async def _schedule_same_key_retry(self, claim: _Claim, *, next_lookup: int) -> str:
        now = utcnow()
        delay = self._retry_delay(claim, lookup_count=next_lookup)
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(AfterSalesOperationRow)
                .where(
                    AfterSalesOperationRow.operation_id == claim.operation_id,
                    AfterSalesOperationRow.lease_owner == claim.owner,
                    AfterSalesOperationRow.lease_epoch == claim.epoch,
                    or_(
                        AfterSalesOperationRow.status.in_(_UNKNOWN_STATUSES),
                        and_(
                            AfterSalesOperationRow.status == "prepared",
                            AfterSalesOperationRow.dispatched_at.is_(None),
                        ),
                    ),
                )
                .values(
                    status="retry_wait",
                    reconciliation_state="same_key_retry_eligible",
                    lookup_count=AfterSalesOperationRow.lookup_count + 1,
                    next_attempt_at=now + timedelta(seconds=delay),
                    last_error_code="NOT_VISIBLE",
                    lease_owner=None,
                    lease_until=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if getattr(result, "rowcount", 0) != 1:
                return _OUTCOME_SKIPPED
            case = await session.get(AfterSalesCaseRow, claim.case_id)
            if case is not None and case.state != "completed":
                await session.execute(
                    update(AfterSalesCaseRow)
                    .where(
                        AfterSalesCaseRow.case_id == claim.case_id,
                        AfterSalesCaseRow.state != "completed",
                    )
                    .values(
                        state="executing",
                        revision=AfterSalesCaseRow.revision + 1,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
            await AfterSalesRepository(session).append_event(
                case_id=claim.case_id,
                event_type="operation_retry_wait",
                actor_staff_id=None,
                old_state=claim.status,
                new_state="retry_wait",
                entity_type="operation",
                entity_id=claim.operation_id,
                reason="NOT_VISIBLE",
            )
        return _OUTCOME_RETRY

    # ------------------------------------------------------------- completion

    async def _complete(
        self,
        claim: _Claim,
        context: _Context,
        ticket: ServiceTicket,
        *,
        lookup_counted: bool = False,
    ) -> str:
        now = utcnow()
        for _ in range(3):
            try:
                async with self._session_factory() as session, session.begin():
                    operation = await session.get(AfterSalesOperationRow, claim.operation_id)
                    if operation is None:
                        raise _LeaseLost()
                    # Ownership comes before the idempotent success shortcut:
                    # a late worker must never report another epoch's result.
                    if not self._owns(operation, claim):
                        raise _LeaseLost()
                    if operation.status == "succeeded":
                        return _OUTCOME_SUCCEEDED
                    if operation.status not in ACTIVE_OPERATION_STATUSES:
                        raise _LeaseLost()
                    try:
                        stored_payload = _load_json(operation.payload_json)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        raise _CorruptOperation("payload_invalid") from None
                    if (
                        operation.payload_sha256 != sha256_text(operation.payload_json)
                        or not isinstance(stored_payload, dict)
                        or canonical_json(stored_payload) != canonical_json(context.payload)
                        or operation.idempotency_key != context.operation.idempotency_key
                    ):
                        raise _CorruptOperation("payload_digest_mismatch")
                    approval = await session.get(AfterSalesApprovalRow, operation.approval_id)
                    if (
                        approval is None
                        or approval.case_id != operation.case_id
                        or approval.draft_id != operation.draft_id
                        or approval.snapshot_sha256 != approval_snapshot_digest(context.snapshot)
                    ):
                        raise _CorruptOperation("snapshot_digest_mismatch")
                    case = await session.get(AfterSalesCaseRow, operation.case_id)
                    if case is None:
                        raise _CorruptOperation("case_context_changed")
                    receipt_problems = self._ticket_problems(
                        ticket, context.snapshot, context.payload, operation.approval_id
                    )
                    if receipt_problems:
                        raise _CorruptOperation("receipt_mismatch")
                    values: dict[str, object] = {
                        "state": "completed",
                        "remote_status": "ticket_created",
                        "remote_ticket_id": ticket.ticket_id,
                    }
                    if not await AfterSalesRepository(session).cas_case(
                        market=case.market,
                        case_id=case.case_id,
                        expected_revision=case.revision,
                        values=values,
                    ):
                        raise _CaseRace()
                    operation_values: dict[str, Any] = {
                        "status": "succeeded",
                        "receipt_json": canonical_json(
                            ticket.model_dump(mode="json", exclude_none=True)
                        ),
                        "last_error_code": None,
                        "failure_reason": None,
                        "next_attempt_at": None,
                        "reconciliation_state": "matched",
                        "reconcile_started_at": func.coalesce(
                            AfterSalesOperationRow.reconcile_started_at, now
                        ),
                        "lease_owner": None,
                        "lease_until": None,
                        "updated_at": now,
                    }
                    if lookup_counted:
                        operation_values["lookup_count"] = AfterSalesOperationRow.lookup_count + 1
                    result = await session.execute(
                        update(AfterSalesOperationRow)
                        .where(
                            AfterSalesOperationRow.operation_id == claim.operation_id,
                            AfterSalesOperationRow.lease_owner == claim.owner,
                            AfterSalesOperationRow.lease_epoch == claim.epoch,
                            AfterSalesOperationRow.status.in_(ACTIVE_OPERATION_STATUSES),
                        )
                        .values(**operation_values)
                        .execution_options(synchronize_session=False)
                    )
                    if getattr(result, "rowcount", 0) != 1:
                        raise _LeaseLost()
                    approval_result = await session.execute(
                        update(AfterSalesApprovalRow)
                        .where(
                            AfterSalesApprovalRow.approval_id == operation.approval_id,
                            AfterSalesApprovalRow.case_id == operation.case_id,
                            AfterSalesApprovalRow.draft_id == operation.draft_id,
                            AfterSalesApprovalRow.status.in_(
                                ("approved", "executing", "expired", "invalidated")
                            ),
                        )
                        .values(status="consumed")
                        .execution_options(synchronize_session=False)
                    )
                    if getattr(approval_result, "rowcount", 0) != 1:
                        raise _ConsumeConflict()
                    draft_result = await session.execute(
                        update(AfterSalesDraftRow)
                        .where(
                            AfterSalesDraftRow.draft_id == operation.draft_id,
                            AfterSalesDraftRow.case_id == operation.case_id,
                            AfterSalesDraftRow.status.in_(("approved", "executing", "invalidated")),
                        )
                        .values(status="consumed")
                        .execution_options(synchronize_session=False)
                    )
                    if getattr(draft_result, "rowcount", 0) != 1:
                        raise _ConsumeConflict()
                    await AfterSalesRepository(session).append_event(
                        case_id=claim.case_id,
                        event_type="operation_succeeded",
                        actor_staff_id=None,
                        old_state=operation.status,
                        new_state="succeeded",
                        entity_type="operation",
                        entity_id=claim.operation_id,
                        reason="receipt_verified",
                    )
                    await AfterSalesRepository(session).append_event(
                        case_id=claim.case_id,
                        event_type="case_completed",
                        actor_staff_id=None,
                        old_state=case.state,
                        new_state="completed",
                        entity_type="case",
                        entity_id=str(claim.case_id),
                        reason="ticket_verified",
                    )
                return _OUTCOME_SUCCEEDED
            except _CaseRace:
                continue
            except _CorruptOperation as exc:
                return await self._mark_manual(claim, exc.code, "stored_operation_invalid")
            except _ConsumeConflict:
                # Do not claim completion if the original approval/draft could
                # not be retired atomically. Keep the operation fenced for a
                # human instead of fabricating a completed case.
                return await self._mark_manual(claim, "CONSUME_CONFLICT", "consume_conflict")
            except _LeaseLost:
                return _OUTCOME_SKIPPED
            except SQLAlchemyError as exc:
                raise DatabaseError("failed to persist the verified execution") from exc
        return _OUTCOME_DEFERRED

    # -------------------------------------------------------------- failures

    async def _fail_prepared(self, claim: _Claim, problems: list[str]) -> str:
        reason = ",".join(problems)[:128]
        current = await self._read_operation(claim)
        if current is None:
            return _OUTCOME_SKIPPED
        if current.unknown_attempts > 0:
            # Even a prepared-looking row must not be declared "never sent"
            # when an earlier external effect was never disproven.
            return await self._schedule_reconciliation_outcome(
                claim,
                problems[0] if problems else "UNKNOWN_EFFECT",
                delay=self._retry_delay(claim, lookup_count=current.lookup_count + 1),
                increment_lookup=False,
            )
        if await self._case_has_remote_ticket(claim.case_id):
            # A trusted observation of an existing ticket cannot be discarded
            # by failing a local operation that never dispatched.
            return await self._mark_manual(
                claim, "REMOTE_TICKET_UNVERIFIED", "remote_ticket_requires_review"
            )
        expired = "approval_expired" in problems
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            operation = await session.get(AfterSalesOperationRow, claim.operation_id)
            if operation is None or not self._owns(operation, claim):
                return _OUTCOME_SKIPPED
            result = await session.execute(
                update(AfterSalesOperationRow)
                .where(
                    AfterSalesOperationRow.operation_id == claim.operation_id,
                    AfterSalesOperationRow.lease_owner == claim.owner,
                    AfterSalesOperationRow.lease_epoch == claim.epoch,
                    AfterSalesOperationRow.status == "prepared",
                    AfterSalesOperationRow.dispatched_at.is_(None),
                )
                .values(
                    status="failed",
                    failure_reason=reason,
                    last_error_code="APPROVAL_EXPIRED" if expired else "CONTEXT_INVALID",
                    next_attempt_at=None,
                    lease_owner=None,
                    lease_until=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if getattr(result, "rowcount", 0) != 1:
                return _OUTCOME_SKIPPED
            await session.execute(
                update(AfterSalesApprovalRow)
                .where(
                    AfterSalesApprovalRow.approval_id == operation.approval_id,
                    AfterSalesApprovalRow.status.in_(("approved", "pending")),
                )
                .values(
                    status="expired" if expired else "invalidated",
                    decision_reason=reason,
                    decided_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                update(AfterSalesDraftRow)
                .where(
                    AfterSalesDraftRow.draft_id == operation.draft_id,
                    AfterSalesDraftRow.status.in_(("approved", "pending_approval")),
                )
                .values(status="invalidated", decided_at=now)
                .execution_options(synchronize_session=False)
            )
            case = await session.get(AfterSalesCaseRow, claim.case_id)
            if case is not None and case.state == "waiting_approval":
                await session.execute(
                    update(AfterSalesCaseRow)
                    .where(
                        AfterSalesCaseRow.case_id == claim.case_id,
                        AfterSalesCaseRow.state == "waiting_approval",
                    )
                    .values(
                        state="processing",
                        revision=AfterSalesCaseRow.revision + 1,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
            await AfterSalesRepository(session).append_event(
                case_id=claim.case_id,
                event_type="operation_failed",
                actor_staff_id=None,
                old_state="prepared",
                new_state="failed",
                entity_type="operation",
                entity_id=claim.operation_id,
                reason=reason,
            )
            await AfterSalesRepository(session).append_event(
                case_id=claim.case_id,
                event_type="approval_expired" if expired else "approval_invalidated",
                actor_staff_id=None,
                old_state="waiting_approval",
                new_state="processing",
                entity_type="approval",
                entity_id=operation.approval_id,
                reason=reason,
            )
        return _OUTCOME_FAILED

    async def _fail_retry(self, claim: _Claim, reason: str) -> str:
        return await self._fail_after_dispatch(
            claim,
            reason=reason,
            code="CONTEXT_INVALID",
            case_reason="context_invalid",
        )

    async def _fail_external_rejection(self, claim: _Claim, code: str) -> str:
        return await self._fail_after_dispatch(
            claim,
            reason=f"upstream_rejected:{code}"[:128],
            code=code,
            case_reason="upstream_rejected_before_effect",
        )

    async def _fail_after_dispatch(
        self, claim: _Claim, *, reason: str, code: str, case_reason: str
    ) -> str:
        current = await self._read_operation(claim)
        if current is None:
            return _OUTCOME_SKIPPED
        if current.unknown_attempts > 0:
            # A later 400/401 or a now-invalid context only proves that *this*
            # attempt did not write. The earlier unknown attempt remains open,
            # so keep the active-operation guard and look up the original key
            # again instead of releasing the case as failed.
            return await self._schedule_reconciliation_outcome(
                claim,
                code,
                delay=self._retry_delay(claim, lookup_count=current.lookup_count + 1),
                increment_lookup=False,
                reason=reason,
            )
        if await self._case_has_remote_ticket(claim.case_id):
            return await self._mark_manual(
                claim, "REMOTE_TICKET_UNVERIFIED", "remote_ticket_requires_review"
            )
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            operation = await session.get(AfterSalesOperationRow, claim.operation_id)
            if operation is None or not self._owns(operation, claim):
                return _OUTCOME_SKIPPED
            result = await session.execute(
                update(AfterSalesOperationRow)
                .where(
                    AfterSalesOperationRow.operation_id == claim.operation_id,
                    AfterSalesOperationRow.lease_owner == claim.owner,
                    AfterSalesOperationRow.lease_epoch == claim.epoch,
                    AfterSalesOperationRow.status.in_(("dispatching", "retry_wait")),
                )
                .values(
                    status="failed",
                    failure_reason=reason,
                    last_error_code=code,
                    next_attempt_at=None,
                    lease_owner=None,
                    lease_until=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if getattr(result, "rowcount", 0) != 1:
                return _OUTCOME_SKIPPED
            await session.execute(
                update(AfterSalesApprovalRow)
                .where(
                    AfterSalesApprovalRow.approval_id == operation.approval_id,
                    AfterSalesApprovalRow.status.in_(("approved", "executing")),
                )
                .values(status="invalidated", decision_reason=reason, decided_at=now)
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                update(AfterSalesDraftRow)
                .where(
                    AfterSalesDraftRow.draft_id == operation.draft_id,
                    AfterSalesDraftRow.status.in_(("approved", "executing")),
                )
                .values(status="invalidated", decided_at=now)
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                update(AfterSalesCaseRow)
                .where(
                    AfterSalesCaseRow.case_id == claim.case_id,
                    AfterSalesCaseRow.state.not_in(("completed",)),
                )
                .values(
                    state="failed",
                    revision=AfterSalesCaseRow.revision + 1,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            await AfterSalesRepository(session).append_event(
                case_id=claim.case_id,
                event_type="operation_failed",
                actor_staff_id=None,
                old_state="dispatching",
                new_state="failed",
                entity_type="operation",
                entity_id=claim.operation_id,
                reason=reason,
            )
            await AfterSalesRepository(session).append_event(
                case_id=claim.case_id,
                event_type="case_failed",
                actor_staff_id=None,
                old_state="executing",
                new_state="failed",
                entity_type="case",
                entity_id=str(claim.case_id),
                reason=case_reason,
            )
        return _OUTCOME_FAILED

    async def _mark_manual(
        self,
        claim: _Claim,
        code: str,
        reason: str,
        *,
        increment_lookup: bool = False,
    ) -> str:
        scheduled = await self._schedule_reconciliation(
            claim,
            code,
            delay=None,
            increment_lookup=increment_lookup,
            force_manual=True,
            reason=reason,
        )
        return _OUTCOME_MANUAL if scheduled else _OUTCOME_SKIPPED

    async def _schedule_retry(self, claim: _Claim, code: str) -> str:
        operation = await self._read_operation(claim)
        if operation is None:
            return _OUTCOME_SKIPPED
        # Each dispatch increments attempt only when the POST is persisted.
        if operation.attempt >= self._settings.after_sales_worker_write_budget:
            return await self._mark_manual(
                claim, "WRITE_BUDGET_EXHAUSTED", "write_budget_exhausted"
            )
        now = utcnow()
        delay = self._retry_delay(claim, lookup_count=operation.attempt + 1)
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(AfterSalesOperationRow)
                .where(
                    AfterSalesOperationRow.operation_id == claim.operation_id,
                    AfterSalesOperationRow.lease_owner == claim.owner,
                    AfterSalesOperationRow.lease_epoch == claim.epoch,
                    AfterSalesOperationRow.status == "dispatching",
                )
                .values(
                    status="retry_wait",
                    next_attempt_at=now + timedelta(seconds=delay),
                    last_error_code=code,
                    reconciliation_state=None,
                    lease_owner=None,
                    lease_until=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if getattr(result, "rowcount", 0) != 1:
                return _OUTCOME_SKIPPED
            await AfterSalesRepository(session).append_event(
                case_id=claim.case_id,
                event_type="operation_retry_wait",
                actor_staff_id=None,
                old_state="dispatching",
                new_state="retry_wait",
                entity_type="operation",
                entity_id=claim.operation_id,
                reason=code,
            )
        return _OUTCOME_RETRY

    async def _schedule_reconciliation(
        self,
        claim: _Claim,
        code: str,
        *,
        delay: float | None,
        increment_lookup: bool,
        force_manual: bool = False,
        reason: str | None = None,
        mark_unknown: bool = False,
    ) -> str | None:
        now = utcnow()
        manual = force_manual
        if not manual and increment_lookup:
            operation = await self._read_operation(claim)
            if operation is None:
                return None
            if operation.lookup_count + 1 >= self._settings.after_sales_worker_lookup_budget:
                manual = True
            elif self._reconciliation_expired(operation, now=now):
                manual = True
        next_attempt = (
            None if manual else now + timedelta(seconds=delay or self._retry_delay(claim))
        )
        async with self._session_factory() as session, session.begin():
            values: dict[str, Any] = {
                "status": "reconciliation_required",
                "reconciliation_state": _MANUAL_REVIEW if manual else "required",
                "reconcile_started_at": func.coalesce(
                    AfterSalesOperationRow.reconcile_started_at, now
                ),
                "last_error_code": code,
                "failure_reason": (reason or code)[:128],
                "next_attempt_at": next_attempt,
                "lease_owner": None,
                "lease_until": None,
                "updated_at": now,
            }
            if increment_lookup:
                values["lookup_count"] = AfterSalesOperationRow.lookup_count + 1
            if mark_unknown:
                values["unknown_attempts"] = AfterSalesOperationRow.unknown_attempts + 1
            result = await session.execute(
                update(AfterSalesOperationRow)
                .where(
                    AfterSalesOperationRow.operation_id == claim.operation_id,
                    AfterSalesOperationRow.lease_owner == claim.owner,
                    AfterSalesOperationRow.lease_epoch == claim.epoch,
                    AfterSalesOperationRow.status.in_(ACTIVE_OPERATION_STATUSES),
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if getattr(result, "rowcount", 0) != 1:
                return None
            case = await session.get(AfterSalesCaseRow, claim.case_id)
            old_state = case.state if case is not None else None
            if case is not None and case.state != "completed":
                await session.execute(
                    update(AfterSalesCaseRow)
                    .where(
                        AfterSalesCaseRow.case_id == claim.case_id,
                        AfterSalesCaseRow.state != "completed",
                    )
                    .values(
                        state="reconciliation_required",
                        revision=AfterSalesCaseRow.revision + 1,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
            repository = AfterSalesRepository(session)
            await repository.append_event(
                case_id=claim.case_id,
                event_type="manual_review_required" if manual else "reconciliation_pending",
                actor_staff_id=None,
                old_state=old_state,
                new_state="reconciliation_required",
                entity_type="operation",
                entity_id=claim.operation_id,
                reason=(reason or code)[:128],
            )
        return _MANUAL_REVIEW if manual else "scheduled"

    async def _schedule_reconciliation_outcome(
        self,
        claim: _Claim,
        code: str,
        *,
        delay: float | None,
        increment_lookup: bool,
        force_manual: bool = False,
        reason: str | None = None,
        mark_unknown: bool = False,
    ) -> str:
        scheduled = await self._schedule_reconciliation(
            claim,
            code,
            delay=delay,
            increment_lookup=increment_lookup,
            force_manual=force_manual,
            reason=reason,
            mark_unknown=mark_unknown,
        )
        if scheduled is None:
            return _OUTCOME_SKIPPED
        return _OUTCOME_MANUAL if scheduled == _MANUAL_REVIEW else _OUTCOME_RECONCILING

    async def _release_with_backoff(self, claim: _Claim, code: str) -> str:
        current = await self._read_operation(claim)
        if current is None:
            return _OUTCOME_SKIPPED
        if current.status == "dispatching" or current.status in _UNKNOWN_STATUSES:
            # A dispatching row whose attempt was interrupted (timeout, local
            # commit failure or cancellation) is an unknown external outcome,
            # not a safe-to-fail prepared row.
            scheduled = await self._schedule_reconciliation(
                claim,
                code,
                delay=self._retry_delay(claim, lookup_count=current.lookup_count + 1),
                increment_lookup=False,
                mark_unknown=current.status == "dispatching",
            )
            if scheduled is None:
                return _OUTCOME_SKIPPED
            return _OUTCOME_MANUAL if scheduled == _MANUAL_REVIEW else _OUTCOME_RECONCILING
        if current.status not in {"prepared", "retry_wait"}:
            return _OUTCOME_SKIPPED
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(AfterSalesOperationRow)
                .where(
                    AfterSalesOperationRow.operation_id == claim.operation_id,
                    AfterSalesOperationRow.lease_owner == claim.owner,
                    AfterSalesOperationRow.lease_epoch == claim.epoch,
                    AfterSalesOperationRow.status == current.status,
                )
                .values(
                    next_attempt_at=now + timedelta(seconds=self._retry_delay(claim)),
                    last_error_code=code,
                    lease_owner=None,
                    lease_until=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if getattr(result, "rowcount", 0) != 1:
                return _OUTCOME_SKIPPED
            await AfterSalesRepository(session).append_event(
                case_id=claim.case_id,
                event_type="operation_deferred",
                actor_staff_id=None,
                old_state=current.status,
                new_state=current.status,
                entity_type="operation",
                entity_id=claim.operation_id,
                reason=code,
            )
        return _OUTCOME_DEFERRED

    async def _handle_interrupted(self, claim: _Claim, code: str) -> str:
        try:
            return await self._release_with_backoff(claim, code)
        except Exception:  # noqa: BLE001 - best effort after an already-failed attempt
            logger.error(
                "after_sales worker could not release lease operation_id=%s",
                claim.operation_id,
            )
            return _OUTCOME_DEFERRED

    # --------------------------------------------------------------- context

    async def _load_context(self, claim: _Claim) -> _Context:
        async with self._session_factory() as session:
            operation = await session.get(AfterSalesOperationRow, claim.operation_id)
            if operation is None or not self._owns(operation, claim):
                raise _LeaseLost()
            case = await session.get(AfterSalesCaseRow, operation.case_id)
            approval = await session.get(AfterSalesApprovalRow, operation.approval_id)
            draft = await session.get(AfterSalesDraftRow, operation.draft_id)
            if case is None or approval is None or draft is None:
                raise _CorruptOperation("operation_chain_invalid")
            materials = list(await AfterSalesRepository(session).list_materials(case.case_id))
            requester = await StaffRepository(session).get_current_actor(
                approval.requester_staff_id
            )
            decider = (
                await StaffRepository(session).get_current_actor(approval.decided_by_staff_id)
                if approval.decided_by_staff_id is not None
                else None
            )
        try:
            snapshot = _load_json(approval.snapshot_json)
            payload = _load_json(operation.payload_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            raise _CorruptOperation("stored_json_invalid") from None
        if not isinstance(snapshot, dict) or not isinstance(payload, dict):
            raise _CorruptOperation("stored_json_invalid")
        return _Context(
            operation=operation,
            case=case,
            approval=approval,
            draft=draft,
            snapshot=snapshot,
            payload=payload,
            materials=materials,
            requester=requester,
            decider=decider,
        )

    def _require_contract(self, context: _Context) -> None:
        code = self._contract_problem(context)
        if code is not None:
            raise _CorruptOperation(code)

    def _contract_problem(self, context: _Context) -> str | None:
        try:
            case_snapshot = cast(dict[str, Any], context.snapshot["case"])
            order_snapshot = cast(dict[str, Any], context.snapshot["order"])
            draft_snapshot = cast(dict[str, Any], context.snapshot["draft"])
            evidence_snapshot = cast(dict[str, Any], context.snapshot["evidence"])
        except (KeyError, TypeError):
            return "snapshot_invalid"
        if not all(
            isinstance(item, dict)
            for item in (case_snapshot, order_snapshot, draft_snapshot, evidence_snapshot)
        ):
            return "snapshot_invalid"
        operation = context.operation
        if (
            case_snapshot.get("case_id") != operation.case_id
            or context.approval.case_id != operation.case_id
            or context.approval.draft_id != operation.draft_id
            or draft_snapshot.get("draft_id") != operation.draft_id
            or order_snapshot.get("order_id") != case_snapshot.get("order_id")
            or case_snapshot.get("market") not in AFTER_SALES_MARKETS
        ):
            return "snapshot_identity_mismatch"
        if context.approval.snapshot_sha256 != approval_snapshot_digest(context.snapshot):
            return "snapshot_digest_mismatch"
        if operation.payload_sha256 != sha256_text(operation.payload_json):
            return "payload_digest_mismatch"
        try:
            expected = operation_payload(context.snapshot, approval_id=context.approval.approval_id)
            wire = CreateServiceTicketInput.model_validate(context.payload)
        except (KeyError, TypeError, ValueError, ValidationError):
            return "payload_invalid"
        if canonical_json(_load_json(operation.payload_json)) != canonical_json(context.payload):
            return "payload_digest_mismatch"
        if canonical_json(expected) != canonical_json(context.payload):
            return "payload_mismatch"
        if canonical_json(dump_wire_model(wire)) != canonical_json(context.payload):
            return "payload_invalid"
        if not evidence_snapshot.get("policy_version"):
            return "policy_missing"
        return None

    @staticmethod
    def _snapshot_market(snapshot: dict[str, Any]) -> str:
        case_snapshot = cast(dict[str, Any], snapshot["case"])
        return str(case_snapshot["market"])

    def _ticket_problems(
        self,
        ticket: ServiceTicket,
        snapshot: dict[str, Any],
        payload: dict[str, Any],
        approval_id: str,
    ) -> list[str]:
        try:
            case_snapshot = cast(dict[str, Any], snapshot["case"])
            order_snapshot = cast(dict[str, Any], snapshot["order"])
        except (KeyError, TypeError):
            return ["snapshot_invalid"]
        problems: list[str] = []
        # Market and order identity come from the immutable approval snapshot;
        # all wire fields come from the persisted operation payload. The
        # mutable case row is never allowed to substitute for either.
        if case_snapshot.get("order_id") != order_snapshot.get("order_id"):
            problems.append("snapshot_identity_mismatch")
        if ticket.market != case_snapshot.get("market"):
            problems.append("ticket_market_mismatch")
        if ticket.order_id != order_snapshot.get("order_id"):
            problems.append("ticket_order_mismatch")
        if ticket.request_id != payload.get("request_id"):
            problems.append("ticket_request_mismatch")
        if ticket.action != payload.get("action"):
            problems.append("ticket_action_mismatch")
        if ticket.reply != payload.get("reply"):
            problems.append("ticket_reply_mismatch")
        if ticket.draft_version != str(payload.get("draft_version")):
            problems.append("ticket_draft_version_mismatch")
        if ticket.approval_id != approval_id or ticket.approval_id != payload.get("approval_id"):
            problems.append("ticket_approval_mismatch")
        if ticket.policy_version != payload.get("policy_version"):
            problems.append("ticket_policy_version_mismatch")
        if ticket.order_version != payload.get("order_version"):
            problems.append("ticket_order_version_mismatch")
        receipt = ticket.receipt
        if (
            receipt.ticket_id != ticket.ticket_id
            or receipt.request_id != ticket.request_id
            or receipt.action != ticket.action
            or receipt.request_status != "ticket_created"
        ):
            problems.append("ticket_receipt_mismatch")
        return list(dict.fromkeys(problems))

    @staticmethod
    def _owns(operation: AfterSalesOperationRow, claim: _Claim) -> bool:
        return (
            operation.lease_owner == claim.owner
            and operation.lease_epoch == claim.epoch
            and operation.case_id == claim.case_id
        )

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _is_permanent_read_error(exc: ConnectorError) -> bool:
        return isinstance(exc, ConnectorValidationError | ConnectorPermissionError) or (
            exc.code
            in {
                "VALIDATION_ERROR",
                "UNSUPPORTED_CONTRACT_VERSION",
                "UNAUTHORIZED",
                "FORBIDDEN_MARKET",
                "UNSUPPORTED_MEDIA_TYPE",
                "ATTACHMENT_EXPIRED",
            }
        )

    def _retry_delay(self, claim: _Claim, *, lookup_count: int | None = None) -> float:
        attempt = lookup_count if lookup_count is not None else 1
        base = self._settings.after_sales_worker_retry_base_seconds
        maximum = self._settings.after_sales_worker_retry_max_seconds
        return min(maximum, base * (2 ** max(0, attempt - 1)))

    def _reconciliation_expired(
        self, operation: AfterSalesOperationRow, *, now: datetime | None = None
    ) -> bool:
        if operation.reconcile_started_at is None:
            return False
        started = as_utc(operation.reconcile_started_at)
        current = now or utcnow()
        return (current - started).total_seconds() >= (
            self._settings.after_sales_worker_reconciliation_max_seconds
        )

    async def _case_has_remote_ticket(self, case_id: int) -> bool:
        async with self._session_factory() as session:
            case = await session.get(AfterSalesCaseRow, case_id)
            return case is not None and case.remote_ticket_id is not None

    async def _read_operation(self, claim: _Claim) -> AfterSalesOperationRow | None:
        async with self._session_factory() as session:
            operation = await session.get(AfterSalesOperationRow, claim.operation_id)
            if operation is None or not self._owns(operation, claim):
                return None
            return operation

    # --------------------------------------------------------------- operator

    async def list_operations(
        self,
        market: str,
        *,
        status: str | None,
        limit: int,
        offset: int,
    ) -> WorkerOperationListResponse:
        filters: list[Any] = [AfterSalesCaseRow.market == market]
        if status:
            filters.append(AfterSalesOperationRow.status == status)
        async with self._session_factory() as session:
            total = await session.scalar(
                select(func.count())
                .select_from(AfterSalesOperationRow)
                .join(
                    AfterSalesCaseRow,
                    AfterSalesCaseRow.case_id == AfterSalesOperationRow.case_id,
                )
                .where(*filters)
            )
            rows = list(
                (
                    await session.execute(
                        select(AfterSalesOperationRow, AfterSalesCaseRow)
                        .join(
                            AfterSalesCaseRow,
                            AfterSalesCaseRow.case_id == AfterSalesOperationRow.case_id,
                        )
                        .where(*filters)
                        .order_by(
                            AfterSalesOperationRow.updated_at.desc(),
                            AfterSalesOperationRow.operation_id.desc(),
                        )
                        .offset(offset)
                        .limit(limit + 1)
                    )
                ).all()
            )
        has_more = len(rows) > limit
        rows = rows[:limit]
        return WorkerOperationListResponse(
            items=[_worker_summary(operation, case) for operation, case in rows],
            total=int(total or 0),
            limit=limit,
            offset=offset,
            has_more=has_more,
        )

    async def get_operation(self, market: str, operation_id: str) -> WorkerOperationDetail:
        async with self._session_factory() as session:
            operation, case = await self._operator_row(session, market, operation_id)
            events = await AfterSalesRepository(session).list_events(
                case.case_id, after_seq=0, limit=50
            )
        return _worker_detail(operation, case, list(events))

    async def continue_lookup(self, market: str, operation_id: str) -> WorkerOperationDetail:
        claim = await self._claim_operator(market, operation_id)
        try:
            context = await self._load_context(claim)
        except _LeaseLost:
            raise StateConflictError("the operation must be looked up by another worker") from None
        except _CorruptOperation as exc:
            await self._mark_manual(claim, exc.code, "stored_operation_invalid")
            return await self.get_operation(market, operation_id)
        try:
            self._require_contract(context)
        except _CorruptOperation as exc:
            await self._mark_manual(claim, exc.code, "stored_operation_invalid")
            return await self.get_operation(market, operation_id)
        await self._lookup(context, claim, manual=True)
        return await self.get_operation(market, operation_id)

    async def _claim_operator(self, market: str, operation_id: str) -> _Claim:
        now = utcnow()
        lease_until = now + timedelta(seconds=self._settings.after_sales_worker_lease_seconds)
        async with self._session_factory() as session, session.begin():
            operation, case = await self._operator_row(session, market, operation_id)
            allowed = operation.status in _UNKNOWN_STATUSES or (
                operation.status == "prepared"
                and operation.dispatched_at is None
                and case.remote_ticket_id is not None
            )
            if not allowed:
                raise StateConflictError("the operation has no unresolved receipt to look up")
            values: dict[str, Any] = {
                "lease_owner": self._owner,
                "lease_epoch": operation.lease_epoch + 1,
                "lease_until": lease_until,
                "updated_at": now,
            }
            if operation.status in _UNKNOWN_STATUSES:
                values["reconciliation_state"] = "checking"
                if operation.unknown_attempts < 1:
                    values["unknown_attempts"] = 1
            result = await session.execute(
                update(AfterSalesOperationRow)
                .where(
                    AfterSalesOperationRow.operation_id == operation_id,
                    AfterSalesOperationRow.status == operation.status,
                    AfterSalesOperationRow.lease_epoch == operation.lease_epoch,
                    or_(
                        AfterSalesOperationRow.lease_until.is_(None),
                        AfterSalesOperationRow.lease_until <= now,
                    ),
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if getattr(result, "rowcount", 0) != 1:
                raise StateConflictError("the operation is already being processed")
        return _Claim(
            operation_id=operation_id,
            case_id=case.case_id,
            market=market,
            owner=self._owner,
            epoch=operation.lease_epoch + 1,
            status=operation.status,
        )

    async def _operator_row(
        self, session: AsyncSession, market: str, operation_id: str
    ) -> tuple[AfterSalesOperationRow, AfterSalesCaseRow]:
        operation = await session.get(AfterSalesOperationRow, operation_id)
        if operation is None:
            raise NotFoundError()
        case = await session.get(AfterSalesCaseRow, operation.case_id)
        if case is None or case.market != market:
            raise NotFoundError()
        return operation, case


class _BudgetReached(Exception):
    pass


class _ContextChanged(Exception):
    pass


def market_of(context: _Context) -> str:
    return str(cast(dict[str, Any], context.snapshot["case"])["market"])


def _load_json(raw: str) -> Any:
    return json.loads(raw)


def _worker_summary(
    operation: AfterSalesOperationRow, case: AfterSalesCaseRow
) -> WorkerOperationSummary:
    return WorkerOperationSummary(
        operation_id=operation.operation_id,
        case_id=operation.case_id,
        market=cast(Any, case.market),
        status=cast(Any, operation.status),
        case_state=cast(Any, case.state),
        attempt=operation.attempt,
        lookup_count=operation.lookup_count,
        reconciliation_state=cast(Any, operation.reconciliation_state),
        remote_ticket_id=case.remote_ticket_id,
        last_error_code=operation.last_error_code,
        failure_reason=operation.failure_reason,
        dispatched_at=(
            as_utc(operation.dispatched_at) if operation.dispatched_at is not None else None
        ),
        next_attempt_at=(
            as_utc(operation.next_attempt_at) if operation.next_attempt_at is not None else None
        ),
        updated_at=as_utc(operation.updated_at),
    )


def _worker_detail(
    operation: AfterSalesOperationRow,
    case: AfterSalesCaseRow,
    events: list[AfterSalesEventRow],
) -> WorkerOperationDetail:
    summary = _worker_summary(operation, case)
    return WorkerOperationDetail(
        **summary.model_dump(),
        reconcile_started_at=(
            as_utc(operation.reconcile_started_at)
            if operation.reconcile_started_at is not None
            else None
        ),
        events=[
            WorkerOperationEvent(
                seq=event.seq,
                event_type=event.event_type,
                old_state=event.old_state,
                new_state=event.new_state,
                reason=event.reason,
                created_at=as_utc(event.created_at),
            )
            for event in events
        ],
    )
