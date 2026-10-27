from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Venture(Base):
    """A business idea moving through the venture lifecycle
    (research_validation → launch → marketing → scaling).

    JSON columns are always reassigned, never mutated in place, so SQLAlchemy
    sees the change:
      metrics         {name: {value, verified, source, evidence, recorded_at, recorded_by}}
      scores          {"idea": <lifecycle.score_idea result>, "forecast": {...}}
      gate_overrides  {phase: {metric: threshold}} — founder-set thresholds
      history         append-only list of {at, event, ...} entries
    """

    __tablename__ = "ventures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    idea: Mapped[str] = mapped_column(Text(), nullable=False)
    target_customer: Mapped[str | None] = mapped_column(Text())
    business_model: Mapped[str | None] = mapped_column(Text())
    phase: Mapped[str] = mapped_column(String(50), nullable=False, server_default="research_validation")
    status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="exploring")
    source: Mapped[str] = mapped_column(String(100), nullable=False, server_default="founder")
    metrics: Mapped[dict | None] = mapped_column(JSON())
    scores: Mapped[dict | None] = mapped_column(JSON())
    gate_overrides: Mapped[dict | None] = mapped_column(JSON())
    history: Mapped[list | None] = mapped_column(JSON())
    company_config_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("company_config.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
