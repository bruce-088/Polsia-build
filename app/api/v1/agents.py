from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.crew_factory import AGENT_MAP
from app.api.deps import require_api_key
from app.core.database import get_db
from app.services import task_service

router = APIRouter(prefix="/api/v1/agents", tags=["agents"], dependencies=[Depends(require_api_key)])


class TriggerBody(BaseModel):
    task_title: str | None = None


@router.post("/{agent_type}/trigger", status_code=202)
async def trigger_agent(agent_type: str, body: TriggerBody | None = None, db: AsyncSession = Depends(get_db)):
    if agent_type not in task_service.VALID_AGENT_TYPES:
        raise HTTPException(status_code=422, detail=f"Unknown agent type: {agent_type}")

    title = (body.task_title if body else None) or f"Manually triggered {agent_type} run"
    task = await task_service.create_task(db, title=title, agent_type=agent_type, source="manual")

    # Imported here (not at module top) so tests can patch
    # celery_app.tasks.agent_tasks.run_agent_task.delay cleanly.
    from celery_app.tasks.agent_tasks import run_agent_task

    run_agent_task.delay(task.id)

    return {"status": "queued", "task_id": task.id}


@router.get("/status")
async def agent_status(db: AsyncSession = Depends(get_db)):
    statuses = []
    for agent_type in AGENT_MAP:
        summary = await task_service.get_tasks_today_summary(db)
        statuses.append(
            {
                "agent_type": agent_type,
                "last_run_at": None,
                "last_run_status": None,
                "tasks_today": summary["total"],
                "tasks_total": summary["total"],
            }
        )
    return statuses
