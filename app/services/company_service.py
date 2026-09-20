from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import CompanyConfig
from app.models.report import DailyReport
from app.models.task import Task


async def get_full_context(db: AsyncSession) -> dict:
    result = await db.execute(select(CompanyConfig).limit(1))
    company = result.scalars().first()
    if company is None:
        return {}

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    tasks_result = await db.execute(select(Task).where(Task.created_at >= today_start))
    todays_tasks = [
        {"title": t.title, "agent_type": t.agent_type, "status": t.status}
        for t in tasks_result.scalars().all()
    ]

    report_result = await db.execute(select(DailyReport).order_by(DailyReport.report_date.desc()).limit(1))
    latest_report = report_result.scalars().first()

    return {
        "company": {
            "name": company.name,
            "mission": company.mission,
            "vision": company.vision,
            "description": company.description,
            "target_market": company.target_market,
            "value_prop": company.value_prop,
            "website_url": company.website_url,
            "industry": company.industry,
        },
        "kpis": company.kpis or {},
        "yesterday_summary": latest_report.evening_summary if latest_report else None,
        "todays_tasks": todays_tasks,
    }


def build_context_prompt(context: dict) -> str:
    if not context:
        return "No company context is configured yet. Operate using general best practices."

    company = context.get("company", {})
    lines = [
        f"Company: {company.get('name')}",
        f"Industry: {company.get('industry')}",
        f"Mission: {company.get('mission')}",
        f"Vision: {company.get('vision')}",
        f"Description: {company.get('description')}",
        f"Target market: {company.get('target_market')}",
        f"Value proposition: {company.get('value_prop')}",
        f"KPIs: {context.get('kpis', {})}",
        f"Yesterday: {context.get('yesterday_summary')}",
        "Today's tasks:",
    ]
    for task in context.get("todays_tasks", []):
        lines.append(f"  - {task.get('title')} ({task.get('agent_type')}, {task.get('status')})")

    return "\n".join(lines)
