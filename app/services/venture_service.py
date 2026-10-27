"""Venture lifecycle service — the enforcement point for everything the
venture agents propose.

Agents only return proposals. This service decides what actually happens,
re-checking every rule against current state rather than trusting stored
agent output:
- only permitted delegates get tasks (Commander → phase lead/Tracker/
  Predictor; phase lead → that phase's workers);
- Tracker's metric updates are recorded unverified;
- a phase advances only after a RED founder approval *and* a fresh gate
  re-check on verified metrics;
- applying the same agent output twice is a no-op.

Like the rest of the services layer, nothing here commits (get_db and the
Celery tasks own the transaction) and nothing here calls Celery or any
external system.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.crew_factory import AGENT_MAP
from app.agents.venture import lifecycle
from app.models.agent_run import AgentRun
from app.models.approval import ApprovalRequest
from app.models.task import Task
from app.models.venture import Venture
from app.services import approval_service, task_service

ADVANCE_DECISION = "advance_venture_phase"
FOUNDER_DECISION = "venture_founder_decision"
MAX_FOUNDER_DECISIONS_PER_APPLY = 5
HISTORY_LIMIT = 200
FOUNDER_STATUSES = ("active", "paused", "killed", "graduated")


class VentureError(ValueError):
    """Invalid request against the venture lifecycle."""


class GateNotPassed(VentureError):
    def __init__(self, gate: dict):
        super().__init__(f"{gate['gate']} gate not passed")
        self.gate = gate


class VentureConflict(VentureError):
    """The request no longer matches the venture's current state."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_history(venture: Venture, event: str, **details) -> None:
    history = list(venture.history or [])
    history.append({"at": _now(), "event": event, **details})
    venture.history = history[-HISTORY_LIMIT:]


# ─── CRUD ─────────────────────────────────────────────────────────────────────


async def create_venture(
    db: AsyncSession,
    name: str,
    idea: str,
    target_customer: str | None = None,
    business_model: str | None = None,
    gate_overrides: dict | None = None,
    source: str = "founder",
    company_config_id: int | None = None,
) -> Venture:
    name = (name or "").strip()
    idea = (idea or "").strip()
    if not name or not idea:
        raise VentureError("name and idea are required")
    try:
        overrides = lifecycle.validate_gate_overrides(gate_overrides)
    except lifecycle.LifecycleError as exc:
        raise VentureError(str(exc)) from exc
    existing = await db.execute(select(Venture).where(Venture.name == name))
    if existing.scalars().first() is not None:
        raise VentureConflict(f"A venture named {name!r} already exists")
    venture = Venture(
        name=name,
        idea=idea,
        target_customer=target_customer,
        business_model=business_model,
        phase=lifecycle.PHASE_ORDER[0],
        status="exploring",
        source=source,
        metrics={},
        scores={},
        gate_overrides=overrides,
        history=[],
        company_config_id=company_config_id,
    )
    _append_history(venture, "created", source=source)
    db.add(venture)
    await db.flush()
    await db.refresh(venture)
    return venture


def _text(value: object, limit: int = 2000) -> str:
    return str(value).strip()[:limit] if isinstance(value, str | int | float) else ""


