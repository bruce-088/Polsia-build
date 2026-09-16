from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class EmailCampaign(Base):
    __tablename__ = "email_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    goal: Mapped[str | None] = mapped_column(Text())
    target_segment: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(50), server_default="active")
    total_sent: Mapped[int] = mapped_column(Integer, server_default="0")
    total_opened: Mapped[int] = mapped_column(Integer, server_default="0")
    total_replied: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmailLog(Base):
    __tablename__ = "email_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prospect_id: Mapped[int] = mapped_column(Integer, ForeignKey("prospects.id"), nullable=False)
    campaign_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("email_campaigns.id"))
    sequence_step: Mapped[int] = mapped_column(Integer, server_default="1")
    subject: Mapped[str | None] = mapped_column(String(512))
    body: Mapped[str | None] = mapped_column(Text())
    sendgrid_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(50), server_default="sent")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    follow_up_due: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
