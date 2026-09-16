from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tasks.id"))
    agent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    run_type: Mapped[str] = mapped_column(String(100), server_default="task")
    status: Mapped[str] = mapped_column(String(50), server_default="running")
    input_context: Mapped[dict | None] = mapped_column(JSON())
    output: Mapped[dict | None] = mapped_column(JSON())
    raw_log: Mapped[str | None] = mapped_column(Text())
    tokens_used: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    duration_secs: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