async def import_bbuilder_brief(db: AsyncSession, brief: dict) -> Venture:
    """Create an exploring venture from a bbuilder (Mya) Polsia company brief —
    the JSON that bbuilder's buildPolsiaBriefFromIdea / downloadPolsiaBrief
    produce. bbuilder's fit score is kept as an external heuristic only;
    Predictor's rubric score and the gates still decide everything."""
    if not isinstance(brief, dict):
        raise VentureError("brief must be a JSON object")
    name = _text(brief.get("name"), 255)
    lines = [
        _text(brief.get("description")),
        f"Value proposition: {_text(brief.get('value_prop'))}" if _text(brief.get("value_prop")) else "",
        f"Mission: {_text(brief.get('mission'))}" if _text(brief.get("mission")) else "",
        f"Product type: {_text(brief.get('product_type'), 100)}" if _text(brief.get("product_type")) else "",
        f"Industry: {_text(brief.get('industry'), 100)}" if _text(brief.get("industry")) else "",
    ]
    idea = "\n".join(line for line in lines if line)
    plans = (brief.get("pricing_model") or {}).get("plans") if isinstance(brief.get("pricing_model"), dict) else None
    business_model = ", ".join(
        f"{_text(plan.get('name'), 60) or 'Plan'} ${plan['price']:g}/mo"
        for plan in (plans if isinstance(plans, list) else [])
        if isinstance(plan, dict) and lifecycle.coerce_metric_value(plan.get("price")) is not None
    )
    venture = await create_venture(
        db,
        name=name,
        idea=idea,
        target_customer=_text(brief.get("target_market")) or None,
        business_model=business_model or None,
        source="bbuilder",
    )
    fit = brief.get("fit") if isinstance(brief.get("fit"), dict) else {}
    venture.scores = {
        "bbuilder_fit": {
            "score": lifecycle.coerce_metric_value(fit.get("score")),
            "signals": [s for s in fit.get("signals") or [] if isinstance(s, str)][:10],
            "warnings": [w for w in fit.get("warnings") or [] if isinstance(w, str)][:10],
            "note": "Keyword heuristic from bbuilder; not the lifecycle rubric.",
        }
    }
    _append_history(venture, "imported", origin=_text(brief.get("source"), 100) or "bbuilder")
    await db.flush()
    await db.refresh(venture)
    return venture


async def get_venture(db: AsyncSession, venture_id: int) -> Venture | None:
    return await db.get(Venture, venture_id)


async def list_ventures(db: AsyncSession, status: str | None = None) -> list[Venture]:
    stmt = select(Venture).order_by(Venture.id)
    if status is not None:
        stmt = stmt.where(Venture.status == status)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def set_status(db: AsyncSession, venture: Venture, status: str, reason: str = "") -> Venture:
    """Founder-only status change (the API key holder is the founder)."""
    if status not in FOUNDER_STATUSES:
        raise VentureError(f"status must be one of {FOUNDER_STATUSES}")
    if venture.status == status:
        return venture
    _append_history(venture, "status_changed", from_status=venture.status, to_status=status, reason=reason)
    venture.status = status
    await db.flush()
    await db.refresh(venture)
    return venture


# ─── Gate and snapshot ────────────────────────────────────────────────────────


def gate_status(venture: Venture) -> dict:
    overrides = (venture.gate_overrides or {}).get(venture.phase)
    return lifecycle.evaluate_gate(venture.phase, venture.metrics or {}, overrides)


def snapshot(venture: Venture) -> dict:
    """What a venture agent sees, embedded in its task metadata."""
    return {
        "id": venture.id,
        "name": venture.name,
        "idea": venture.idea,
        "target_customer": venture.target_customer,
        "business_model": venture.business_model,
        "phase": venture.phase,
        "status": venture.status,
        "metrics": venture.metrics or {},
        "scores": venture.scores or {},
        "gate_overrides": venture.gate_overrides or {},
        "history": (venture.history or [])[-5:],
        "gate": gate_status(venture),
    }


def to_dict(venture: Venture) -> dict:
    return {
        **snapshot(venture),
        "source": venture.source,
        "history": venture.history or [],
        "created_at": venture.created_at,
        "updated_at": venture.updated_at,
    }


# ─── Metrics ──────────────────────────────────────────────────────────────────


