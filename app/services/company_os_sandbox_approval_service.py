"""Founder resolution and replay-safe resume for Stage 2 synthetic decisions.

These functions never invoke normal approval endpoints or production services.
The caller owns the transaction. Synthetic adapters must honor the stable
idempotency key across retries, including after a database rollback.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.company_os_integration import (
    CompanyOSIntegrationError,
    evaluate_integration_capability,
    validate_integration_registry,
)
from app.agents.company_os_policy import CompanyOSPolicyError, evaluate_action_policy
from app.agents.company_os_workflow import (
    CompanyOSWorkflowError,
    require_valid_transition,
    validate_workflow_definition,
)
from app.config import settings
from app.models.company_os_sandbox import (
    CompanyOSSandboxApproval,
    CompanyOSSandboxEvent,
    CompanyOSWorkflowInstance,
)
from app.services.company_os_sandbox_coordinator import (
    SyntheticAdapter,
    _append,
    _validate_native,
)

STATUSES = {
    "approved", "rejected", "modified", "needs_more_evidence", "expired", "cancelled",
}
EXECUTABLE = {"approved", "modified"}


class SandboxApprovalError(ValueError):
    """An approval cannot safely resolve or resume in the requested manner."""


async def resolve_sandbox_approval(
    db: AsyncSession, *, approval_id: int, status: str, founder_id: str,
    founder_minutes: float, resolution_key: str, event_schema: dict[str, Any],
    workflow: dict[str, Any], corrected_decision: dict[str, Any] | None = None,
    manual_evidence_ref: str | None = None,
) -> CompanyOSSandboxEvent:
    """Resolve a pending request exactly once; conflicting replays fail closed."""
    if not settings.sandbox_mode:
        raise SandboxApprovalError("Stage 2 requires sandbox_mode")
    if status not in STATUSES or not isinstance(founder_id, str) or not founder_id:
        raise SandboxApprovalError("valid resolution status and founder identity are required")
    if not isinstance(resolution_key, str) or not resolution_key:
        raise SandboxApprovalError("resolution idempotency key is required")
    if isinstance(founder_minutes, bool) or not isinstance(founder_minutes, int | float) or not math.isfinite(founder_minutes) or founder_minutes < 0:
        raise SandboxApprovalError("founder minutes must be a finite nonnegative number")
    if (status == "modified") != (corrected_decision is not None):
        raise SandboxApprovalError("modified status requires a corrected decision, and only modified status permits one")
    if corrected_decision is not None:
        if not isinstance(corrected_decision, dict):
            raise SandboxApprovalError("corrected decision must be an object")
        try:
            json.dumps(corrected_decision, allow_nan=False)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SandboxApprovalError("corrected decision is not JSON compatible") from exc
    if manual_evidence_ref is not None and (status not in EXECUTABLE or not isinstance(manual_evidence_ref, str) or not manual_evidence_ref.startswith("sandbox://")):
        raise SandboxApprovalError("manual execution needs an executable resolution and sandbox evidence")

    approval = await db.scalar(select(CompanyOSSandboxApproval).where(
        CompanyOSSandboxApproval.id == approval_id,
    ).with_for_update())
    if approval is None:
        raise SandboxApprovalError("approval does not exist")
    if approval.status != "pending":
        if (approval.resolution_key == resolution_key and approval.status == status and
            approval.founder_id == founder_id and approval.founder_minutes == founder_minutes and
            approval.corrected_decision == corrected_decision and approval.manual_evidence_ref == manual_evidence_ref):
            return await db.get(CompanyOSSandboxEvent, approval.resolution_event_id)
        raise SandboxApprovalError("only pending approval may be resolved")

    instance = await db.scalar(select(CompanyOSWorkflowInstance).where(
        CompanyOSWorkflowInstance.id == approval.workflow_instance_id,
    ).with_for_update())
    request = await db.get(CompanyOSSandboxEvent, approval.request_event_id)
    if (instance is None or request is None or request.event_type != "approval_requested" or
        instance.sandbox_run_id != approval.sandbox_run_id or
        request.workflow_instance_id != instance.id or request.sandbox_run_id != approval.sandbox_run_id or
        request.payload["approval"]["decision_id"] != approval.decision_id or
        request.native_decision is None or request.native_decision["action"] != approval.action):
        raise SandboxApprovalError("approval is not bound to its original request and action")
    if (workflow.get("id") != instance.workflow_id or instance.current_state != request.state_before or
        instance.version != request.payload.get("metadata", {}).get("workflow_version") or instance.terminal):
        raise SandboxApprovalError("approval workflow has changed since request")
    if corrected_decision == request.native_decision and status == "modified":
        raise SandboxApprovalError("a modified resolution must contain an actual correction")

    autonomy = "A3" if manual_evidence_ref else "A2" if status == "modified" else "A1"
    resolution = await _append(
        db, instance, instance.version, resolution_key, event_schema, workflow,
        request.payload["evidence_refs"], request.native_decision, None,
        event_type="approval_resolved", result="observed" if status in EXECUTABLE else "blocked",
        risk="RED", action=approval.action, agent=request.payload["agent_type"],
        classification="approval_queue", state_after=instance.current_state,
        approval={"decision_id": approval.decision_id, "status": status},
        integration=None, category="founder_resolution", error=None,
        autonomy_class=autonomy, founder_minutes=founder_minutes,
        metadata_extra={"founder_id": founder_id, **({"corrected_decision": corrected_decision} if corrected_decision is not None else {})},
    )
    approval.status = status
    approval.founder_id = founder_id
    approval.founder_minutes = founder_minutes
    approval.autonomy_class = autonomy
    approval.corrected_decision = deepcopy(corrected_decision)
    approval.manual_evidence_ref = manual_evidence_ref
    approval.resolution_key = resolution_key
    approval.resolution_event_id = resolution.id
    approval.resolved_at = datetime.now(UTC)
    await db.flush()
    return resolution


async def resume_sandbox_approval(
    db: AsyncSession, *, approval_id: int, workflow: dict[str, Any],
    policy: dict[str, Any], integration_registry: dict[str, Any],
    event_schema: dict[str, Any], canonical_agents: list[str],
    canonical_handoffs: list[str], canonical_actions: list[str],
    synthetic_adapters: dict[str, SyntheticAdapter],
) -> CompanyOSSandboxEvent:
    """Resume one founder-authorized action using its frozen native evidence.

    The durable approval ID determines the adapter idempotency key. A sandbox
    adapter must replay the same receipt for that key if persistence rolls back.
    """
    if not settings.sandbox_mode:
        raise SandboxApprovalError("Stage 2 requires sandbox_mode")
    approval = await db.scalar(select(CompanyOSSandboxApproval).where(
        CompanyOSSandboxApproval.id == approval_id,
    ).with_for_update())
    if approval is None:
        raise SandboxApprovalError("approval does not exist")
    if approval.status not in EXECUTABLE:
        raise SandboxApprovalError("resolution does not permit execution")
    if approval.resume_event_id is not None:
        return await db.get(CompanyOSSandboxEvent, approval.resume_event_id)
    if approval.resolution_event_id is None or not approval.founder_id:
        raise SandboxApprovalError("founder resolution is incomplete")

    instance = await db.scalar(select(CompanyOSWorkflowInstance).where(
        CompanyOSWorkflowInstance.id == approval.workflow_instance_id,
    ).with_for_update())
    request = await db.get(CompanyOSSandboxEvent, approval.request_event_id)
    if (instance is None or request is None or request.native_decision is None or
        instance.sandbox_run_id != approval.sandbox_run_id or
        request.workflow_instance_id != instance.id or
        request.payload["approval"]["decision_id"] != approval.decision_id or
        request.native_decision["action"] != approval.action):
        raise SandboxApprovalError("approval and original native decision do not match")
    if instance.terminal or instance.current_state != request.state_before or instance.version != request.payload.get("metadata", {}).get("workflow_version", 0):
        raise SandboxApprovalError("workflow changed since approval request")
    if workflow.get("id") != instance.workflow_id:
        raise SandboxApprovalError("workflow does not match approved request")
    validate_workflow_definition(workflow)
    validate_integration_registry(integration_registry)

    original = request.native_decision
    effective = approval.corrected_decision if approval.status == "modified" else original
    snapshot = approval.input_snapshot
    receipt: str | None = None
    transition = None
    integration = None
    reason: str | None = None
    try:
        _validate_native(effective, instance, workflow, canonical_agents, canonical_handoffs, canonical_actions)
        if effective["state_after"] != instance.current_state:
            transition = require_valid_transition(
                workflow, current_state=instance.current_state,
                action=effective["action"], proposed_state=effective["state_after"],
                proposed_risk=effective["risk_level"],
            )
        if transition and set(transition.requirements) - set(snapshot.get("verified_requirements", [])):
            raise SandboxApprovalError("transition requirements lack frozen evidence")
        intent = deepcopy(effective["policy_intent"])
        intent.pop("founder_approval_status", None)
        if intent.get("requires_authority"):
            intent["authority_verified"] = snapshot.get("authority_verified") is True
        if intent.get("requires_consent"):
            intent["consent_verified"] = snapshot.get("consent_verified") is True
        if intent.get("action_type") != effective["action"] or intent.get("risk_level") != effective["risk_level"]:
            raise SandboxApprovalError("corrected policy intent mismatches action or risk")
        intent["founder_approval_status"] = "approved"
        policy_result = evaluate_action_policy(intent, policy)
        if policy_result.disposition != "allow":
            raise SandboxApprovalError("founder resolution cannot override policy block")
        request_integration = effective["integration"]
        if request_integration is not None:
            integration = evaluate_integration_capability(
                integration_registry, integration=request_integration["name"],
                phase=request_integration["phase"],
                requested_use=request_integration.get("use"),
                requested_scope=request_integration.get("scope"),
            )
            if not integration.allowed or integration.mode == "live" or (request_integration["phase"] in {"read", "execute"} and integration.mode != "sandbox"):
                raise SandboxApprovalError("sandbox integration capability unavailable")
            if request_integration["phase"] == "execute" and (not intent["external_write"] or intent.get("integration_mode") != integration.mode):
                raise SandboxApprovalError("integration execution and policy intent disagree")
        if transition and transition.integration != (request_integration["name"] if request_integration else None):
            raise SandboxApprovalError("transition integration differs from workflow")
        if transition and transition.integration and request_integration["phase"] != "execute":
            raise SandboxApprovalError("integration transition requires sandbox execution")
        if intent["external_write"] and (integration is None or request_integration["phase"] != "execute"):
            raise SandboxApprovalError("write requires registered sandbox execution")
        if approval.manual_evidence_ref is None and request_integration is not None and request_integration["phase"] in {"read", "execute"}:
            if request_integration["name"] not in synthetic_adapters:
                raise SandboxApprovalError("registered synthetic adapter is missing")
            try:
                receipt = await synthetic_adapters[request_integration["name"]].execute(
                    deepcopy(effective), idempotency_key=f"stage2-approval-resume:{approval.id}",
                )
            except Exception as exc:
                raise SandboxApprovalError(f"synthetic adapter failed: {type(exc).__name__}: {exc}") from exc
            if not isinstance(receipt, str) or not receipt.startswith("sandbox://"):
                raise SandboxApprovalError("synthetic adapter did not return sandbox evidence")
    except (SandboxApprovalError, CompanyOSWorkflowError, CompanyOSPolicyError, CompanyOSIntegrationError, KeyError, TypeError, ValueError) as exc:
        reason = str(exc)

    resume_key = f"stage2-approval-resume:{approval.id}"
    if reason is not None:
        event = await _append(
            db, instance, instance.version, resume_key, event_schema, workflow,
            request.payload["evidence_refs"], original, None,
            event_type="failure_detected", result="blocked", risk="GREEN",
            action=approval.action, agent=request.payload["agent_type"],
            classification="failure_recovery", state_after=instance.current_state,
            approval={"decision_id": approval.decision_id, "status": approval.status},
            integration=integration, category="approval_resume", error=reason,
            autonomy_class=approval.autonomy_class, founder_minutes=0,
        )
    else:
        refs = list(dict.fromkeys([*request.payload["evidence_refs"], *(
            [approval.manual_evidence_ref] if approval.manual_evidence_ref else [receipt] if receipt else []
        )]))
        event = await _append(
            db, instance, instance.version, resume_key, event_schema, workflow,
            refs, original, None,
            event_type="terminal_outcome" if transition and effective["state_after"] in workflow.get("terminal_states", []) else "transition_completed" if transition else "decision_produced",
            result="executed_in_sandbox" if transition or receipt or approval.manual_evidence_ref else "observed",
            risk=transition.risk_level if transition else effective["risk_level"],
            action=effective["action"], agent=effective["agent_type"],
            classification=snapshot["primary_classification"],
            state_after=effective["state_after"],
            approval={"decision_id": approval.decision_id, "status": approval.status},
            integration=integration, category="approved_resume", error=None,
            autonomy_class=approval.autonomy_class, founder_minutes=0,
            metadata_extra={"founder_id": approval.founder_id, "approval_request_event_id": request.event_id,
                            **({"founder_corrected_decision": effective} if approval.status == "modified" else {})},
        )
    approval.resume_event_id = event.id
    approval.resume_key = resume_key
    await db.flush()
    return event
