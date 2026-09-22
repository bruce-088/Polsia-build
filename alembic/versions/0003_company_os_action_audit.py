"""add append-only Company OS action audit

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_os_action_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id")),
        sa.Column("agent_run_id", sa.Integer(), sa.ForeignKey("agent_runs.id")),
        sa.Column("approval_request_id", sa.Integer(), sa.ForeignKey("approval_requests.id")),
        sa.Column("company_slug", sa.String(100), nullable=False),
        sa.Column("scenario_id", sa.String(100)),
        sa.Column("agent_type", sa.String(100), nullable=False),
        sa.Column("proposed_action", sa.String(255), nullable=False),
        sa.Column("decision_envelope", postgresql.JSONB(), nullable=False),
        sa.Column("policy_decision", postgresql.JSONB()),
        sa.Column("workflow_transition", postgresql.JSONB()),
        sa.Column("approval_state", sa.String(50), nullable=False),
        sa.Column("integration_state", postgresql.JSONB()),
        sa.Column("initial_execution_status", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "company_os_action_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "action_record_id",
            sa.Integer(),
            sa.ForeignKey("company_os_action_records.id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("execution_status", sa.String(50), nullable=False),
        sa.Column("approval_state", sa.String(50), nullable=False),
        sa.Column("details", sa.Text()),
        sa.Column("evidence", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("company_os_action_events")
    op.drop_table("company_os_action_records")