async def record_metrics(
    db: AsyncSession,
    venture: Venture,
    updates: list[dict],
    *,
    verified: bool,
    recorded_by: str,
) -> dict:
    """Record metric values. `verified` is decided by the caller's authority
    (founder API = True, Tracker = False), never by the update itself."""
    metrics = dict(venture.metrics or {})
    accepted, rejected = [], []
    for item in updates or []:
        if not isinstance(item, dict):
            rejected.append({"item": item, "reason": "not an object"})
            continue
        name = item.get("metric")
        value = lifecycle.coerce_metric_value(item.get("value"))
        if not lifecycle.is_valid_metric_name(name):
            rejected.append({**item, "reason": "invalid metric name"})
            continue
        if value is None:
            rejected.append({**item, "reason": "value is not a finite number"})
            continue
        source = str(item.get("source") or "").strip()
        if not verified and not source:
            rejected.append({**item, "reason": "unverified updates must cite a source"})
            continue
        metrics[name] = {
            "value": value,
            "verified": verified,
            "source": source or recorded_by,
            "evidence": str(item.get("evidence") or "")[:1000],
            "recorded_at": _now(),
            "recorded_by": recorded_by,
        }
        accepted.append(name)
    if accepted:
        venture.metrics = metrics
        _append_history(venture, "metrics_recorded", metrics=accepted, verified=verified, by=recorded_by)
        await db.flush()
        await db.refresh(venture)
    return {"accepted": accepted, "rejected": rejected}


async def verify_metrics(db: AsyncSession, venture: Venture, names: list[str], verified_by: str = "founder") -> dict:
    metrics = dict(venture.metrics or {})
    verified, unknown = [], []
    for name in names or []:
        entry = metrics.get(name)
        if not isinstance(entry, dict):
            unknown.append(name)
            continue
        metrics[name] = {**entry, "verified": True, "verified_by": verified_by, "verified_at": _now()}
        verified.append(name)
    if verified:
        venture.metrics = metrics
        _append_history(venture, "metrics_verified", metrics=verified, by=verified_by)
        await db.flush()
        await db.refresh(venture)
    return {"verified": verified, "unknown": unknown}


# ─── Tasks ────────────────────────────────────────────────────────────────────


def runnable_agents(venture: Venture) -> tuple[str, ...]:
    """Venture agents the founder (or a scheduler) may start directly."""
    return (lifecycle.COMMANDER, lifecycle.TRACKER, lifecycle.PREDICTOR, lifecycle.get_phase(venture.phase).lead_agent)


def _worker_description(venture: Venture, description: str) -> str:
    return (
        f"[Venture: {venture.name} — phase {lifecycle.get_phase(venture.phase).name}]\n"
        f"Idea: {venture.idea}\n"
        f"Target customer: {venture.target_customer or 'not yet defined'}\n"
        "This task is for the venture above, which is separate from the configured "
        "company; treat company context as background only. Propose and draft only; "
        "do not send, publish, spend, or deploy.\n\n"
        f"{description}"
    ).strip()


async def create_venture_task(
    db: AsyncSession,
    venture: Venture,
    agent_type: str,
    title: str | None = None,
    description: str | None = None,
    mode: str | None = None,
    evidence: list | None = None,
    source: str = "venture",
    parent_task_id: int | None = None,
    *,
    allowed: tuple[str, ...] | None = None,
) -> Task:
    """Create a task for a venture agent (snapshot in metadata) or, when
    `allowed` lists worker agents, for an existing operating agent (venture
    brief prepended to the description)."""
    if venture.status in ("killed", "graduated"):
        raise VentureConflict(f"Venture is {venture.status}; no new work can be started")
    permitted = allowed if allowed is not None else runnable_agents(venture)
    if agent_type not in permitted:
        raise VentureError(f"{agent_type!r} cannot be run for a venture in {venture.phase}; allowed: {list(permitted)}")
    metadata = {"venture_id": venture.id, "venture_phase": venture.phase}
    if parent_task_id is not None:
        metadata["parent_task_id"] = parent_task_id
    if agent_type in AGENT_MAP:
        task_description = _worker_description(venture, description or "")
    else:
        task_description = description
        metadata["venture"] = snapshot(venture)
        if mode:
            metadata["mode"] = mode
        if evidence:
            metadata["evidence"] = evidence
    return await task_service.create_task(
        db,
        title=(title or f"{agent_type.replace('_', ' ').title()} — {venture.name}")[:512],
        agent_type=agent_type,
        source=source,
        description=task_description,
        task_metadata=metadata,
    )


