from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text(), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON())
    level: Mapped[str] = mapped_column(String(20), server_default="info")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DailyReport(Base):
    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, unique=True, nullable=False)
    morning_plan: Mapped[str | None] = mapped_column(Text())
    evening_summary: Mapped[str | None] = mapped_column(Text())
    tasks_planned: Mapped[int] = mapped_column(Integer, server_default="0")
    tasks_completed: Mapped[int] = mapped_column(Integer, server_default="0")
    tasks_failed: Mapped[int] = mapped_column(Integer, server_default="0")
    metrics_snapshot: Mapped[dict | None] = mapped_column(JSON())
    insights: Mapped[list | None] = mapped_column(JSON())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
