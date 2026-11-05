"""after-sales staff credentials

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "after_sales_staff",
        sa.Column("staff_id", sa.String(64), primary_key=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("token_sha256", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("roles", sa.String(128), nullable=False, server_default=""),
        sa.Column("allowed_markets", sa.String(32), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_after_sales_staff_token_sha256",
        "after_sales_staff",
        ["token_sha256"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_after_sales_staff_token_sha256", table_name="after_sales_staff")
    op.drop_table("after_sales_staff")