async def create_ideation_task(db: AsyncSession, constraints: str, count: int = 8) -> Task:
    count = max(1, min(int(count), 10))
    return await task_service.create_task(
        db,
        title=f"Generate {count} venture ideas",
        agent_type="research_validation",
        source="venture",
        description=f"Generate {count} ideas.\nFounder constraints:\n{constraints.strip()}",
        task_metadata={"mode": "ideate", "idea_count": count},
    )


async def latest_output(db: AsyncSession, task_id: int) -> dict | None:
    stmt = (
        select(AgentRun)
        .where(AgentRun.task_id == task_id, AgentRun.status == "completed")
        .order_by(AgentRun.id.desc())
        .limit(1)
    )
    run = (await db.execute(stmt)).scalars().first()
    return run.output if run is not None else None


# ─── Applying agent output ────────────────────────────────────────────────────


async def _mark_applied(db: AsyncSession, task: Task, summary: dict) -> None:
    task.task_metadata = {**(task.task_metadata or {}), "applied_at": _now(), "applied": summary}
    await db.flush()


async def _founder_decisions(db: AsyncSession, venture: Venture, task: Task, output: dict) -> list[int]:
    ids = []
    for decision in (output.get("founder_decisions") or [])[:MAX_FOUNDER_DECISIONS_PER_APPLY]:
        if not isinstance(decision, dict) or not decision.get("decision"):
            continue
        if str(decision["decision"]).lower().startswith("advance to"):
            continue  # advancement has its own gated path: request_phase_advance
        request = await approval_service.create_approval_request(
            db,
            task_id=task.id,
            requested_by_agent=task.agent_type,
            decision_type=FOUNDER_DECISION,
            risk_level="RED",
            title=f"[{venture.name}] {decision['decision']}"[:512],
            summary=(
                f"{decision.get('why') or 'Raised by ' + task.agent_type}. "
                "Approving records your decision only; nothing executes automatically."
            ),
            evidence=[output.get("summary") or ""],
            options=[{"id": "approve", "label": "Approve"}, {"id": "reject", "label": "Reject"}],
            payload={"venture_id": venture.id, "phase": venture.phase},
        )
        ids.append(request.id)
    return ids


