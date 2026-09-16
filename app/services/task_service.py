from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.task import Task

VALID_AGENT_TYPES = {
    "orchestrator",
    "business_planning",
    "competitor_research",
    "social_media",
    "ads_management",
    "email_outreach",
    "customer_support",
    "code_generation",
    "finance",
}


async def create_task(
    db: AsyncSession,
    title: str,
    agent_type: str,
    priority: int = 3,
    source: str = "orchestrator",
    **kwargs,
) -> Task:
    task = Task(title=title, agent_type=agent_type, priority=priority, source=source, **kwargs)
    db.add(task)
    await db.flush()
    await db.refresh(task)
    return task


async def get_task(db: AsyncSession, task_id: int) -> Task | None:
    return await db.get(Task, task_id)


async def get_tasks_today_summary(db: AsyncSession) -> dict:
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    async def _count(status: str | None = None):
        stmt = select(func.count(Task.id)).where(Task.created_at >= today_start)
        if status is not None:
            stmt = stmt.where(Task.status == status)
        result = await db.execute(stmt)
        return result.scalar_one()

    return {
        "total": await _count(),
        "completed": await _count("completed"),
        "pending": await _count("pending"),
        "failed": await _count("failed"),
    }


async def list_tasks(db: AsyncSession, status: str | None = None) -> list[Task]:
    stmt = select(Task)
    if status is not None:
        stmt = stmt.where(Task.status == status)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_task_status(
    db: AsyncSession,
    task_id: int,
    status: str,
    result_summary: str | None = None,
    error_message: str | None = None,
) -> Task | None:
    task = await db.get(Task, task_id)
    if task is None:
        return None
    task.status = status
    if result_summary is not None:
        task.result_summary = result_summary
    if error_message is not None:
        task.error_message = error_message
    await db.flush()
    await db.refresh(task)
    return task


async def create_agent_run(
    db: AsyncSession,
    agent_type: str,
    task_id: int | None = None,
    input_context: dict | None = None,
) -> AgentRun:
    run = AgentRun(agent_type=agent_type, task_id=task_id, input_context=input_context)
    db.add(run)
    await db.flush()
    await db.refresh(run)
    return run


async def finish_agent_run(
    db: AsyncSession,
    run_id: int,
    status: str,
    output: dict | None = None,
    duration_secs: float | None = None,
) -> AgentRun | None:
    run = await db.get(AgentRun, run_id)
    if run is None:
        return None
    run.status = status
    run.output = output
    run.duration_secs = duration_secs
    run.ended_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(run)
    return run
