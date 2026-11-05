"""SQLAlchemy ORM models."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.time import utcnow


class Base(DeclarativeBase):
    pass


class OrderRow(Base):
    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    total_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tracking_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    receiver_city: Mapped[str | None] = mapped_column(String(64), nullable=True)

    items: Mapped[list["OrderItemRow"]] = relationship(back_populates="order")


class OrderItemRow(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"))
    sku: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    customized: Mapped[bool] = mapped_column(default=False)

    order: Mapped[OrderRow] = relationship(back_populates="items")


class ProductRow(Base):
    __tablename__ = "products"

    sku: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(64), index=True)
    price_cents: Mapped[int] = mapped_column(Integer)
    inventory: Mapped[int] = mapped_column(Integer)
    tags: Mapped[str] = mapped_column(String(256), default="")
    description: Mapped[str] = mapped_column(Text, default="")


class RefundRow(Base):
    __tablename__ = "refunds"

    refund_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32))
    amount_cents: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReturnRow(Base):
    __tablename__ = "returns"

    return_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SessionRow(Base):
    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLogRow(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdempotencyRow(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    response_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AfterSalesStaffRow(Base):
    """Internal pilot staff credential.

    Only the SHA-256 digest of the bearer token is stored. Roles and markets
    are comma-separated allowlists read on every authenticated request so that
    revocation takes effect without caching.
    """

    __tablename__ = "after_sales_staff"

    staff_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    roles: Mapped[str] = mapped_column(String(128), default="")
    allowed_markets: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


ACTIVE_OPERATION_STATUSES = (
    "prepared",
    "dispatching",
    "awaiting_receipt",
    "retry_wait",
    "reconciliation_required",
)
_ACTIVE_OPERATION_SQL = (
    "status IN (" + ",".join(f"'{value}'" for value in ACTIVE_OPERATION_STATUSES) + ")"
)


class AfterSalesCaseRow(Base):
    """DTC-owned projection and internal workflow state for one E-pig request.

    Remote fields are a projection of E-pig. Internal state, revision, draft
    pointer and verified receipts are never reset by feed synchronisation.
    """

    __tablename__ = "after_sales_case"
    __table_args__ = (
        UniqueConstraint("market", "request_id", name="uq_after_sales_case_market_request"),
        Index("ix_after_sales_case_market_state_updated", "market", "state", "updated_at"),
        Index("ix_after_sales_case_market_order", "market", "order_id"),
    )

    case_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_type: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="processing")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    material_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_draft_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remote_status: Mapped[str] = mapped_column(String(32), nullable=False)
    remote_ticket_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Keep the contract's full decimal sequence as text. PostgreSQL INT4 would
    # silently narrow future sequence ranges.
    remote_sequence: Mapped[str] = mapped_column(String(64), nullable=False)
    remote_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attachments_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    order_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    order_queried_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AfterSalesSyncCursorRow(Base):
    __tablename__ = "after_sales_sync_cursor"

    market: Mapped[str] = mapped_column(String(8), primary_key=True)
    cursor: Mapped[str | None] = mapped_column(String(512), nullable=True)
    high_watermark: Mapped[str] = mapped_column(String(64), nullable=False, default="0")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AfterSalesMaterialRow(Base):
    """Append-only internal material record, separate from E-pig fields."""

    __tablename__ = "after_sales_material"
    __table_args__ = (Index("ix_after_sales_material_case_created", "case_id", "created_at"),)

    material_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("after_sales_case.case_id"), nullable=False)
    material_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    applies_to_sku: Mapped[str | None] = mapped_column(String(128), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_staff_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AfterSalesDraftRow(Base):
    __tablename__ = "after_sales_draft"
    __table_args__ = (
        UniqueConstraint("case_id", "version", name="uq_after_sales_draft_case_version"),
        Index("ix_after_sales_draft_case_status", "case_id", "status"),
    )

    draft_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("after_sales_case.case_id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    internal_summary: Mapped[str] = mapped_column(Text, nullable=False)
    consumer_reply: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    policy_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    order_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    material_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    category_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="editing")
    author_staff_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AfterSalesApprovalRow(Base):
    __tablename__ = "after_sales_approval"
    __table_args__ = (
        Index(
            "uq_after_sales_approval_pending_draft",
            "draft_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_after_sales_approval_case_status", "case_id", "status"),
    )

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("after_sales_case.case_id"), nullable=False)
    draft_id: Mapped[str] = mapped_column(ForeignKey("after_sales_draft.draft_id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    requester_staff_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_by_staff_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AfterSalesOperationRow(Base):
    """One approved business write. Retries reuse the same key and payload."""

    __tablename__ = "after_sales_operation"
    __table_args__ = (
        UniqueConstraint("approval_id", name="uq_after_sales_operation_approval"),
        UniqueConstraint("idempotency_key", name="uq_after_sales_operation_idempotency"),
        Index(
            "uq_after_sales_operation_active_case",
            "case_id",
            unique=True,
            sqlite_where=text(_ACTIVE_OPERATION_SQL),
            postgresql_where=text(_ACTIVE_OPERATION_SQL),
        ),
    )

    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("after_sales_case.case_id"), nullable=False)
    approval_id: Mapped[str] = mapped_column(
        ForeignKey("after_sales_approval.approval_id"), nullable=False
    )
    draft_id: Mapped[str] = mapped_column(ForeignKey("after_sales_draft.draft_id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="prepared")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    receipt_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AfterSalesEventRow(Base):
    """Append-only, case-local audit event with a monotonic sequence."""

    __tablename__ = "after_sales_event"
    __table_args__ = (
        UniqueConstraint("case_id", "seq", name="uq_after_sales_event_case_seq"),
        Index("ix_after_sales_event_case_seq", "case_id", "seq"),
    )

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("after_sales_case.case_id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_staff_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    old_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
