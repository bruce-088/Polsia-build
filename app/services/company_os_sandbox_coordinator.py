"""Mandatory governed path for one Stage 2 synthetic action.

The caller supplies frozen Company OS inputs and a synthetic adapter. This
module does not load production integrations or interpret generic task status.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.company_os_compliance import EMAIL_USES, is_email_execute, is_email_integration
from app.agents.company_os_integration import (
    CompanyOSIntegrationError,
    IntegrationDecision,
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
    CompanyOSSandboxRun,
    CompanyOSWorkflowInstance,
)
from app.services.company_os_sandbox_dispatch import (
    DispatchBlocked,
    RecipientReviewBlocked,
    execute_dispatch,
    find_dispatch,
    metadata_for,
    refusal_evidence,
)
from app.services.company_os_sandbox_service import append_sandbox_event
from app.services.company_os_synthetic_adapters import (
    SyntheticCapabilityError,
    SyntheticComplianceBlock,
)


class SyntheticAdapter(Protocol):
    """A registered sandbox-only executor; it must never call production services.

    A repeated idempotency key must return the same receipt without repeating
    its synthetic side effect, even when the event transaction was rolled back.
    """

    async def execute(self, decision: dict[str, Any], *, idempotency_key: str, run_id: str, recipient_id: str) -> str:
        """Return a nonempty synthetic evidence reference after execution."""


class SandboxCoordinatorError(ValueError):
    """The coordinator cannot safely handle the supplied run inputs."""


DecisionProvider = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def coordinate_sandbox_action(
    db: AsyncSession,
    *,
    workflow_instance_id: int,
    expected_version: int,
    idempotency_key: str,
    synthetic_input: dict[str, Any],
    decide: DecisionProvider,
    workflow: dict[str, Any],
    policy: dict[str, Any],
    integration_registry: dict[str, Any],
    event_schema: dict[str, Any],
    canonical_agents: list[str],
    canonical_handoffs: list[str],
    canonical_actions: list[str],
    synthetic_adapters: dict[str, SyntheticAdapter],
) -> CompanyOSSandboxEvent:
    """Govern a single decision and persist exactly one native operational event.

    The caller owns the database transaction. A denied action records failure
    evidence without mutating workflow state. RED approval remains pending;
    resolution and replay semantics belong to S2-P1.1.
    """
    if not settings.sandbox_mode:
        raise SandboxCoordinatorError("Stage 2 requires sandbox_mode")
    if not idempotency_key or not isinstance(synthetic_input, dict):
        raise SandboxCoordinatorError("idempotency_key and synthetic input are required")
    try:
        json.dumps(synthetic_input, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SandboxCoordinatorError("synthetic input must be JSON compatible") from exc
    evidence_refs = synthetic_input.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not evidence_refs or any(
        not isinstance(ref, str) or not ref.startswith("sandbox://") for ref in evidence_refs
    ) or len(evidence_refs) != len(set(evidence_refs)):
        raise SandboxCoordinatorError("synthetic input requires sandbox evidence refs")
    if not isinstance(synthetic_input.get("primary_classification"), str):
        raise SandboxCoordinatorError("synthetic input requires a primary classification")
    verified_requirements = synthetic_input.get("verified_requirements", [])
    if not isinstance(verified_requirements, list) or any(
        not isinstance(item, str) for item in verified_requirements
    ):
        raise SandboxCoordinatorError("verified requirements must be a list of strings")
    if not all(isinstance(value, list) and value and all(
        isinstance(item, str) and item for item in value
    ) for value in (canonical_agents, canonical_handoffs, canonical_actions)):
        raise SandboxCoordinatorError("canonical action, agent, and handoff lists are required")

    instance = await db.scalar(
        select(CompanyOSWorkflowInstance).where(
            CompanyOSWorkflowInstance.id == workflow_instance_id
        ).with_for_update().execution_options(populate_existing=True)
    )
    if instance is None:
        raise SandboxCoordinatorError("workflow instance does not exist")
    if workflow.get("id") != instance.workflow_id:
        raise SandboxCoordinatorError("workflow does not match instance")
    validate_workflow_definition(workflow)
    validate_integration_registry(integration_registry)
    if instance.current_state not in workflow["states"]:
        raise SandboxCoordinatorError("current workflow state is not canonical")

    # Do not invoke the model or an adapter for a replay or stale delivery.
    existing = await db.scalar(
        select(CompanyOSSandboxEvent.id).where(
            CompanyOSSandboxEvent.sandbox_run_id == instance.sandbox_run_id,
            CompanyOSSandboxEvent.idempotency_key == idempotency_key,
        )
    )
    if existing is not None or instance.version != expected_version or instance.terminal:
        raise SandboxCoordinatorError("duplicate, stale, or terminal workflow delivery")

    dispatch_metadata = {}
    compliance_evidence = None
    try:
        pending = await find_dispatch(db, instance, synthetic_adapters, snapshot=synthetic_input, registry=integration_registry)
    except DispatchBlocked as exc:
        return await _compliance_event(
            db, instance, expected_version, idempotency_key, event_schema, workflow,
            evidence_refs, exc.native, synthetic_input, None, exc.result.evidence, exc.metadata,
        )
    except SyntheticCapabilityError as exc:
        return await _review_event(
            db, instance, expected_version, idempotency_key, event_schema, workflow,
            evidence_refs, str(exc), agent=canonical_agents[0],
        )
    awaiting_resume = bool(pending) and await _awaiting_resume(db, instance, pending[1])
    if pending and pending[1]["status"] == "executed" and not awaiting_resume:
        return await _reconcile_event(
            db, instance, expected_version, idempotency_key, event_schema, workflow,
            evidence_refs, pending[1], synthetic_input, canonical_agents,
            canonical_handoffs, canonical_actions,
        )

    native: dict[str, Any] | None = None
    native_failure: dict[str, Any] | None = None
    transition = None
    decision: dict[str, Any] | None = None
    denial: tuple[str, str] | None = None
    integration = None
    adapter_receipt: str | None = None

    try:
        returned = (deepcopy(pending[1]["consumers"][instance.id]["consumer_decision"])
                    if pending else await decide(deepcopy(synthetic_input)))
    except Exception as exc:
        native_failure = {"type": type(exc).__name__, "message": str(exc)}
        if getattr(exc, "raw_output", None) is not None:
            raw_output = exc.raw_output
            try:
                json.dumps(raw_output, allow_nan=False)
            except (TypeError, ValueError, OverflowError):
                raw_output = repr(raw_output)
            native_failure["raw_output"] = raw_output
        denial = ("provider", "provider failed before a native decision")
    else:
        native_failure = None
        if isinstance(returned, dict):
            try:
                json.dumps(returned, allow_nan=False)
            except (TypeError, ValueError, OverflowError):
                native_failure = {"type": "contract", "raw_output": repr(returned)}
                denial = ("contract", "native output is not JSON compatible")
            else:
                native = deepcopy(returned)
        else:
            native_failure = {"type": "contract", "raw_output": returned if isinstance(returned, str) else repr(returned)}
            denial = ("contract", "native output is not an object")

    if native is not None:
        try:
            _validate_native(native, instance, workflow, canonical_agents,
                             canonical_handoffs, canonical_actions)
        except SandboxCoordinatorError as exc:
            denial = ("contract", str(exc))
        if denial is None:
            if native["state_after"] != instance.current_state:
                try:
                    transition = require_valid_transition(
                        workflow,
                        current_state=instance.current_state,
                        action=native["action"],
                        proposed_state=native["state_after"],
                        proposed_risk=native["risk_level"],
                    )
                except CompanyOSWorkflowError as exc:
                    denial = ("transition", str(exc))
            if denial is None:
                intent = deepcopy(native["policy_intent"])
                # Model-authored approval cannot authorize a RED action.
                # P1.1 will provide a verified founder resolution separately.
                intent.pop("founder_approval_status", None)
                if intent.get("requires_authority"):
                    intent["authority_verified"] = synthetic_input.get("authority_verified") is True
                if intent.get("requires_consent"):
                    intent["consent_verified"] = synthetic_input.get("consent_verified") is True
                missing_requirements = set(transition.requirements) - set(
                    verified_requirements
                ) if transition else set()
                if missing_requirements:
                    denial = ("transition", "canonical transition requirements lack evidence")
                elif intent.get("action_type") != native["action"] or intent.get("risk_level") != native["risk_level"]:
                    denial = ("contract", "policy intent does not match native action and risk")
                else:
                    try:
                        decision = evaluate_action_policy(intent, policy)
                    except (CompanyOSPolicyError, TypeError, ValueError) as exc:
                        denial = ("policy", str(exc))
            if denial is None and decision is not None and decision.disposition == "block":
                denial = ("policy", "; ".join(decision.reasons))
            if denial is None and native["integration"] is not None:
                request = native["integration"]
                try:
                    integration = evaluate_integration_capability(
                        integration_registry,
                        integration=request["name"],
                        phase=request["phase"],
                        requested_use=request.get("use"),
                        requested_scope=request.get("scope"),
                    )
                except (CompanyOSIntegrationError, KeyError, TypeError) as exc:
                    denial = ("integration", str(exc))
                else:
                    if (not integration.allowed or
                        (request["phase"] in {"read", "execute"} and integration.mode != "sandbox") or
                        integration.mode == "live"):
                        denial = ("integration", "integration is not verified for sandbox execution")
                    elif request["phase"] == "execute" and (
                        not native["policy_intent"].get("external_write") or
                        native["policy_intent"].get("integration_mode") != integration.mode
                    ):
                        denial = ("contract", "execution intent does not match the sandbox capability")
            if denial is None and transition and transition.integration != (
                native["integration"]["name"] if native["integration"] else None
            ):
                denial = ("transition", "transition integration does not match the canonical workflow")
            if denial is None and transition and transition.integration and (
                native["integration"]["phase"] != "execute"
            ):
                denial = ("transition", "integration transition requires sandbox execution")
            if denial is None and native["policy_intent"]["external_write"] and (
                integration is None or native["integration"]["phase"] != "execute"
            ):
                denial = ("integration", "write requires registered sandbox execution")

    if (denial is None and native is not None and isinstance(native.get("action"), str)
            and (awaiting_resume or await _has_unresumed_approval(db, instance, native["action"]))):
        denial = ("approval", "approved action requires approval resume; direct execution rejected")

    if denial is None and decision is not None and decision.disposition == "requires_founder":
        approval_id = synthetic_input.get("approval_decision_id")
        if not isinstance(approval_id, str) or not approval_id:
            denial = ("approval", "pending approval requires a decision ID")
        else:
            outstanding = await db.scalar(select(CompanyOSSandboxApproval.id).where(
                CompanyOSSandboxApproval.workflow_instance_id == instance.id,
                CompanyOSSandboxApproval.action == native["action"],
                CompanyOSSandboxApproval.status.in_(("pending", "approved", "modified")),
                CompanyOSSandboxApproval.resume_event_id.is_(None),
            ))
            if outstanding is not None:
                denial = ("approval", "action already has an unresolved founder request")
            else:
                event = await _append(
                db, instance, expected_version, idempotency_key, event_schema,
                workflow, evidence_refs, native, native_failure,
                event_type="approval_requested", result="escalated",
                risk="RED", action=native["action"], agent=native["agent_type"],
                classification="approval_queue", state_after=instance.current_state,
                approval={"decision_id": approval_id, "status": "pending"},
                integration=integration, category="approval", error=None,
                metadata_extra={"workflow_version": instance.version},
                )
                db.add(CompanyOSSandboxApproval(
                    sandbox_run_id=instance.sandbox_run_id,
                    workflow_instance_id=instance.id,
                    request_event_id=event.id,
                    decision_id=approval_id,
                    action=native["action"],
                    input_snapshot=deepcopy(synthetic_input),
                    status="pending",
                ))
                await db.flush()
                return event

    if (denial is None and native is not None and native["integration"] is not None
        and native["integration"]["phase"] in {"read", "execute"}):
        name = native["integration"]["name"]
        adapter = synthetic_adapters.get(name)
        if adapter is None:
            denial = ("integration", "registered synthetic adapter is missing")
        else:
            try:
                if is_email_execute(native["integration"], integration_registry):
                    adapter_receipt, compliance_evidence = await execute_dispatch(
                        db, instance, native, synthetic_input, integration_registry, adapter,
                        dispatch_metadata, existing=pending[1] if pending else None,
                    )
                elif is_email_integration(native["integration"], integration_registry):
                    raise SyntheticCapabilityError("email integration permits only a governed execute")
                elif native["integration"]["phase"] == "execute":
                    raise RecipientReviewBlocked("non-email execute blocked: durable dispatch ledger is required")
                else:
                    run = await db.get(CompanyOSSandboxRun, instance.sandbox_run_id)
                    dispatch_metadata.update(recipient_id=instance.entity_id, dispatch_attempted=True)
                    adapter_receipt = await adapter.execute(
                        deepcopy(native), idempotency_key=idempotency_key,
                        run_id=run.run_id, recipient_id=instance.entity_id,
                    )
                if not isinstance(adapter_receipt, str) or not adapter_receipt.startswith("sandbox://"):
                    raise SandboxCoordinatorError("synthetic adapter did not return sandbox evidence")
            except DispatchBlocked as exc:
                return await _compliance_event(
                    db, instance, expected_version, idempotency_key, event_schema,
                    workflow, evidence_refs, native, synthetic_input, integration,
                    exc.result.evidence, dispatch_metadata,
                )
            except SyntheticComplianceBlock:
                return await _compliance_event(
                    db, instance, expected_version, idempotency_key, event_schema, workflow,
                    evidence_refs, native, synthetic_input, integration,
                    refusal_evidence(adapter, instance, native, integration_registry, dispatch_metadata), dispatch_metadata,
                )
            except RecipientReviewBlocked as exc:
                denial = ("dispatch_review" if "orphan" in str(exc) else "unsupported_execute", str(exc))
            except (SyntheticCapabilityError, SandboxCoordinatorError) as exc:
                denial = ("integration", f"synthetic adapter failed: {type(exc).__name__}: {exc}")

    if denial is not None:
        category, reason = denial
        return await _append(
            db, instance, expected_version, idempotency_key, event_schema, workflow,
            evidence_refs, native, native_failure,
            event_type="failure_detected", result="failed" if category in {"provider", "contract", "integration"} else "blocked",
            # Reporting a failed action is GREEN. The native risk remains in
            # native_decision; a RED event would require a real approval record.
            risk="GREEN",
            action=native.get("action", "record_failure") if native and isinstance(native.get("action"), str) and native.get("action") else "record_failure",
            agent=native.get("agent_type", "governance") if native and native.get("agent_type") in canonical_agents else canonical_agents[0],
            classification="failure_recovery", state_after=instance.current_state,
            approval=None, integration=integration, category=category, error=reason,
            metadata_extra=dispatch_metadata,
        )

    # Approval and capability checks have completed. A state change and its
    # evidence are flushed together by the persistence service.
    return await _append(
        db, instance, expected_version, idempotency_key, event_schema, workflow,
        [*evidence_refs, *([adapter_receipt] if adapter_receipt else [])],
        native, None, event_type=(
            "terminal_outcome" if transition and native["state_after"] in workflow.get("terminal_states", [])
            else "transition_completed" if transition else "decision_produced"),
        result="executed_in_sandbox" if transition or adapter_receipt else "observed",
        risk=transition.risk_level if transition else native["risk_level"],
        action=native["action"], agent=native["agent_type"],
        classification=synthetic_input["primary_classification"],
        state_after=native["state_after"], approval=None,
        integration=integration, category="allow", error=None,
        metadata_extra=dispatch_metadata, compliance=compliance_evidence,
    )


def _validate_native(
    native: dict[str, Any], instance: CompanyOSWorkflowInstance,
    workflow: dict[str, Any], agents: list[str], handoffs: list[str], actions: list[str],
) -> None:
    required = {"action", "risk_level", "state_after", "agent_type", "handoff_to", "policy_intent", "integration"}
    if native.keys() != required:
        raise SandboxCoordinatorError("native decision has missing or unexpected fields")
    if not all(isinstance(native[key], str) for key in ("action", "agent_type", "handoff_to", "risk_level", "state_after")):
        raise SandboxCoordinatorError("native action, owner, risk, and state must be strings")
    if native["action"] not in actions or native["agent_type"] not in agents or native["handoff_to"] not in handoffs:
        raise SandboxCoordinatorError("native action, agent, or handoff is not canonical")
    if native["risk_level"] not in {"GREEN", "YELLOW", "RED"} or native["state_after"] not in workflow["states"]:
        raise SandboxCoordinatorError("native risk or workflow state is not canonical")
    if not isinstance(native["policy_intent"], dict):
        raise SandboxCoordinatorError("native policy intent is not an object")
    if native["integration"] is not None:
        request = native["integration"]
        if (not isinstance(request, dict) or
            not isinstance(request.get("name"), str) or
            not isinstance(request.get("phase"), str) or
            request["phase"] not in {"draft", "propose", "read", "execute"}):
            raise SandboxCoordinatorError("native integration request is invalid")
        if set(request) - {"name", "phase", "use", "scope", "message"}:
            raise SandboxCoordinatorError("native integration has unexpected fields")
        if any(key in request and not isinstance(request[key], str) for key in ("use", "scope")):
            raise SandboxCoordinatorError("integration use and scope must be strings")
        if "message" in request:
            message = request["message"]
            if (request["phase"] != "execute" or not isinstance(message, dict)
                    or set(message) != {"template_id", "fills"}
                    or not isinstance(message["template_id"], str) or not message["template_id"]
                    or not isinstance(message["fills"], dict)
                    or any(not isinstance(k, str) or not isinstance(v, str)
                           for k, v in message["fills"].items())):
                raise SandboxCoordinatorError("native integration message is invalid")
        if request["phase"] == "execute" and request.get("use") in EMAIL_USES and "message" not in request:
            raise SandboxCoordinatorError("email execution requires template_id and fills")


async def _append(
    db: AsyncSession, instance: CompanyOSWorkflowInstance, expected_version: int,
    idempotency_key: str, schema: dict[str, Any], workflow: dict[str, Any],
    refs: list[str], native: dict[str, Any] | None, failure: dict[str, Any] | None,
    *, event_type: str, result: str, risk: str, action: str, agent: str,
    classification: str, state_after: str | None,
    approval: dict[str, Any] | None, integration: Any, category: str,
    error: str | None, autonomy_class: str | None = None,
    founder_minutes: float = 0, metadata_extra: dict[str, Any] | None = None,
    compliance: dict[str, Any] | None = None,
) -> CompanyOSSandboxEvent:
    payload = {
        "schema_version": "0.1.0", "event_id": str(uuid4()),
        "occurred_at": datetime.now(UTC).isoformat(),
        "primary_classification": classification,
        "workflow_id": instance.workflow_id, "entity_type": instance.entity_type,
        "entity_id": instance.entity_id, "event_type": event_type,
        "agent_type": agent, "action": action, "risk_level": risk,
        "state_before": instance.current_state, "state_after": state_after,
        "result": result, "external_side_effect": False,
        "approval": approval,
        "integration": ({"name": integration.integration, "mode": integration.mode,
                         "attempted_write": integration.phase == "execute", "target": "sandbox" if integration.phase == "execute" else "none"}
                        if integration and integration.mode not in {"unregistered", "live"} else None),
        "evidence_refs": refs,
        "autonomy": {"class": autonomy_class or ("A1" if approval else "A0"), "founder_minutes": founder_minutes},
        "error": error, "metadata": {"gate": category, **(metadata_extra or {})},
    }
    if compliance is not None:
        payload["compliance"] = compliance
    return await append_sandbox_event(
        db, workflow_instance_id=instance.id, expected_version=expected_version,
        idempotency_key=idempotency_key, event_payload=payload,
        event_schema=schema, workflow_definition=workflow,
        native_decision=native, native_failure=failure,
    )


async def _compliance_event(
    db, instance, version, key, schema, workflow, refs, native, snapshot, integration,
    compliance, metadata, *, override=False,
):
    if override:
        compliance = {**compliance, "override_attempt": override}
    return await _append(
        db, instance, version, key, schema, workflow, refs, native, None,
        event_type="transition_attempted", result="blocked", risk="RED",
        action=native["action"], agent=native["agent_type"],
        classification=snapshot["primary_classification"], state_after=instance.current_state,
        approval=None, integration=integration, category="compliance",
        error=", ".join(compliance["reason_codes"]), compliance=compliance,
        metadata_extra={**metadata, **({"override_attempt": True} if override else {})},
    )


async def _review_event(db, instance, version, key, schema, workflow, refs, reason, *,
                        native=None, agent, metadata=None, approval=None, autonomy_class=None):
    """Record a dispatch condition that needs review; nothing is sent and state is unchanged."""
    failure = None if native else {"type": "dispatch_review", "message": reason}
    return await _append(
        db, instance, version, key, schema, workflow, refs, native, failure,
        event_type="failure_detected", result="blocked", risk="GREEN",
        action=native["action"] if native else "record_failure",
        agent=native["agent_type"] if native else agent, classification="failure_recovery",
        state_after=instance.current_state, approval=approval, integration=None,
        category="dispatch_review", error=reason, metadata_extra=metadata or {},
        autonomy_class=autonomy_class,
    )


async def _reconcile_event(
    db, instance, version, key, schema, workflow, refs, record, snapshot,
    agents, handoffs, actions, *, approval=None, autonomy_class=None,
    original=None, extra_metadata=None,
):
    native = deepcopy(record["consumers"][instance.id]["consumer_decision"])
    _validate_native(native, instance, workflow, agents, handoffs, actions)
    transition = require_valid_transition(
        workflow, current_state=instance.current_state, action=native["action"],
        proposed_state=native["state_after"], proposed_risk=native["risk_level"],
    )
    receipt = record["receipt"]
    if not isinstance(receipt, str) or not receipt.startswith("sandbox://"):
        raise SandboxCoordinatorError("executed dispatch has no valid receipt")
    request = native["integration"]
    integration = IntegrationDecision(request["name"], request["phase"], "sandbox", True, True,
                                      "receipt-only reconciliation")
    return await _append(
        db, instance, version, key, schema, workflow, list(dict.fromkeys([*refs, receipt])),
        original or native, None,
        event_type="terminal_outcome" if native["state_after"] in workflow.get("terminal_states", []) else "transition_completed",
        result="executed_in_sandbox", risk=transition.risk_level,
        action=native["action"], agent=native["agent_type"],
        classification=snapshot["primary_classification"], state_after=native["state_after"],
        approval=approval, integration=integration, category="reconciliation", error=None,
        autonomy_class=autonomy_class,
        metadata_extra={**metadata_for(record), "reconciled": True, **(extra_metadata or {})},
    )


async def _awaiting_resume(db, instance, record) -> bool:
    """A dispatch bound to an approval stays with approval resume, even if a correction changed its action."""
    consumer = record["consumers"][instance.id]
    approval_id = consumer.get("approval_id")
    if approval_id is not None:
        approval = await db.get(CompanyOSSandboxApproval, approval_id)
        if approval is not None and approval.status in ("approved", "modified") and approval.resume_event_id is None:
            return True
    return await _has_unresumed_approval(db, instance, consumer["consumer_decision"]["action"])


async def _has_unresumed_approval(db, instance, action) -> bool:
    """An unresumed approval still binds its founder-corrected action, not just its original one."""
    approvals = await db.scalars(select(CompanyOSSandboxApproval).where(
        CompanyOSSandboxApproval.workflow_instance_id == instance.id,
        CompanyOSSandboxApproval.status.in_(("approved", "modified")),
        CompanyOSSandboxApproval.resume_event_id.is_(None),
    ))
    for approval in approvals:
        effective_action = (
            approval.corrected_decision.get("action")
            if approval.status == "modified" and isinstance(approval.corrected_decision, dict)
            else approval.action
        )
        if effective_action == action:
            return True
    return False