async def apply_agent_output(db: AsyncSession, venture: Venture, task: Task, output: dict) -> dict:
    """Turn a completed venture-agent proposal into concrete, permitted work."""
    metadata = task.task_metadata or {}
    if metadata.get("venture_id") != venture.id:
        raise VentureError("Task does not belong to this venture")
    if metadata.get("applied_at"):
        return {"already_applied": True, **(metadata.get("applied") or {})}
    phase_independent = (lifecycle.TRACKER, lifecycle.PREDICTOR)
    if metadata.get("venture_phase") != venture.phase and task.agent_type not in phase_independent:
        raise VentureConflict(
            f"Output was produced in phase {metadata.get('venture_phase')!r}; venture is now in {venture.phase!r}"
        )

    summary: dict = {"agent_type": task.agent_type, "created_task_ids": [], "rejected": []}
    agent_type = task.agent_type
    if agent_type == lifecycle.COMMANDER:
        allowed = lifecycle.commander_dispatchable(venture.phase)
        for item in output.get("dispatch") or []:
            try:
                created = await create_venture_task(
                    db,
                    venture,
                    item.get("agent_type"),
                    title=item.get("title"),
                    description=item.get("description"),
                    mode=item.get("mode"),
                    source="commander",
                    parent_task_id=task.id,
                    allowed=allowed,
                )
                summary["created_task_ids"].append(created.id)
            except VentureError as exc:
                summary["rejected"].append({"item": item, "reason": str(exc)})
    elif agent_type == lifecycle.get_phase(venture.phase).lead_agent:
        allowed = lifecycle.lead_dispatchable(venture.phase)
        for item in output.get("proposed_tasks") or []:
            try:
                created = await create_venture_task(
                    db,
                    venture,
                    item.get("agent_type"),
                    title=item.get("title"),
                    description=item.get("description"),
                    source=agent_type,
                    parent_task_id=task.id,
                    allowed=allowed,
                )
                summary["created_task_ids"].append(created.id)
            except VentureError as exc:
                summary["rejected"].append({"item": item, "reason": str(exc)})
    elif agent_type == lifecycle.TRACKER:
        recorded = await record_metrics(
            db, venture, output.get("metric_updates") or [], verified=False, recorded_by=f"tracker:task-{task.id}"
        )
        summary["metrics_recorded"] = recorded["accepted"]
        summary["rejected"] = recorded["rejected"]
    elif agent_type == lifecycle.PREDICTOR:
        scores = dict(venture.scores or {})
        if output.get("mode") == "score_idea" and isinstance(output.get("idea_score"), dict):
            scores["idea"] = {**output["idea_score"], "task_id": task.id, "scored_at": _now()}
            summary["idea_score"] = output["idea_score"].get("weighted_score")
        elif output.get("mode") == "forecast":
            scores["forecast"] = {
                key: output.get(key)
                for key in ("gate", "gate_pass_probability", "horizon_days", "drivers", "leading_indicators", "confidence")
            } | {"task_id": task.id, "forecast_at": _now()}
            summary["gate_pass_probability"] = output.get("gate_pass_probability")
        venture.scores = scores
        _append_history(venture, "scores_updated", task_id=task.id, mode=output.get("mode"))
    else:
        raise VentureError(f"Output from {agent_type!r} cannot be applied to a venture in {venture.phase}")

    summary["founder_approval_ids"] = await _founder_decisions(db, venture, task, output)
    if venture.status == "exploring" and summary["created_task_ids"]:
        venture.status = "active"
        _append_history(venture, "status_changed", from_status="exploring", to_status="active", reason="work started")
    _append_history(venture, "output_applied", task_id=task.id, agent_type=agent_type)
    await _mark_applied(db, task, summary)
    await db.refresh(venture)
    return summary


async def apply_ideation_output(db: AsyncSession, task: Task, output: dict) -> dict:
    """Create exploring ventures from a research_validation ideation run.
    Creating internal records is GREEN; nothing external happens."""
    metadata = task.task_metadata or {}
    if task.agent_type != "research_validation" or metadata.get("mode") != "ideate":
        raise VentureError("Task is not an ideation run")
    if metadata.get("applied_at"):
        return {"already_applied": True, **(metadata.get("applied") or {})}
    created, skipped = [], []
    for idea in output.get("ideas") or []:
        if not isinstance(idea, dict):
            continue
        description = idea.get("one_liner") or ""
        detail = "\n".join(
            f"{label}: {idea[key]}"
            for key, label in (
                ("agent_fulfillment", "Agent fulfillment"),
                ("acquisition_channel", "Acquisition channel"),
                ("smoke_test", "Smoke test"),
                ("riskiest_assumption", "Riskiest assumption"),
            )
            if idea.get(key)
        )
        try:
            venture = await create_venture(
                db,
                name=idea.get("name") or "",
                idea=f"{description}\n{detail}".strip(),
                target_customer=idea.get("target_customer") or None,
                business_model=idea.get("business_model") or None,
                source=f"ideation:task-{task.id}",
            )
            created.append(venture.id)
        except VentureError as exc:
            skipped.append({"name": idea.get("name"), "reason": str(exc)})
    summary = {"created_venture_ids": created, "skipped": skipped}
    await _mark_applied(db, task, summary)
    return summary


# ─── Phase advancement (RED) ──────────────────────────────────────────────────


