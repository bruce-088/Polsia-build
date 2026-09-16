from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Competitor(Base):
    __tablename__ = "competitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    website: Mapped[str | None] = mapped_column(String(512))
    pricing_info: Mapped[dict | None] = mapped_column(JSON())
    positioning: Mapped[str | None] = mapped_column(Text())
    strengths: Mapped[list | None] = mapped_column(JSON())
    weaknesses: Mapped[list | None] = mapped_column(JSON())
    last_researched: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
