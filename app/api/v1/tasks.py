from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_api_key
from app.core.database import get_db
from app.models.task import Task
from app.services import task_service

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"], dependencies=[Depends(require_api_key)])


class TaskCreate(BaseModel):
    title: str
    agent_type: str
    priority: int = 3


def _task_to_dict(task: Task) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "agent_type": task.agent_type,
        "priority": task.priority,
        "status": task.status,
        "source": task.source,
        "result_summary": task.result_summary,
        "error_message": task.error_message,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


@router.get("")
async def list_tasks(db: AsyncSession = Depends(get_db)):
    tasks = await task_service.list_tasks(db)
    return [_task_to_dict(t) for t in tasks]


@router.post("", status_code=201)
async def create_task(body: TaskCreate, db: AsyncSession = Depends(get_db)):
    task = await task_service.create_task(
        db, title=body.title, agent_type=body.agent_type, priority=body.priority
    )
    return _task_to_dict(task)


@router.get("/{task_id}")
async def get_task(task_id: int, db: AsyncSession = Depends(get_db)):
    task = await task_service.get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_to_dict(task)
