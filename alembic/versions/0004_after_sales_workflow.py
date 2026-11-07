"""persistent after-sales workflow

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-06
"""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_ACTIVE_OPERATION_SQL = (
    "status IN ('prepared','dispatching','awaiting_receipt','retry_wait',"
    "'reconciliation_required')"
)


def upgrade() -> None:
    op.create_table(
        "after_sales_case",
        sa.Column("case_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("market", sa.String(8), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("order_id", sa.String(128), nullable=False),
        sa.Column("request_type", sa.String(32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("state", sa.String(32), nullable=False, server_default="processing"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("material_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_draft_id", sa.String(64), nullable=True),
        sa.Column("remote_status", sa.String(32), nullable=False),
        sa.Column("remote_ticket_id", sa.String(128), nullable=True),
        sa.Column("remote_sequence", sa.String(64), nullable=False),
        sa.Column("remote_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attachments_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("order_snapshot_json", sa.Text(), nullable=True),
        sa.Column("order_version", sa.String(128), nullable=True),
        sa.Column("order_queried_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("market", "request_id", name="uq_after_sales_case_market_request"),
    )
    op.create_index(
        "ix_after_sales_case_market_state_updated",
        "after_sales_case",
        ["market", "state", "updated_at"],
    )
    op.create_index(
        "ix_after_sales_case_market_order",
        "after_sales_case",
        ["market", "order_id"],
    )

    op.create_table(
        "after_sales_sync_cursor",
        sa.Column("market", sa.String(8), primary_key=True),
        sa.Column("cursor", sa.String(512), nullable=True),
        sa.Column("high_watermark", sa.String(64), nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "after_sales_material",
        sa.Column("material_id", sa.String(64), primary_key=True),
        sa.Column(
            "case_id",
            sa.Integer(),
            sa.ForeignKey("after_sales_case.case_id"),
            nullable=False,
        ),
        sa.Column("material_type", sa.String(32), nullable=False),
        sa.Column("source", sa.String(500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("applies_to_sku", sa.String(128), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_staff_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_after_sales_material_case_created",
        "after_sales_material",
        ["case_id", "created_at"],
    )

    op.create_table(
        "after_sales_draft",
        sa.Column("draft_id", sa.String(64), primary_key=True),
        sa.Column(
            "case_id",
            sa.Integer(),
            sa.ForeignKey("after_sales_case.case_id"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("internal_summary", sa.Text(), nullable=False),
        sa.Column("consumer_reply", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("evidence_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("policy_version", sa.String(128), nullable=True),
        sa.Column("order_version", sa.String(128), nullable=True),
        sa.Column("material_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("category_snapshot_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(32), nullable=False, server_default="editing"),
        sa.Column("author_staff_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("case_id", "version", name="uq_after_sales_draft_case_version"),
    )
    op.create_index(
        "ix_after_sales_draft_case_status",
        "after_sales_draft",
        ["case_id", "status"],
    )

    op.create_table(
        "after_sales_approval",
        sa.Column("approval_id", sa.String(64), primary_key=True),
        sa.Column(
            "case_id",
            sa.Integer(),
            sa.ForeignKey("after_sales_case.case_id"),
            nullable=False,
        ),
        sa.Column(
            "draft_id",
            sa.String(64),
            sa.ForeignKey("after_sales_draft.draft_id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("requester_staff_id", sa.String(64), nullable=False),
        sa.Column("decided_by_staff_id", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_after_sales_approval_pending_draft",
        "after_sales_approval",
        ["draft_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_after_sales_approval_case_status",
        "after_sales_approval",
        ["case_id", "status"],
    )

    op.create_table(
        "after_sales_operation",
        sa.Column("operation_id", sa.String(64), primary_key=True),
        sa.Column(
            "case_id",
            sa.Integer(),
            sa.ForeignKey("after_sales_case.case_id"),
            nullable=False,
        ),
        sa.Column(
            "approval_id",
            sa.String(64),
            sa.ForeignKey("after_sales_approval.approval_id"),
            nullable=False,
        ),
        sa.Column(
            "draft_id",
            sa.String(64),
            sa.ForeignKey("after_sales_draft.draft_id"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="prepared"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt_json", sa.Text(), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("failure_reason", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("approval_id", name="uq_after_sales_operation_approval"),
        sa.UniqueConstraint("idempotency_key", name="uq_after_sales_operation_idempotency"),
    )
    op.create_index(
        "uq_after_sales_operation_active_case",
        "after_sales_operation",
        ["case_id"],
        unique=True,
        sqlite_where=sa.text(_ACTIVE_OPERATION_SQL),
        postgresql_where=sa.text(_ACTIVE_OPERATION_SQL),
    )

    op.create_table(
        "after_sales_event",
        sa.Column("event_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "case_id",
            sa.Integer(),
            sa.ForeignKey("after_sales_case.case_id"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor_staff_id", sa.String(64), nullable=True),
        sa.Column("old_state", sa.String(32), nullable=True),
        sa.Column("new_state", sa.String(32), nullable=True),
        sa.Column("entity_type", sa.String(32), nullable=True),
        sa.Column("entity_id", sa.String(64), nullable=True),
        sa.Column("reason", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("case_id", "seq", name="uq_after_sales_event_case_seq"),
    )
    op.create_index(
        "ix_after_sales_event_case_seq",
        "after_sales_event",
        ["case_id", "seq"],
    )


def downgrade() -> None:
    for table, indexes in (
        (
            "after_sales_event",
            ["ix_after_sales_event_case_seq"],
        ),
        (
            "after_sales_operation",
            ["uq_after_sales_operation_active_case"],
        ),
        (
            "after_sales_approval",
            [
                "ix_after_sales_approval_case_status",
                "uq_after_sales_approval_pending_draft",
            ],
        ),
        ("after_sales_draft", ["ix_after_sales_draft_case_status"]),
        ("after_sales_material", ["ix_after_sales_material_case_created"]),
        ("after_sales_sync_cursor", []),
        (
            "after_sales_case",
            [
                "ix_after_sales_case_market_order",
                "ix_after_sales_case_market_state_updated",
            ],
        ),
    ):
        for index in indexes:
            op.drop_index(index, table_name=table)
        op.drop_table(table)
