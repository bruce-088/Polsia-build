"""add ventures table for the venture lifecycle layer

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ventures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("idea", sa.Text(), nullable=False),
        sa.Column("target_customer", sa.Text()),
        sa.Column("business_model", sa.Text()),
        sa.Column("phase", sa.String(50), nullable=False, server_default="research_validation"),
        sa.Column("status", sa.String(50), nullable=False, server_default="exploring"),
        sa.Column("source", sa.String(100), nullable=False, server_default="founder"),
        sa.Column("metrics", postgresql.JSONB()),
        sa.Column("scores", postgresql.JSONB()),
        sa.Column("gate_overrides", postgresql.JSONB()),
        sa.Column("history", postgresql.JSONB()),
        sa.Column("company_config_id", sa.Integer(), sa.ForeignKey("company_config.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_ventures_status", "ventures", ["status"])


def downgrade() -> None:
    op.drop_index("ix_ventures_status", table_name="ventures")
    op.drop_table("ventures")
