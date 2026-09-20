"""add approval_requests (Founder Inbox)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("requested_by_agent", sa.String(100), nullable=False),
        sa.Column("decision_type", sa.String(100), nullable=False),
        sa.Column("risk_level", sa.String(20), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB()),
        sa.Column("options", postgresql.JSONB()),
        sa.Column("recommended_option", sa.String(100)),
        sa.Column("cost_or_commitment", sa.Text()),
        sa.Column("payload", postgresql.JSONB()),
        sa.Column("status", sa.String(50), server_default="pending"),
        sa.Column("resolved_by", sa.String(100)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("approval_requests")
