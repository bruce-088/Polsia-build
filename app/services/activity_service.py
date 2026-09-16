from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import publish_event
from app.models.report import ActivityLog


async def log_activity(
    db: AsyncSession,
    agent_type: str,
    action: str,
    summary: str,
    level: str = "info",
    detail: dict | None = None,
) -> ActivityLog:
    entry = ActivityLog(agent_type=agent_type, action=action, summary=summary, level=level, detail=detail)
    db.add(entry)
    await db.flush()
    await db.refresh(entry)

    await publish_event(
        "polsia:activity",
        {
            "agent_type": agent_type,
            "action": action,
            "summary": summary,
            "level": level,
        },
    )
    return entry


async def get_recent_activity(db: AsyncSession, limit: int = 10) -> list[ActivityLog]:
    stmt = select(ActivityLog).order_by(ActivityLog.id.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())
