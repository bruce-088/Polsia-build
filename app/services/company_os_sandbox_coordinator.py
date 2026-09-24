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
from app.models.company_os_sandbox import CompanyOSSandboxEvent, CompanyOSWorkflowInstance
from app.services.company_os_sandbox_service import append_sandbox_event


class SyntheticAdapter(Protocol):
    """A registered sandbox-only executor; it must never call production services."""

    async def execute(self, decision: dict[str, Any], *, idempotency_key: str) -> str:
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
        )
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

    native: dict[str, Any] | None = None
    native_failure: dict[str, Any] | None = None
    transition = None
    decision: dict[str, Any] | None = None
    denial: tuple[str, str] | None = None
    integration = None
    adapter_receipt: str | None = None

    try:
        returned = await decide(deepcopy(synthetic_input))
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

    if denial is None and decision is not None and decision.disposition == "requires_founder":
        approval_id = synthetic_input.get("approval_decision_id")
        if not isinstance(approval_id, str) or not approval_id:
            denial = ("approval", "pending approval requires a decision ID")
        else:
            return await _append(
                db, instance, expected_version, idempotency_key, event_schema,
                workflow, evidence_refs, native, native_failure,
                event_type="approval_requested", result="escalated",
                risk="RED", action=native["action"], agent=native["agent_type"],
                classification="approval_queue", state_after=instance.current_state,
                approval={"decision_id": approval_id, "status": "pending"},
                integration=integration, category="approval", error=None,
            )

    if (denial is None and native is not None and native["integration"] is not None
        and native["integration"]["phase"] in {"read", "execute"}):
        name = native["integration"]["name"]
        adapter = synthetic_adapters.get(name)
        if adapter is None:
            denial = ("integration", "registered synthetic adapter is missing")
        else:
            try:
                adapter_receipt = await adapter.execute(deepcopy(native), idempotency_key=idempotency_key)
                if not isinstance(adapter_receipt, str) or not adapter_receipt.startswith("sandbox://"):
                    raise SandboxCoordinatorError("synthetic adapter did not return sandbox evidence")
            except Exception as exc:
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
        )

    # Approval and capability checks have completed. A state change and its
    # evidence are flushed together by the persistence service.
    return await _append(
        db, instance, expected_version, idempotency_key, event_schema, workflow,
        [*evidence_refs, *([adapter_receipt] if adapter_receipt else [])],
        native, None, event_type="transition_completed" if transition else "decision_produced",
        result="executed_in_sandbox" if transition or adapter_receipt else "observed",
        risk=transition.risk_level if transition else native["risk_level"],
        action=native["action"], agent=native["agent_type"],
        classification=synthetic_input["primary_classification"],
        state_after=native["state_after"], approval=None,
        integration=integration, category="allow", error=None,
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


async def _append(
    db: AsyncSession, instance: CompanyOSWorkflowInstance, expected_version: int,
    idempotency_key: str, schema: dict[str, Any], workflow: dict[str, Any],
    refs: list[str], native: dict[str, Any] | None, failure: dict[str, Any] | None,
    *, event_type: str, result: str, risk: str, action: str, agent: str,
    classification: str, state_after: str | None,
    approval: dict[str, Any] | None, integration: Any, category: str,
    error: str | None,
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
        "autonomy": {"class": "A1" if approval else "A0", "founder_minutes": 0},
        "error": error, "metadata": {"gate": category},
    }
    return await append_sandbox_event(
        db, workflow_instance_id=instance.id, expected_version=expected_version,
        idempotency_key=idempotency_key, event_payload=payload,
        event_schema=schema, workflow_definition=workflow,
        native_decision=native, native_failure=failure,
    )
