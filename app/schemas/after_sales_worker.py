"""Sanitised operator-facing execution/reconciliation views.

These models are deliberately narrower than :class:`CaseDetail`: operators
see progress and safe error codes, never consumer replies, descriptions,
payloads, receipts, attachment names or service credentials.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.after_sales import AfterSalesMarket
from app.schemas.after_sales_workflow import CaseState, OperationStatus

ReconciliationState = Literal[
    "required",
    "checking",
    "matched",
    "same_key_retry_eligible",
    "manual_review",
]


class WorkerOperationSummary(BaseModel):
    operation_id: str
    case_id: int
    market: AfterSalesMarket
    status: OperationStatus
    case_state: CaseState
    attempt: int
    lookup_count: int
    reconciliation_state: ReconciliationState | None
    remote_ticket_id: str | None
    last_error_code: str | None
    failure_reason: str | None
    dispatched_at: datetime | None
    next_attempt_at: datetime | None
    updated_at: datetime


class WorkerOperationListResponse(BaseModel):
    items: list[WorkerOperationSummary]
    total: int
    limit: int
    offset: int
    has_more: bool


class WorkerOperationEvent(BaseModel):
    seq: int
    event_type: str
    old_state: str | None
    new_state: str | None
    reason: str | None
    created_at: datetime


class WorkerOperationDetail(WorkerOperationSummary):
    reconcile_started_at: datetime | None
    events: list[WorkerOperationEvent]


__all__ = [
    "ReconciliationState",
    "WorkerOperationDetail",
    "WorkerOperationEvent",
    "WorkerOperationListResponse",
    "WorkerOperationSummary",
]
