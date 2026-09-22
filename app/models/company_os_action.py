"""Append-only audit evidence for Company OS decisions and execution."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CompanyOSActionRecord(Base):
    __tablename__ = "company_os_action_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tasks.id"))
    agent_run_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("agent_runs.id"))
    approval_request_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("approval_requests.id")
    )
    company_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    scenario_id: Mapped[str | None] = mapped_column(String(100))
    agent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    proposed_action: Mapped[str] = mapped_column(String(255), nullable=False)
    decision_envelope: Mapped[dict] = mapped_column(JSON(), nullable=False)
    policy_decision: Mapped[dict | None] = mapped_column(JSON())
    workflow_transition: Mapped[dict | None] = mapped_column(JSON())
    approval_state: Mapped[str] = mapped_column(String(50), nullable=False)
    integration_state: Mapped[dict | None] = mapped_column(JSON())
    initial_execution_status: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CompanyOSActionEvent(Base):
    __tablename__ = "company_os_action_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_record_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("company_os_action_records.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    execution_status: Mapped[str] = mapped_column(String(50), nullable=False)
    approval_state: Mapped[str] = mapped_column(String(50), nullable=False)
    details: Mapped[str | None] = mapped_column(Text())
    evidence: Mapped[dict | list | None] = mapped_column(JSON())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
