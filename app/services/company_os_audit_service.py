"""Create immutable Company OS decisions and append-only lifecycle events."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company_os_action import CompanyOSActionEvent, CompanyOSActionRecord

INITIAL_STATUSES = {"not_attempted", "proposed", "blocked", "awaiting_approval", "authorized"}
APPROVAL_STATES = {
    "not_required", "pending", "approved", "rejected", "modified",
    "needs_more_evidence", "expired", "cancelled",
}
ALLOWED_TRANSITIONS = {
    "not_attempted": {"proposed", "cancelled"},
    "proposed": {"awaiting_approval", "authorized", "blocked", "cancelled"},
    "awaiting_approval": {"authorized", "blocked", "cancelled"},
    "authorized": {"executing", "cancelled"},
    "executing": {"succeeded", "failed"},
    "failed": {"authorized", "cancelled"},
    "blocked": set(),
    "succeeded": set(),
    "cancelled": set(),
}


class CompanyOSAuditError(ValueError):
    """Raised when an audit record would misstate the action lifecycle."""


async def create_action_record(
    db: AsyncSession,
    *,
    company_slug: str,
    agent_type: str,
    proposed_action: str,
    decision_envelope: dict,
    approval_state: str,
    initial_execution_status: str,
    policy_decision: dict | None = None,
    workflow_transition: dict | None = None,
    integration_state: dict | None = None,
    scenario_id: str | None = None,
    task_id: int | None = None,
    agent_run_id: int | None = None,
    approval_request_id: int | None = None,
) -> CompanyOSActionRecord:
    if not company_slug or not agent_type or not proposed_action:
        raise CompanyOSAuditError("company, agent, and proposed action are required")
    if not isinstance(decision_envelope, dict) or not decision_envelope:
        raise CompanyOSAuditError("the original decision envelope is required")
    if approval_state not in APPROVAL_STATES:
        raise CompanyOSAuditError(f"invalid approval state: {approval_state}")
    if initial_execution_status not in INITIAL_STATUSES:
        raise CompanyOSAuditError(
            f"invalid initial execution status: {initial_execution_status}"
        )
    if initial_execution_status == "authorized" and approval_state not in {
        "approved", "not_required"
    }:
        raise CompanyOSAuditError("authorization requires approved or unnecessary approval")

    record = CompanyOSActionRecord(
        company_slug=company_slug,
        agent_type=agent_type,
        proposed_action=proposed_action,
        decision_envelope=decision_envelope,
        policy_decision=policy_decision,
        workflow_transition=workflow_transition,
        approval_state=approval_state,
        integration_state=integration_state,
        initial_execution_status=initial_execution_status,
        scenario_id=scenario_id,
        task_id=task_id,
        agent_run_id=agent_run_id,
        approval_request_id=approval_request_id,
    )
    db.add(record)
    await db.flush()
    await db.refresh(record)
    return record


async def list_action_events(
    db: AsyncSession, action_record_id: int
) -> list[CompanyOSActionEvent]:
    result = await db.execute(
        select(CompanyOSActionEvent)
        .where(CompanyOSActionEvent.action_record_id == action_record_id)
        .order_by(CompanyOSActionEvent.id.asc())
    )
    return list(result.scalars().all())


async def append_action_event(
    db: AsyncSession,
    *,
    action_record_id: int,
    event_type: str,
    execution_status: str,
    approval_state: str,
    details: str | None = None,
    evidence: dict | list | None = None,
) -> CompanyOSActionEvent:
    record = await db.get(CompanyOSActionRecord, action_record_id)
    if record is None:
        raise CompanyOSAuditError("action record does not exist")
    if not event_type:
        raise CompanyOSAuditError("event type is required")
    if approval_state not in APPROVAL_STATES:
        raise CompanyOSAuditError(f"invalid approval state: {approval_state}")
    events = await list_action_events(db, action_record_id)
    previous = events[-1].execution_status if events else record.initial_execution_status
    if execution_status not in ALLOWED_TRANSITIONS.get(previous, set()):
        raise CompanyOSAuditError(
            f"invalid execution transition: {previous} -> {execution_status}"
        )
    if execution_status in {"authorized", "executing", "succeeded"} and approval_state not in {
        "approved", "not_required"
    }:
        raise CompanyOSAuditError(
            f"{execution_status} requires approved or unnecessary approval"
        )
    if execution_status == "succeeded" and evidence is None:
        raise CompanyOSAuditError("success requires execution evidence")
    if execution_status == "failed" and not details:
        raise CompanyOSAuditError("failure requires details")

    event = CompanyOSActionEvent(
        action_record_id=action_record_id,
        event_type=event_type,
        execution_status=execution_status,
        approval_state=approval_state,
        details=details,
        evidence=evidence,
    )
    db.add(event)
    await db.flush()
    await db.refresh(event)
    return event
