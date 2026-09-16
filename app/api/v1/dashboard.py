from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_api_key
from app.core.database import get_db
from app.services import activity_service, company_service, report_service, task_service

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"], dependencies=[Depends(require_api_key)])

health_router = APIRouter(prefix="/api/v1", tags=["health"])


@health_router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/summary")
async def summary(db: AsyncSession = Depends(get_db)):
    counts = await task_service.get_tasks_today_summary(db)
    context = await company_service.get_full_context(db)
    report = await report_service.get_daily_report(db, date.today())

    return {
        "tasks_today_total": counts["total"],
        "tasks_today_completed": counts["completed"],
        "tasks_today_pending": counts["pending"],
        "tasks_today_failed": counts["failed"],
        "active_agents": [],
        "kpis": context.get("kpis", {}),
        "last_report_date": report.report_date if report else None,
    }


@router.get("/activity")
async def activity(db: AsyncSession = Depends(get_db)):
    entries = await activity_service.get_recent_activity(db, limit=20)
    return [
        {
            "id": e.id,
            "agent_type": e.agent_type,
            "action": e.action,
            "summary": e.summary,
            "level": e.level,
            "created_at": e.created_at,
        }
        for e in entries
    ]


@router.get("/reports/daily")
async def daily_report(db: AsyncSession = Depends(get_db)):
    report = await report_service.get_daily_report(db, date.today())
    if report is None:
        return None
    return {
        "report_date": report.report_date,
        "morning_plan": report.morning_plan,
        "evening_summary": report.evening_summary,
        "tasks_planned": report.tasks_planned,
        "tasks_completed": report.tasks_completed,
        "tasks_failed": report.tasks_failed,
        "metrics_snapshot": report.metrics_snapshot,
        "insights": report.insights,
    }
