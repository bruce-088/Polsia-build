"""add durable Stage 2 approval identity and lifecycle

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_os_sandbox_approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sandbox_run_id", sa.Integer(), sa.ForeignKey("company_os_sandbox_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workflow_instance_id", sa.Integer(), sa.ForeignKey("company_os_workflow_instances.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_event_id", sa.Integer(), sa.ForeignKey("company_os_sandbox_events.id"), nullable=False),
        sa.Column("decision_id", sa.String(160), nullable=False),
        sa.Column("action", sa.String(160), nullable=False),
        sa.Column("input_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("founder_id", sa.String(160)),
        sa.Column("founder_minutes", sa.Float()),
        sa.Column("autonomy_class", sa.String(2)),
        sa.Column("corrected_decision", postgresql.JSONB(none_as_null=True)),
        sa.Column("manual_evidence_ref", sa.String(512)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution_event_id", sa.Integer(), sa.ForeignKey("company_os_sandbox_events.id")),
        sa.Column("resolution_key", sa.String(255)),
        sa.Column("resume_event_id", sa.Integer(), sa.ForeignKey("company_os_sandbox_events.id")),
        sa.Column("resume_key", sa.String(255)),
        sa.UniqueConstraint("sandbox_run_id", "decision_id", name="uq_company_os_sandbox_approval_decision"),
        sa.UniqueConstraint("request_event_id", name="uq_company_os_sandbox_approval_request_event"),
        sa.UniqueConstraint("resolution_event_id", name="uq_company_os_sandbox_approval_resolution_event"),
        sa.UniqueConstraint("resume_event_id", name="uq_company_os_sandbox_approval_resume_event"),
        sa.CheckConstraint("founder_minutes IS NULL OR founder_minutes >= 0", name="ck_company_os_sandbox_approval_minutes"),
    )
    op.create_index(
        "uq_company_os_sandbox_approval_pending_action",
        "company_os_sandbox_approvals", ["workflow_instance_id", "action"],
        unique=True, postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("uq_company_os_sandbox_approval_pending_action", table_name="company_os_sandbox_approvals")
    op.drop_table("company_os_sandbox_approvals")
