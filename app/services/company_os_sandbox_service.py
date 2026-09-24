"""Append-only persistence for Company OS Stage 2 sandbox evidence."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.company_os_workflow import (
    CompanyOSWorkflowError,
    require_valid_transition,
    validate_workflow_definition,
)
from app.models.company_os_sandbox import (
    CompanyOSSandboxEvent,
    CompanyOSSandboxRun,
    CompanyOSWorkflowInstance,
)


class SandboxPersistenceError(ValueError):
    """The requested sandbox persistence operation is invalid."""


class SandboxConflictError(SandboxPersistenceError):
    """The operation conflicts with already-persisted sandbox state."""


async def create_sandbox_run(
    db: AsyncSession,
    *,
    run_id: str,
    company_slug: str,
    protocol_version: str,
    input_manifest: dict[str, Any],
) -> CompanyOSSandboxRun:
    """Create a run record without committing the caller-owned transaction."""
    if not run_id or not company_slug or not protocol_version:
        raise SandboxPersistenceError(
            "run_id, company_slug, and protocol_version are required"
        )

    existing = await db.scalar(
        select(CompanyOSSandboxRun.id).where(CompanyOSSandboxRun.run_id == run_id)
    )
    if existing is not None:
        raise SandboxConflictError(f"sandbox run already exists: {run_id}")

    run = CompanyOSSandboxRun(
        run_id=run_id,
        company_slug=company_slug,
        protocol_version=protocol_version,
        input_manifest=deepcopy(input_manifest),
        status="created",
    )
    db.add(run)
    await db.flush()
    return run


async def create_workflow_instance(
    db: AsyncSession,
    *,
    sandbox_run_id: int,
    workflow_id: str,
    entity_type: str,
    entity_id: str,
    initial_state: str | None,
) -> CompanyOSWorkflowInstance:
    """Create one authoritative workflow identity within a sandbox run."""
    if not workflow_id or not entity_type or not entity_id:
        raise SandboxPersistenceError(
            "workflow_id, entity_type, and entity_id are required"
        )
    run = await db.get(CompanyOSSandboxRun, sandbox_run_id)
    if run is None:
        raise SandboxPersistenceError("sandbox run does not exist")

    existing = await db.scalar(
        select(CompanyOSWorkflowInstance.id).where(
            CompanyOSWorkflowInstance.sandbox_run_id == sandbox_run_id,
            CompanyOSWorkflowInstance.workflow_id == workflow_id,
            CompanyOSWorkflowInstance.entity_type == entity_type,
            CompanyOSWorkflowInstance.entity_id == entity_id,
        )
    )
    if existing is not None:
        raise SandboxConflictError("workflow instance identity already exists")

    instance = CompanyOSWorkflowInstance(
        sandbox_run_id=sandbox_run_id,
        workflow_id=workflow_id,
        entity_type=entity_type,
        entity_id=entity_id,
        current_state=initial_state,
        terminal=False,
        version=0,
    )
    db.add(instance)
    await db.flush()
    return instance


async def append_sandbox_event(
    db: AsyncSession,
    *,
    workflow_instance_id: int,
    expected_version: int,
    idempotency_key: str,
    event_payload: dict[str, Any],
    event_schema: dict[str, Any],
    workflow_definition: dict[str, Any] | None = None,
    native_decision: dict[str, Any] | None = None,
    native_failure: dict[str, Any] | None = None,
) -> CompanyOSSandboxEvent:
    """Validate and append one event, atomically applying its state transition.

    The caller owns the surrounding transaction. PostgreSQL row locks serialize
    sequence allocation and workflow mutation; database uniqueness constraints
    remain the final defense against concurrent duplicate delivery.
    """
    if not idempotency_key:
        raise SandboxPersistenceError("idempotency_key is required")
    _require_one_native_evidence(native_decision, native_failure)

    instance = await db.scalar(
        select(CompanyOSWorkflowInstance)
        .where(CompanyOSWorkflowInstance.id == workflow_instance_id)
        .with_for_update()
    )
    if instance is None:
        raise SandboxPersistenceError("workflow instance does not exist")

    run = await db.scalar(
        select(CompanyOSSandboxRun)
        .where(CompanyOSSandboxRun.id == instance.sandbox_run_id)
        .with_for_update()
    )
    if run is None:
        raise SandboxPersistenceError("sandbox run does not exist")

    if instance.version != expected_version:
        raise SandboxConflictError(
            f"stale workflow version: expected {expected_version}, "
            f"current {instance.version}"
        )

    payload = deepcopy(event_payload)
    _require_bound_identity(payload, "run_id", run.run_id)
    _require_bound_identity(payload, "company_slug", run.company_slug)
    _require_bound_identity(payload, "workflow_id", instance.workflow_id)
    _require_bound_identity(payload, "entity_type", instance.entity_type)
    _require_bound_identity(payload, "entity_id", instance.entity_id)

    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise SandboxPersistenceError("event_id is required")

    duplicate = await db.scalar(
        select(CompanyOSSandboxEvent.id).where(
            CompanyOSSandboxEvent.sandbox_run_id == run.id,
            (
                (CompanyOSSandboxEvent.event_id == event_id)
                | (CompanyOSSandboxEvent.idempotency_key == idempotency_key)
            ),
        )
    )
    if duplicate is not None:
        raise SandboxConflictError("duplicate event_id or idempotency_key")

    next_sequence = (
        await db.scalar(
            select(func.max(CompanyOSSandboxEvent.sequence)).where(
                CompanyOSSandboxEvent.sandbox_run_id == run.id
            )
        )
        or 0
    ) + 1
    _require_bound_identity(payload, "sequence", next_sequence)
    payload["run_id"] = run.run_id
    payload["company_slug"] = run.company_slug
    payload["sequence"] = next_sequence

    state_before = payload.get("state_before")
    state_after = payload.get("state_after")
    if state_before != instance.current_state:
        raise SandboxConflictError(
            "event state_before does not match authoritative workflow state"
        )

    changes_state = state_after != state_before
    is_terminal = payload.get("event_type") == "terminal_outcome"
    if instance.terminal and (changes_state or is_terminal):
        raise SandboxConflictError("terminal workflow instance cannot transition")
    if changes_state or is_terminal:
        if workflow_definition is None:
            raise SandboxPersistenceError(
                "workflow_definition is required for a state transition or terminal event"
            )
        if workflow_definition.get("id") != instance.workflow_id:
            raise SandboxPersistenceError(
                "workflow definition does not match workflow instance"
            )
        try:
            validate_workflow_definition(workflow_definition)
            if changes_state:
                require_valid_transition(
                    workflow_definition,
                    current_state=state_before,
                    action=payload.get("action"),
                    proposed_state=state_after,
                    proposed_risk=payload.get("risk_level"),
                )
            if is_terminal and state_after not in set(
                workflow_definition.get("terminal_states", [])
            ):
                raise CompanyOSWorkflowError(
                    f"terminal outcome requires a canonical terminal state: {state_after!r}"
                )
        except CompanyOSWorkflowError as exc:
            raise SandboxPersistenceError(f"invalid workflow transition: {exc}") from exc

    try:
        Draft202012Validator.check_schema(event_schema)
        Draft202012Validator(
            event_schema, format_checker=FormatChecker()
        ).validate(payload)
    except SchemaError as exc:
        raise SandboxPersistenceError(f"invalid event schema: {exc.message}") from exc
    except ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path) or "event"
        raise SandboxPersistenceError(
            f"event does not validate at {path}: {exc.message}"
        ) from exc

    event = CompanyOSSandboxEvent(
        sandbox_run_id=run.id,
        workflow_instance_id=instance.id,
        event_id=event_id,
        sequence=next_sequence,
        idempotency_key=idempotency_key,
        primary_classification=payload["primary_classification"],
        event_type=payload["event_type"],
        result=payload["result"],
        risk_level=payload["risk_level"],
        state_before=state_before,
        state_after=state_after,
        external_side_effect=payload["external_side_effect"],
        terminal_key="terminal" if is_terminal else None,
        payload=payload,
        native_decision=deepcopy(native_decision),
        native_failure=deepcopy(native_failure),
    )
    db.add(event)

    if changes_state:
        instance.current_state = state_after
        instance.version += 1
    if is_terminal:
        instance.terminal = True

    await db.flush()
    return event


async def list_sandbox_events(
    db: AsyncSession, *, sandbox_run_id: int
) -> list[CompanyOSSandboxEvent]:
    """Return immutable event rows in canonical sequence order."""
    result = await db.scalars(
        select(CompanyOSSandboxEvent)
        .where(CompanyOSSandboxEvent.sandbox_run_id == sandbox_run_id)
        .order_by(CompanyOSSandboxEvent.sequence)
    )
    return list(result)


def export_sandbox_event(event: CompanyOSSandboxEvent) -> dict[str, Any]:
    """Export exactly the native canonical payload, excluding internal fields."""
    return deepcopy(event.payload)


def _require_bound_identity(payload: dict[str, Any], field: str, expected: Any) -> None:
    if field in payload and payload[field] != expected:
        raise SandboxPersistenceError(
            f"event {field} does not match authoritative runtime identity"
        )


def _require_one_native_evidence(
    native_decision: dict[str, Any] | None,
    native_failure: dict[str, Any] | None,
) -> None:
    decision_present = isinstance(native_decision, dict) and bool(native_decision)
    failure_present = isinstance(native_failure, dict) and bool(native_failure)
    if decision_present == failure_present:
        raise SandboxPersistenceError(
            "exactly one non-empty native_decision or native_failure is required"
        )
