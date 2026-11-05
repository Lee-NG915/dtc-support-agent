"""after-sales operation reconciliation counters

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-06

The worker needs a durable, bounded lookup counter and a product-7.6
reconciliation sub-state. These are additive operation fields; no second
queue or task table is introduced.
"""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "after_sales_operation",
        sa.Column("lookup_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "after_sales_operation",
        sa.Column("unknown_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "after_sales_operation",
        sa.Column("reconciliation_state", sa.String(32), nullable=True),
    )
    op.add_column(
        "after_sales_operation",
        sa.Column("reconcile_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("after_sales_operation", "reconcile_started_at")
    op.drop_column("after_sales_operation", "reconciliation_state")
    op.drop_column("after_sales_operation", "unknown_attempts")
    op.drop_column("after_sales_operation", "lookup_count")
