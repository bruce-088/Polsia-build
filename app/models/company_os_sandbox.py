"""Authoritative Stage 2 sandbox run, workflow state, and event evidence."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CompanyOSSandboxRun(Base):
    __tablename__ = "company_os_sandbox_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    company_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(50), nullable=False)
    input_manifest: Mapped[dict] = mapped_column(JSON(), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="created")
    event_log_path: Mapped[str | None] = mapped_column(String(512))
    event_log_sha256: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CompanyOSWorkflowInstance(Base):
    __tablename__ = "company_os_workflow_instances"
    __table_args__ = (
        CheckConstraint("version >= 0", name="ck_company_os_workflow_version"),
        UniqueConstraint(
            "sandbox_run_id",
            "workflow_id",
            "entity_type",
            "entity_id",
            name="uq_company_os_workflow_instance_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sandbox_run_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("company_os_sandbox_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_id: Mapped[str] = mapped_column(String(160), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(160), nullable=False)
    current_state: Mapped[str | None] = mapped_column(String(160))
    terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CompanyOSSandboxEvent(Base):
    __tablename__ = "company_os_sandbox_events"
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_company_os_sandbox_event_sequence"),
        CheckConstraint(
            "(native_decision IS NOT NULL AND native_failure IS NULL) OR "
            "(native_decision IS NULL AND native_failure IS NOT NULL)",
            name="ck_company_os_sandbox_native_evidence",
        ),
        UniqueConstraint(
            "sandbox_run_id", "event_id", name="uq_company_os_sandbox_event_id"
        ),
        UniqueConstraint(
            "sandbox_run_id", "sequence", name="uq_company_os_sandbox_event_sequence"
        ),
        UniqueConstraint(
            "sandbox_run_id",
            "idempotency_key",
            name="uq_company_os_sandbox_event_idempotency",
        ),
        UniqueConstraint(
            "workflow_instance_id",
            "terminal_key",
            name="uq_company_os_sandbox_terminal_action",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sandbox_run_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("company_os_sandbox_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_instance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("company_os_workflow_instances.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    primary_classification: Mapped[str] = mapped_column(String(100), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    result: Mapped[str] = mapped_column(String(100), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    state_before: Mapped[str | None] = mapped_column(String(160))
    state_after: Mapped[str | None] = mapped_column(String(160))
    external_side_effect: Mapped[bool] = mapped_column(Boolean, nullable=False)
    terminal_key: Mapped[str | None] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON(), nullable=False)
    native_decision: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    native_failure: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CompanyOSSandboxApproval(Base):
    """One founder decision bound to an immutable request event and workflow."""

    __tablename__ = "company_os_sandbox_approvals"
    __table_args__ = (
        Index(
            "uq_company_os_sandbox_approval_open_action",
            "workflow_instance_id", "action", unique=True,
            postgresql_where=text("resume_event_id IS NULL AND status IN ('pending', 'approved', 'modified')"),
            sqlite_where=text("resume_event_id IS NULL AND status IN ('pending', 'approved', 'modified')"),
        ),
        UniqueConstraint("sandbox_run_id", "decision_id", name="uq_company_os_sandbox_approval_decision"),
        UniqueConstraint("request_event_id", name="uq_company_os_sandbox_approval_request_event"),
        UniqueConstraint("resolution_event_id", name="uq_company_os_sandbox_approval_resolution_event"),
        UniqueConstraint("resume_event_id", name="uq_company_os_sandbox_approval_resume_event"),
        CheckConstraint("founder_minutes IS NULL OR founder_minutes >= 0", name="ck_company_os_sandbox_approval_minutes"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sandbox_run_id: Mapped[int] = mapped_column(Integer, ForeignKey("company_os_sandbox_runs.id", ondelete="CASCADE"), nullable=False)
    workflow_instance_id: Mapped[int] = mapped_column(Integer, ForeignKey("company_os_workflow_instances.id", ondelete="CASCADE"), nullable=False)
    request_event_id: Mapped[int] = mapped_column(Integer, ForeignKey("company_os_sandbox_events.id"), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(160), nullable=False)
    action: Mapped[str] = mapped_column(String(160), nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(JSON(), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    founder_id: Mapped[str | None] = mapped_column(String(160))
    founder_minutes: Mapped[float | None] = mapped_column()
    autonomy_class: Mapped[str | None] = mapped_column(String(2))
    corrected_decision: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    manual_evidence_ref: Mapped[str | None] = mapped_column(String(512))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_event_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("company_os_sandbox_events.id"))
    resolution_key: Mapped[str | None] = mapped_column(String(255))
    resume_event_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("company_os_sandbox_events.id"))
    resume_key: Mapped[str | None] = mapped_column(String(255))
