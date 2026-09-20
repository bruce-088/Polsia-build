from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ApprovalRequest(Base):
    """The Founder Inbox — a decision an agent isn't authorized to make on
    its own. Field shape aligned with the acqivo-company-os repo's
    approval_request.schema.json where practical. `payload` is a
    Polsia-specific addition beyond that schema: whatever's needed to
    actually execute the decision once approved (e.g. reply_to/subject/
    body for a pending email reply) — the OS schema is runtime-agnostic
    and doesn't need this, but a real runtime does."""

    __tablename__ = "approval_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tasks.id"))
    requested_by_agent: Mapped[str] = mapped_column(String(100), nullable=False)
    decision_type: Mapped[str] = mapped_column(String(100), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str] = mapped_column(Text(), nullable=False)
    evidence: Mapped[list | None] = mapped_column(JSON())
    options: Mapped[list | None] = mapped_column(JSON())
    recommended_option: Mapped[str | None] = mapped_column(String(100))
    cost_or_commitment: Mapped[str | None] = mapped_column(Text())
    payload: Mapped[dict | None] = mapped_column(JSON())
    status: Mapped[str] = mapped_column(String(50), server_default="pending")
    resolved_by: Mapped[str | None] = mapped_column(String(100))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
