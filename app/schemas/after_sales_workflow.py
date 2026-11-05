"""Request/response contracts for the DTC-internal persistent workflow.

These are internal staff API models, not the E-pig wire contract. Every
mutation request carries an explicit ``expected_revision`` and forbids
unknown fields so clients cannot submit staff identity, approval state,
receipts, arbitrary policy versions or connector parameters.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.connectors.models import (
    RequestStatus,
    RequestType,
    ServiceOrder,
    ServiceShipment,
    ServiceTicket,
    TicketAction,
)
from app.core.time import as_utc, utcnow
from app.schemas.after_sales import AfterSalesMarket, StaffRole

APPROVE_REJECT_ROLES: frozenset[StaffRole] = frozenset({"approver"})

CaseState = Literal[
    "pending",
    "processing",
    "needs_info",
    "waiting_approval",
    "executing",
    "reconciliation_required",
    "completed",
    "failed",
]
DraftStatus = Literal[
    "editing",
    "pending_approval",
    "approved",
    "executing",
    "consumed",
    "rejected",
    "invalidated",
    "superseded",
]
ApprovalStatus = Literal[
    "pending",
    "approved",
    "executing",
    "consumed",
    "rejected",
    "expired",
    "invalidated",
]
OperationStatus = Literal[
    "prepared",
    "dispatching",
    "awaiting_receipt",
    "retry_wait",
    "reconciliation_required",
    "succeeded",
    "failed",
]
MaterialType = Literal[
    "manual_verification",
    "delivery_verification",
    "customer_contact",
    "internal_note",
    "evidence",
]


class _ForbidExtra(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SyncResponse(BaseModel):
    market: AfterSalesMarket
    processed: int
    created: int
    updated: int
    stale_ignored: int
    has_more: bool
    high_watermark: str
    last_success_at: datetime


class CaseSummary(BaseModel):
    case_id: int
    market: AfterSalesMarket
    request_id: str
    order_id: str
    request_type: RequestType
    state: CaseState
    revision: int
    attachment_count: int
    remote_status: RequestStatus
    remote_ticket_id: str | None
    updated_at: datetime
    has_pending_approval: bool
    has_prepared_operation: bool


class CaseListResponse(BaseModel):
    items: list[CaseSummary]
    total: int
    limit: int
    offset: int
    has_more: bool
    last_success_at: datetime | None


class AttachmentView(BaseModel):
    attachment_id: str
    file_name: str
    media_type: str
    size_bytes: int
    status: str
    created_at: datetime
    expires_at: datetime


class MaterialView(BaseModel):
    material_id: str
    material_type: MaterialType
    source: str
    content: str
    applies_to_sku: str | None
    delivered_at: datetime | None
    created_by_staff_id: str
    created_at: datetime


class EvidenceView(BaseModel):
    chunk_id: str
    document_id: str
    title: str
    section: str | None
    content: str
    policy_version: str
    source_path: str
    source_label: str
    source_type: str
    policy_status: str
    is_demo: bool
    retrieved_at: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    content_sha256: str


class DraftView(BaseModel):
    draft_id: str
    version: int
    action: TicketAction
    internal_summary: str
    consumer_reply: str
    evidence: list[EvidenceView]
    evidence_sha256: str
    policy_version: str | None
    order_version: str | None
    material_revision: int
    status: DraftStatus
    author_staff_id: str
    created_at: datetime
    submitted_at: datetime | None
    decided_at: datetime | None


class TrustedCategoryView(BaseModel):
    sku: str
    category: str
    source: str


class CaseEvidenceResponse(BaseModel):
    """Server-derived policy evidence for one case; never a whole-corpus dump."""

    case_id: int
    market: AfterSalesMarket
    as_of: date
    queried_at: datetime
    trusted_categories: list[TrustedCategoryView]
    chunks: list[EvidenceView]
    policy_version: str | None
    evidence_sha256: str
    corpus_revision: str
    missing_reasons: list[str]


TicketVerificationState = Literal["matched_operation", "external_only"]


class CaseTicketResponse(BaseModel):
    """A real remote ticket read; never a local state transition or approval."""

    case_id: int
    market: AfterSalesMarket
    ticket: ServiceTicket
    queried_at: datetime
    verification_state: TicketVerificationState
    operation_id: str | None = None


class AttachmentFingerprintView(BaseModel):
    attachment_id: str
    status: str
    media_type: str
    size_bytes: int
    expires_at: str


class ApprovalCaseSnapshotView(BaseModel):
    case_id: int
    market: AfterSalesMarket
    request_id: str
    order_id: str
    request_type: RequestType


class ApprovalRequestSnapshotView(BaseModel):
    request_id: str
    sequence: str
    status: RequestStatus
    ticket_id: str | None = None
    type: RequestType
    description_sha256: str
    attachments: list[AttachmentFingerprintView]
    updated_at: str


class ApprovalDraftSnapshotView(BaseModel):
    draft_id: str
    version: int
    action: TicketAction
    internal_summary: str
    consumer_reply: str
    evidence_sha256: str


class ApprovalMaterialSnapshotView(BaseModel):
    material_id: str
    material_type: str
    source: str
    content: str
    content_sha256: str
    applies_to_sku: str | None = None
    delivered_at: str | None = None
    created_by_staff_id: str
    created_at: str


class ApprovalMaterialsSnapshotView(BaseModel):
    revision: int
    items: list[ApprovalMaterialSnapshotView]


class ApprovalEvidenceSnapshotView(BaseModel):
    chunks: list[EvidenceView]
    policy_version: str | None = None
    evidence_sha256: str
    corpus_revision: str
    as_of: str | None = None


class ApprovalRequesterSnapshotView(BaseModel):
    staff_id: str
    roles: list[str]
    allowed_markets: list[str]


class ApprovalWindowSnapshotView(BaseModel):
    requested_at: str
    expires_at: str


class ApprovalDeliveryProofView(BaseModel):
    kind: Literal["shipment", "manual_verification"]
    shipment_id: str | None = None
    event_id: str | None = None
    delivered_at: str | None = None
    material_id: str | None = None
    material_type: str | None = None
    applies_to_sku: str | None = None
    source_description: str | None = None
    content_sha256: str | None = None
    recorded_by_staff_id: str | None = None


class ApprovalSnapshotView(BaseModel):
    """Read-only whitelist of the immutable approval snapshot.

    Execution keys, idempotency values, credentials and connector internals
    are intentionally not part of this view. Values come from the stored
    ``snapshot_json`` only, never from the current case/draft/order rows.
    """

    schema_version: int = 1
    case: ApprovalCaseSnapshotView
    order: ServiceOrder
    request: ApprovalRequestSnapshotView
    shipment: ServiceShipment | None = None
    delivery_proof: ApprovalDeliveryProofView | None = None
    materials: ApprovalMaterialsSnapshotView
    draft: ApprovalDraftSnapshotView
    evidence: ApprovalEvidenceSnapshotView
    trusted_categories: list[TrustedCategoryView]
    requester: ApprovalRequesterSnapshotView
    approval: ApprovalWindowSnapshotView


class ApprovalView(BaseModel):
    approval_id: str
    draft_id: str
    status: ApprovalStatus
    requester_staff_id: str
    decided_by_staff_id: str | None
    expires_at: datetime
    snapshot_sha256: str
    snapshot: ApprovalSnapshotView | None = None
    decision_reason: str | None
    created_at: datetime
    decided_at: datetime | None


class OperationView(BaseModel):
    operation_id: str
    approval_id: str
    draft_id: str
    idempotency_key: str
    status: OperationStatus
    attempt: int
    payload: dict[str, object]
    payload_sha256: str
    dispatched_at: datetime | None
    lease_owner: str | None
    lease_epoch: int
    lease_until: datetime | None
    next_attempt_at: datetime | None
    receipt: dict[str, object] | None
    last_error_code: str | None
    failure_reason: str | None
    lookup_count: int = 0
    reconciliation_state: str | None = None
    reconcile_started_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class EventView(BaseModel):
    seq: int
    event_type: str
    actor_staff_id: str | None
    old_state: str | None
    new_state: str | None
    entity_type: str | None
    entity_id: str | None
    reason: str | None
    created_at: datetime


class EventListResponse(BaseModel):
    items: list[EventView]
    after_seq: int
    limit: int
    has_more: bool


class CaseDetail(BaseModel):
    case_id: int
    market: AfterSalesMarket
    request_id: str
    order_id: str
    request_type: RequestType
    description: str
    state: CaseState
    revision: int
    material_revision: int
    remote_status: RequestStatus
    remote_ticket_id: str | None
    request_created_at: datetime
    request_updated_at: datetime
    attachments: list[AttachmentView]
    order: ServiceOrder | None
    order_queried_at: datetime | None
    current_draft: DraftView | None
    drafts: list[DraftView]
    materials: list[MaterialView]
    approvals: list[ApprovalView]
    operations: list[OperationView]
    created_at: datetime
    updated_at: datetime


class MaterialCreateRequest(_ForbidExtra):
    expected_revision: int = Field(ge=1)
    material_type: MaterialType
    source: str = Field(min_length=3, max_length=500)
    content: str = Field(min_length=1, max_length=5000)
    applies_to_sku: str | None = Field(default=None, min_length=1, max_length=128)
    delivered_at: datetime | None = None

    @model_validator(mode="after")
    def validate_delivery_fields(self) -> MaterialCreateRequest:
        structured = self.applies_to_sku is not None or self.delivered_at is not None
        if self.material_type == "delivery_verification":
            if not structured or self.applies_to_sku is None or self.delivered_at is None:
                raise ValueError("delivery_verification requires applies_to_sku and delivered_at")
        elif self.material_type == "manual_verification":
            if (self.applies_to_sku is None) != (self.delivered_at is None):
                raise ValueError(
                    "structured manual_verification requires both applies_to_sku and delivered_at"
                )
        elif structured:
            raise ValueError(
                "applies_to_sku/delivered_at are only valid for manual_verification "
                "or delivery_verification"
            )
        if self.delivered_at is not None:
            if self.delivered_at.tzinfo is None:
                raise ValueError("delivered_at must include a timezone")
            if as_utc(self.delivered_at) > utcnow():
                raise ValueError("delivered_at cannot be in the future")
        return self


class DraftCreateRequest(_ForbidExtra):
    expected_revision: int = Field(ge=1)
    action: TicketAction
    internal_summary: str = Field(min_length=1, max_length=2000)
    consumer_reply: str = Field(min_length=1, max_length=10000, pattern=r"\S")
    evidence_chunk_ids: list[str] = Field(default_factory=list, max_length=20)


class SuggestionRequest(_ForbidExtra):
    expected_revision: int = Field(ge=1)


class SubmitRequest(_ForbidExtra):
    expected_revision: int = Field(ge=1)


class ApproveRequest(_ForbidExtra):
    expected_revision: int = Field(ge=1)


class RejectRequest(_ForbidExtra):
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=2000, pattern=r"\S")


__all__ = [
    "ApprovalCaseSnapshotView",
    "ApprovalDeliveryProofView",
    "ApprovalDraftSnapshotView",
    "ApprovalEvidenceSnapshotView",
    "ApprovalMaterialSnapshotView",
    "ApprovalMaterialsSnapshotView",
    "ApprovalRequestSnapshotView",
    "ApprovalRequesterSnapshotView",
    "ApprovalSnapshotView",
    "ApprovalStatus",
    "ApprovalView",
    "ApprovalWindowSnapshotView",
    "ApproveRequest",
    "AttachmentFingerprintView",
    "AttachmentView",
    "CaseDetail",
    "CaseEvidenceResponse",
    "CaseListResponse",
    "CaseState",
    "CaseSummary",
    "CaseTicketResponse",
    "DraftCreateRequest",
    "DraftStatus",
    "DraftView",
    "EventListResponse",
    "EventView",
    "EvidenceView",
    "MaterialCreateRequest",
    "MaterialType",
    "MaterialView",
    "OperationStatus",
    "OperationView",
    "RejectRequest",
    "SubmitRequest",
    "SuggestionRequest",
    "SyncResponse",
    "TicketVerificationState",
    "TrustedCategoryView",
]