async def _pending_advance(db: AsyncSession, venture: Venture) -> ApprovalRequest | None:
    for request in await approval_service.list_approval_requests(db, status="pending"):
        payload = request.payload or {}
        if (
            request.decision_type == ADVANCE_DECISION
            and payload.get("venture_id") == venture.id
            and payload.get("from_phase") == venture.phase
        ):
            return request
    return None


async def request_phase_advance(
    db: AsyncSession, venture: Venture, requested_by: str = "founder", task_id: int | None = None
) -> ApprovalRequest:
    """Open a RED Founder Inbox request to advance — only if the gate passes
    right now. Idempotent while a request for this transition is pending."""
    if venture.status in ("paused", "killed", "graduated"):
        raise VentureConflict(f"Venture is {venture.status}")
    gate = gate_status(venture)
    if not gate["passed"]:
        raise GateNotPassed(gate)
    to_phase = gate["next_phase"]
    if to_phase is None:
        raise VentureConflict("Venture is in the final phase; use status 'graduated' for disposition")
    existing = await _pending_advance(db, venture)
    if existing is not None:
        return existing
    evidence = [
        f"{c['metric']} = {c['value']:g} ({c['op']} {c['threshold']:g}) — verified, source: "
        f"{(venture.metrics or {}).get(c['metric'], {}).get('source', 'unknown')}"
        for c in gate["criteria"]
    ]
    request = await approval_service.create_approval_request(
        db,
        task_id=task_id,
        requested_by_agent=requested_by,
        decision_type=ADVANCE_DECISION,
        risk_level="RED",
        title=f"[{venture.name}] Advance from {lifecycle.get_phase(venture.phase).name} to "
        f"{lifecycle.get_phase(to_phase).name}",
        summary=(
            f"The {gate['gate']} gate passed on verified metrics. Approving lets the venture "
            f"enter {lifecycle.get_phase(to_phase).name}; the gate is re-checked when applied."
        ),
        evidence=evidence,
        options=[{"id": "approve", "label": "Advance"}, {"id": "reject", "label": "Stay in phase"}],
        recommended_option="approve",
        cost_or_commitment=f"Unlocks {lifecycle.get_phase(to_phase).name} work and its worker agents.",
        payload={"venture_id": venture.id, "from_phase": venture.phase, "to_phase": to_phase},
    )
    _append_history(venture, "advance_requested", approval_id=request.id, to_phase=to_phase)
    await db.flush()
    return request


async def apply_phase_advance(db: AsyncSession, venture: Venture, approval_id: int) -> Venture:
    """Apply a founder-approved advance after re-checking state and gate."""
    request = await approval_service.get_approval_request(db, approval_id)
    if request is None or request.decision_type != ADVANCE_DECISION:
        raise VentureError("No phase-advance approval with that id")
    payload = request.payload or {}
    if payload.get("venture_id") != venture.id:
        raise VentureError("Approval belongs to a different venture")
    applied = [
        entry for entry in venture.history or []
        if entry.get("event") == "phase_advanced" and entry.get("approval_id") == approval_id
    ]
    if applied:
        return venture  # idempotent replay
    if request.status != "approved":
        raise VentureConflict(f"Approval is {request.status}, not approved")
    if venture.phase != payload.get("from_phase"):
        raise VentureConflict(f"Venture is in {venture.phase}, approval was for {payload.get('from_phase')}")
    if venture.status in ("paused", "killed", "graduated"):
        raise VentureConflict(f"Venture is {venture.status}")
    gate = gate_status(venture)
    if not gate["passed"]:
        raise GateNotPassed(gate)
    if gate["next_phase"] != payload.get("to_phase"):
        raise VentureConflict("Approved target phase no longer matches the lifecycle")
    _append_history(
        venture,
        "phase_advanced",
        approval_id=approval_id,
        from_phase=venture.phase,
        to_phase=payload["to_phase"],
        gate=gate["gate"],
    )
    venture.phase = payload["to_phase"]
    venture.status = "active"
    await db.flush()
    await db.refresh(venture)
    return venture
