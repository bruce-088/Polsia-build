"""add Stage 2 sandbox run, workflow instance, and event persistence

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_os_sandbox_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(160), nullable=False, unique=True),
        sa.Column("company_slug", sa.String(100), nullable=False),
        sa.Column("protocol_version", sa.String(50), nullable=False),
        sa.Column("input_manifest", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("event_log_path", sa.String(512)),
        sa.Column("event_log_sha256", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "company_os_workflow_instances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sandbox_run_id",
            sa.Integer(),
            sa.ForeignKey("company_os_sandbox_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workflow_id", sa.String(160), nullable=False),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column("entity_id", sa.String(160), nullable=False),
        sa.Column("current_state", sa.String(160)),
        sa.Column("terminal", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("version >= 0", name="ck_company_os_workflow_version"),
        sa.UniqueConstraint(
            "sandbox_run_id",
            "workflow_id",
            "entity_type",
            "entity_id",
            name="uq_company_os_workflow_instance_identity",
        ),
    )
    op.create_table(
        "company_os_sandbox_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sandbox_run_id",
            sa.Integer(),
            sa.ForeignKey("company_os_sandbox_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workflow_instance_id",
            sa.Integer(),
            sa.ForeignKey("company_os_workflow_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_id", sa.String(160), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("primary_classification", sa.String(100), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("result", sa.String(100), nullable=False),
        sa.Column("risk_level", sa.String(20), nullable=False),
        sa.Column("state_before", sa.String(160)),
        sa.Column("state_after", sa.String(160)),
        sa.Column("external_side_effect", sa.Boolean(), nullable=False),
        sa.Column("terminal_key", sa.String(32)),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("native_decision", postgresql.JSONB(none_as_null=True)),
        sa.Column("native_failure", postgresql.JSONB(none_as_null=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "sequence >= 1", name="ck_company_os_sandbox_event_sequence"
        ),
        sa.CheckConstraint(
            "(native_decision IS NOT NULL AND native_failure IS NULL) OR "
            "(native_decision IS NULL AND native_failure IS NOT NULL)",
            name="ck_company_os_sandbox_native_evidence",
        ),
        sa.UniqueConstraint(
            "sandbox_run_id", "event_id", name="uq_company_os_sandbox_event_id"
        ),
        sa.UniqueConstraint(
            "sandbox_run_id", "sequence", name="uq_company_os_sandbox_event_sequence"
        ),
        sa.UniqueConstraint(
            "sandbox_run_id",
            "idempotency_key",
            name="uq_company_os_sandbox_event_idempotency",
        ),
        sa.UniqueConstraint(
            "workflow_instance_id",
            "terminal_key",
            name="uq_company_os_sandbox_terminal_action",
        ),
    )


def downgrade() -> None:
    op.drop_table("company_os_sandbox_events")
    op.drop_table("company_os_workflow_instances")
    op.drop_table("company_os_sandbox_runs")
