"""Venture lifecycle API — the founder's controls for Commander, Tracker,
Predictor and the phase leads.

Queued agent runs are enqueued as a background task, which FastAPI runs
after get_db() has committed, so a worker never picks up a task id that
isn't visible yet. Nothing here executes external actions.
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.crew_factory import VENTURE_AGENT_MAP
from app.agents.venture import lifecycle
from app.api.deps import require_api_key
from app.core.database import get_db
from app.services import task_service, venture_service
from app.services.venture_service import GateNotPassed, VentureConflict, VentureError

router = APIRouter(prefix="/api/v1/ventures", tags=["ventures"], dependencies=[Depends(require_api_key)])


def _enqueue(task_ids: list[int]) -> None:
    # Imported here (not at module top) so tests and Celery-less environments
    # can import this router; tests patch app.api.v1.ventures._enqueue.
    from celery_app.tasks.agent_tasks import run_agent_task

    for task_id in task_ids:
        run_agent_task.delay(task_id)


def _http_error(exc: VentureError) -> HTTPException:
    if isinstance(exc, GateNotPassed):
        return HTTPException(status_code=409, detail={"message": str(exc), "gate": exc.gate})
    if isinstance(exc, VentureConflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


async def _load(db: AsyncSession, venture_id: int):
    venture = await venture_service.get_venture(db, venture_id)
    if venture is None:
        raise HTTPException(status_code=404, detail="Venture not found")
    return venture


class VentureCreate(BaseModel):
    name: str
    idea: str
    target_customer: str | None = None
    business_model: str | None = None
    gate_overrides: dict | None = None


class IdeateBody(BaseModel):
    constraints: str = Field(min_length=1)
    count: int = Field(default=8, ge=1, le=10)


class MetricIn(BaseModel):
    metric: str
    value: float
    source: str | None = None
    evidence: str | None = None


class MetricsBody(BaseModel):
    metrics: list[MetricIn]
    verified: bool = True


class VerifyBody(BaseModel):
    metrics: list[str]


class RunBody(BaseModel):
    agent_type: str
    title: str | None = None
    description: str | None = None
    mode: str | None = None
    evidence: list | None = None


class AdvanceBody(BaseModel):
    approval_id: int


class StatusBody(BaseModel):
    status: str
    reason: str = ""


@router.get("/lifecycle")
async def lifecycle_definition():
    """Phases, gates, delegation rules and the idea rubric (read-only)."""
    return {
        "phases": [
            {
                "key": phase.key,
                "name": phase.name,
                "lead_agent": phase.lead_agent,
                "objective": phase.objective,
                "playbook": list(phase.playbook),
                "worker_agents": list(phase.worker_agents),
                "founder_decisions": list(phase.founder_decisions),
                "exit_gate": {
                    "name": phase.exit_gate_name,
                    "criteria": [c.as_dict() for c in phase.exit_gate],
                },
            }
            for phase in (lifecycle.get_phase(key) for key in lifecycle.PHASE_ORDER)
        ],
        "shared_agents": [lifecycle.COMMANDER, lifecycle.TRACKER, lifecycle.PREDICTOR],
        "idea_rubric": {
            name: {"weight": weight, "description": description}
            for name, (weight, description) in lifecycle.IDEA_RUBRIC.items()
        },
        "agents": sorted(VENTURE_AGENT_MAP),
    }


@router.get("")
async def list_ventures(status: str | None = None, db: AsyncSession = Depends(get_db)):
    return [venture_service.to_dict(v) for v in await venture_service.list_ventures(db, status=status)]


@router.post("", status_code=201)
async def create_venture(body: VentureCreate, db: AsyncSession = Depends(get_db)):
    try:
        venture = await venture_service.create_venture(db, **body.model_dump())
    except VentureError as exc:
        raise _http_error(exc) from exc
    return venture_service.to_dict(venture)


@router.post("/import/bbuilder", status_code=201)
async def import_bbuilder(brief: dict, db: AsyncSession = Depends(get_db)):
    """Import the Polsia company brief JSON that bbuilder (Mya) exports for a
    saved Business Idea. The venture starts in research_validation."""
    try:
        venture = await venture_service.import_bbuilder_brief(db, brief)
    except VentureError as exc:
        raise _http_error(exc) from exc
    return venture_service.to_dict(venture)


@router.post("/ideate", status_code=202)
async def ideate(body: IdeateBody, background: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    task = await venture_service.create_ideation_task(db, body.constraints, body.count)
    background.add_task(_enqueue, [task.id])
    return {"status": "queued", "task_id": task.id}


@router.post("/ideation/{task_id}/apply")
async def apply_ideation(task_id: int, db: AsyncSession = Depends(get_db)):
    task = await task_service.get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    output = await venture_service.latest_output(db, task_id)
    if output is None:
        raise HTTPException(status_code=409, detail="Task has no completed output yet")
    try:
        return await venture_service.apply_ideation_output(db, task, output)
    except VentureError as exc:
        raise _http_error(exc) from exc


@router.get("/{venture_id}")
async def get_venture(venture_id: int, db: AsyncSession = Depends(get_db)):
    return venture_service.to_dict(await _load(db, venture_id))


@router.get("/{venture_id}/gate")
async def get_gate(venture_id: int, db: AsyncSession = Depends(get_db)):
    return venture_service.gate_status(await _load(db, venture_id))


@router.post("/{venture_id}/metrics")
async def record_metrics(venture_id: int, body: MetricsBody, db: AsyncSession = Depends(get_db)):
    venture = await _load(db, venture_id)
    result = await venture_service.record_metrics(
        db,
        venture,
        [m.model_dump() for m in body.metrics],
        verified=body.verified,
        recorded_by="founder",
    )
    return {**result, "gate": venture_service.gate_status(venture)}


@router.post("/{venture_id}/metrics/verify")
async def verify_metrics(venture_id: int, body: VerifyBody, db: AsyncSession = Depends(get_db)):
    venture = await _load(db, venture_id)
    result = await venture_service.verify_metrics(db, venture, body.metrics)
    return {**result, "gate": venture_service.gate_status(venture)}


@router.post("/{venture_id}/run", status_code=202)
async def run_agent(
    venture_id: int, body: RunBody, background: BackgroundTasks, db: AsyncSession = Depends(get_db)
):
    venture = await _load(db, venture_id)
    try:
        task = await venture_service.create_venture_task(
            db,
            venture,
            body.agent_type,
            title=body.title,
            description=body.description,
            mode=body.mode,
            evidence=body.evidence,
            source="manual",
        )
    except VentureError as exc:
        raise _http_error(exc) from exc
    background.add_task(_enqueue, [task.id])
    return {"status": "queued", "task_id": task.id}


@router.post("/{venture_id}/tasks/{task_id}/apply")
async def apply_output(
    venture_id: int,
    task_id: int,
    background: BackgroundTasks,
    enqueue: bool = True,
    db: AsyncSession = Depends(get_db),
):
    """Apply a completed venture agent's proposal: create its delegated tasks
    (optionally queueing them), record Tracker metrics as unverified, store
    Predictor scores, and file founder decisions in the Founder Inbox."""
    venture = await _load(db, venture_id)
    task = await task_service.get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    output = await venture_service.latest_output(db, task_id)
    if output is None:
        raise HTTPException(status_code=409, detail="Task has no completed output yet")
    try:
        summary = await venture_service.apply_agent_output(db, venture, task, output)
    except VentureError as exc:
        raise _http_error(exc) from exc
    created = summary.get("created_task_ids") or []
    if enqueue and created and not summary.get("already_applied"):
        background.add_task(_enqueue, list(created))
    return {**summary, "queued": bool(enqueue and created and not summary.get("already_applied"))}


@router.post("/{venture_id}/advance/request", status_code=201)
async def request_advance(venture_id: int, db: AsyncSession = Depends(get_db)):
    venture = await _load(db, venture_id)
    try:
        request = await venture_service.request_phase_advance(db, venture)
    except VentureError as exc:
        raise _http_error(exc) from exc
    return {"approval_id": request.id, "status": request.status, "payload": request.payload}


@router.post("/{venture_id}/advance")
async def apply_advance(venture_id: int, body: AdvanceBody, db: AsyncSession = Depends(get_db)):
    venture = await _load(db, venture_id)
    try:
        venture = await venture_service.apply_phase_advance(db, venture, body.approval_id)
    except VentureError as exc:
        raise _http_error(exc) from exc
    return venture_service.to_dict(venture)


@router.post("/{venture_id}/status")
async def set_status(venture_id: int, body: StatusBody, db: AsyncSession = Depends(get_db)):
    venture = await _load(db, venture_id)
    try:
        venture = await venture_service.set_status(db, venture, body.status, body.reason)
    except VentureError as exc:
        raise _http_error(exc) from exc
    return venture_service.to_dict(venture)
