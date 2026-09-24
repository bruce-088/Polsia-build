"""Governed Stage 2 decisions stay native and fail closed before execution."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import func, select

import app.services.company_os_sandbox_approval_service as approval_module
from app.models.company_os_sandbox import CompanyOSSandboxApproval, CompanyOSSandboxEvent
from app.services.company_os_sandbox_approval_service import (
    SandboxApprovalError,
    resolve_sandbox_approval,
    resume_sandbox_approval,
)
from app.services.company_os_sandbox_coordinator import (
    SandboxCoordinatorError,
    coordinate_sandbox_action,
)
from app.services.company_os_sandbox_service import (
    SandboxPersistenceError,
    create_sandbox_run,
    create_workflow_instance,
    export_sandbox_event,
)

WORKFLOW = {
    "id": "prospect_to_meeting",
    "states": ["research", "scored", "sent"],
    "terminal_states": [],
    "transitions": [
        {"from": "research", "to": "scored", "action": "score_against_icp", "risk_level": "GREEN"},
        {"from": "scored", "to": "sent", "action": "send_outreach", "risk_level": "YELLOW", "integration": "sandbox_mail"},
    ],
}
POLICY = {
    "red_action_types": ["pricing_change"],
    "hard_denies": ["bypass_opt_out"],
    "non_approvable_blocks": [],
    "limits": {"max_discount_percent": 10},
}
REGISTRY = {
    "modes": ["disabled", "documented", "sandbox", "read_only", "write_limited", "live"],
    "integrations": {
        "sandbox_mail": {
            "mode": "sandbox", "verified": True, "external_writes": True,
            "desired_use": ["send_outreach"],
        },
    },
}
# Test-only representation of the relevant canonical schema restrictions. The
# production coordinator always receives the frozen schema from its caller.
EVENT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version", "event_id", "run_id", "sequence", "occurred_at",
        "company_slug", "primary_classification", "workflow_id", "entity_type",
        "entity_id", "event_type", "agent_type", "action", "risk_level",
        "state_before", "state_after", "result", "external_side_effect",
        "evidence_refs", "autonomy",
    ],
    "properties": {
        "schema_version": {"const": "0.1.0"},
        "event_id": {"type": "string", "minLength": 1},
        "run_id": {"type": "string", "minLength": 1},
        "sequence": {"type": "integer", "minimum": 1},
        "occurred_at": {"type": "string", "format": "date-time"},
        "company_slug": {"const": "acqivo"},
        "primary_classification": {"enum": ["acquisition", "approval_queue", "failure_recovery"]},
        "workflow_id": {"const": "prospect_to_meeting"},
        "entity_type": {"const": "prospect"},
        "entity_id": {"const": "p-1"},
        "event_type": {"enum": ["decision_produced", "transition_completed", "approval_requested", "approval_resolved", "failure_detected", "terminal_outcome"]},
        "agent_type": {"enum": ["acquisition", "governance"]},
        "action": {"type": "string", "minLength": 1},
        "risk_level": {"enum": ["GREEN", "YELLOW", "RED"]},
        "state_before": {"type": ["string", "null"]},
        "state_after": {"type": ["string", "null"]},
        "result": {"enum": ["observed", "executed_in_sandbox", "escalated", "blocked", "failed"]},
        "external_side_effect": {"const": False},
        "approval": {"oneOf": [{"type": "null"}, {"type": "object", "required": ["decision_id", "status"], "properties": {"decision_id": {"type": "string"}, "status": {"enum": ["pending", "approved", "rejected", "modified", "needs_more_evidence", "expired", "cancelled"]}}, "additionalProperties": False}]},
        "integration": {"oneOf": [{"type": "null"}, {"type": "object", "required": ["name", "mode", "attempted_write", "target"], "properties": {"name": {"type": "string"}, "mode": {"enum": ["sandbox", "disabled", "documented", "read_only", "write_limited"]}, "attempted_write": {"type": "boolean"}, "target": {"enum": ["sandbox", "none"]}}, "additionalProperties": False}]},
        "evidence_refs": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "autonomy": {"type": "object", "required": ["class", "founder_minutes"], "properties": {"class": {"enum": ["A0", "A1", "A2", "A3"]}, "founder_minutes": {"type": "number", "minimum": 0}}, "additionalProperties": False},
        "error": {"type": ["string", "null"]},
        "metadata": {"type": "object"},
    },
    "allOf": [{"if": {"properties": {"risk_level": {"const": "RED"}}}, "then": {"properties": {"approval": {"type": "object"}}}}],
    "additionalProperties": False,
}


class Adapter:
    def __init__(self):
        self.calls = 0

    async def execute(self, decision, *, idempotency_key):
        self.calls += 1
        return "sandbox://mail/receipt-1"


def native(*, action="score_against_icp", state="scored", risk="GREEN", integration=None, **intent):
    return {
        "action": action,
        "risk_level": risk,
        "state_after": state,
        "agent_type": "acquisition",
        "handoff_to": "governance",
        "policy_intent": {"action_type": action, "risk_level": risk, "external_write": integration is not None,
                          **({"integration_mode": "sandbox"} if integration else {}), **intent},
        "integration": integration,
    }


async def setup(db, *, initial_state="research"):
    run = await create_sandbox_run(db, run_id="s2-test", company_slug="acqivo", protocol_version="0.1.0", input_manifest={"commit": "a" * 40})
    instance = await create_workflow_instance(db, sandbox_run_id=run.id, workflow_id=WORKFLOW["id"], entity_type="prospect", entity_id="p-1", initial_state=initial_state)
    return run, instance


async def invoke(db, instance, decision, *, adapter=None, key="delivery-1", version=0, registry=REGISTRY, schema=EVENT_SCHEMA, workflow=WORKFLOW, synthetic_input=None):
    async def decide(_):
        if isinstance(decision, Exception):
            raise decision
        return decision

    return await coordinate_sandbox_action(
        db, workflow_instance_id=instance.id, expected_version=version,
        idempotency_key=key,
        synthetic_input=synthetic_input or {"evidence_refs": ["sandbox://input/p-1"], "primary_classification": "acquisition", "approval_decision_id": "APR-1"},
        decide=decide, workflow=workflow, policy=POLICY,
        integration_registry=registry, event_schema=schema,
        canonical_agents=["acquisition", "governance"],
        canonical_handoffs=["governance"],
        canonical_actions=["score_against_icp", "send_outreach", "pricing_change", "bypass_opt_out"],
        synthetic_adapters={"sandbox_mail": adapter} if adapter else {},
    )


@pytest.mark.asyncio
async def test_green_transition_is_atomic_and_replay_stops_before_model(async_db_session):
    run, instance = await setup(async_db_session)
    output = native()
    event = await invoke(async_db_session, instance, output)
    assert (instance.current_state, instance.version) == ("scored", 1)
    assert event.native_decision == output
    assert event.payload["event_type"] == "transition_completed"
    assert event.payload["external_side_effect"] is False
    assert event.payload["result"] == "executed_in_sandbox"
    Draft202012Validator(EVENT_SCHEMA).validate(export_sandbox_event(event))
    with pytest.raises(SandboxCoordinatorError, match="duplicate"):
        await invoke(async_db_session, instance, output)
    assert await async_db_session.scalar(select(func.count()).select_from(CompanyOSSandboxEvent).where(CompanyOSSandboxEvent.sandbox_run_id == run.id)) == 1


@pytest.mark.asyncio
async def test_red_action_stops_without_adapter_or_state_mutation(async_db_session):
    _, instance = await setup(async_db_session)
    adapter = Adapter()
    output = native(action="pricing_change", state="research", risk="RED")
    event = await invoke(async_db_session, instance, output, adapter=adapter)
    assert event.payload["approval"] == {"decision_id": "APR-1", "status": "pending"}
    assert event.payload["result"] == "escalated"
    assert (instance.current_state, instance.version, adapter.calls) == ("research", 0, 0)


@pytest.mark.asyncio
async def test_hard_deny_remains_blocked_even_with_approval(async_db_session):
    _, instance = await setup(async_db_session)
    adapter = Adapter()
    output = native(action="bypass_opt_out", state="research", risk="RED", founder_approval_status="approved")
    event = await invoke(async_db_session, instance, output, adapter=adapter)
    assert event.payload["result"] == "blocked"
    assert event.payload["metadata"]["gate"] == "policy"
    assert event.native_decision == output
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_model_claimed_founder_approval_cannot_authorize_red(async_db_session):
    _, instance = await setup(async_db_session)
    output = native(action="pricing_change", state="research", risk="RED", founder_approval_status="approved")
    event = await invoke(async_db_session, instance, output)
    assert event.payload["event_type"] == "approval_requested"
    assert event.native_decision == output
    assert instance.current_state == "research"


@pytest.mark.asyncio
async def test_yellow_bound_stops_and_sandbox_adapter_executes_only_when_allowed(async_db_session):
    _, instance = await setup(async_db_session, initial_state="scored")
    adapter = Adapter()
    request = {"name": "sandbox_mail", "phase": "execute", "use": "send_outreach"}
    over = native(action="send_outreach", state="sent", risk="YELLOW", integration=request, limit_name="max_discount_percent", requested_total=20)
    escalated = await invoke(async_db_session, instance, over, adapter=adapter)
    assert escalated.payload["result"] == "escalated"
    assert adapter.calls == 0
    assert instance.current_state == "scored"
    allowed = native(action="send_outreach", state="sent", risk="YELLOW", integration=request, limit_name="max_discount_percent", requested_total=5)
    event = await invoke(async_db_session, instance, allowed, adapter=adapter, key="delivery-2")
    assert event.payload["result"] == "executed_in_sandbox"
    assert event.payload["evidence_refs"][-1] == "sandbox://mail/receipt-1"
    assert (adapter.calls, instance.current_state) == (1, "sent")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,verified", [("disabled", False), ("live", True), ("sandbox", False)])
async def test_disabled_live_and_unverified_integration_cannot_execute(async_db_session, mode, verified):
    _, instance = await setup(async_db_session, initial_state="scored")
    adapter = Adapter()
    registry = deepcopy(REGISTRY)
    registry["integrations"]["sandbox_mail"].update(mode=mode, verified=verified)
    output = native(action="send_outreach", state="sent", risk="YELLOW", integration={"name": "sandbox_mail", "phase": "execute", "use": "send_outreach"})
    output["policy_intent"]["integration_mode"] = mode
    event = await invoke(async_db_session, instance, output, adapter=adapter, registry=registry)
    assert event.payload["result"] in {"failed", "blocked"}
    assert adapter.calls == 0 and instance.current_state == "scored"


@pytest.mark.asyncio
async def test_invalid_native_output_and_provider_failure_preserve_evidence(async_db_session):
    _, instance = await setup(async_db_session)
    malformed = {"action": "make_up"}
    event = await invoke(async_db_session, instance, malformed)
    assert event.native_decision == malformed
    assert event.payload["metadata"]["gate"] == "contract"
    assert instance.current_state == "research"
    failure = await invoke(async_db_session, instance, RuntimeError("provider unavailable"), key="delivery-2")
    assert failure.native_failure == {"type": "RuntimeError", "message": "provider unavailable"}
    assert failure.payload["metadata"]["gate"] == "provider"


@pytest.mark.asyncio
async def test_transition_risk_mismatch_fails_without_repair(async_db_session):
    _, instance = await setup(async_db_session)
    output = native(risk="YELLOW")
    event = await invoke(async_db_session, instance, output)
    assert event.payload["result"] == "blocked"
    assert event.payload["metadata"]["gate"] == "transition"
    assert event.native_decision["risk_level"] == "YELLOW"
    assert instance.current_state == "research"


@pytest.mark.asyncio
async def test_transition_requires_trusted_evidence_before_execution(async_db_session):
    _, instance = await setup(async_db_session)
    workflow = deepcopy(WORKFLOW)
    workflow["transitions"][0]["requirements"] = ["documented_qualification"]
    output = native()
    event = await invoke(async_db_session, instance, output, workflow=workflow)
    assert event.payload["metadata"]["gate"] == "transition"
    assert event.native_decision == output
    assert instance.current_state == "research"


@pytest.mark.asyncio
async def test_production_read_cannot_reach_stage2_adapter(async_db_session):
    _, instance = await setup(async_db_session)
    adapter = Adapter()
    registry = deepcopy(REGISTRY)
    registry["integrations"]["sandbox_mail"]["mode"] = "read_only"
    output = native(state="research", integration={"name": "sandbox_mail", "phase": "read", "use": "send_outreach"})
    output["policy_intent"]["external_write"] = False
    event = await invoke(async_db_session, instance, output, adapter=adapter, registry=registry)
    assert event.payload["metadata"]["gate"] == "integration"
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_missing_adapter_fails_with_native_decision_preserved(async_db_session):
    _, instance = await setup(async_db_session, initial_state="scored")
    output = native(action="send_outreach", state="sent", risk="YELLOW", integration={"name": "sandbox_mail", "phase": "execute", "use": "send_outreach"})
    event = await invoke(async_db_session, instance, output)
    assert event.payload["metadata"]["gate"] == "integration"
    assert event.native_decision == output
    assert instance.current_state == "scored"


@pytest.mark.asyncio
async def test_invalid_nested_native_input_is_a_contract_failure(async_db_session):
    _, instance = await setup(async_db_session)
    output = native(state="research", integration={"name": "sandbox_mail", "phase": []})
    event = await invoke(async_db_session, instance, output)
    assert event.payload["metadata"]["gate"] == "contract"
    assert event.native_decision == output


@pytest.mark.asyncio
async def test_authority_claim_in_model_output_cannot_supply_missing_input_evidence(async_db_session):
    _, instance = await setup(async_db_session, initial_state="scored")
    adapter = Adapter()
    output = native(
        action="send_outreach", state="sent", risk="YELLOW",
        integration={"name": "sandbox_mail", "phase": "execute", "use": "send_outreach"},
        requires_authority=True, authority_verified=True,
    )
    event = await invoke(async_db_session, instance, output, adapter=adapter)
    assert event.payload["result"] == "blocked"
    assert event.native_decision == output
    assert instance.current_state == "scored" and adapter.calls == 0


@pytest.mark.asyncio
async def test_invalid_event_schema_rolls_back_state_change(async_db_session):
    run, instance = await setup(async_db_session)
    schema = deepcopy(EVENT_SCHEMA)
    schema["properties"]["result"] = {"const": "impossible"}
    with pytest.raises(SandboxPersistenceError):
        await invoke(async_db_session, instance, native(), schema=schema)
    assert (instance.current_state, instance.version) == ("research", 0)
    assert await async_db_session.scalar(select(func.count()).select_from(CompanyOSSandboxEvent).where(CompanyOSSandboxEvent.sandbox_run_id == run.id)) == 0


async def pending_approval(db, *, initial_state="scored", total=20, workflow=WORKFLOW):
    _, instance = await setup(db, initial_state=initial_state)
    adapter = Adapter()
    output = native(
        action="send_outreach", state="sent", risk="YELLOW",
        integration={"name": "sandbox_mail", "phase": "execute", "use": "send_outreach"},
        limit_name="max_discount_percent", requested_total=total,
    )
    requested = await invoke(db, instance, output, adapter=adapter, workflow=workflow)
    approval = await db.scalar(select(CompanyOSSandboxApproval).where(
        CompanyOSSandboxApproval.request_event_id == requested.id,
    ))
    assert requested.payload["event_type"] == "approval_requested"
    assert approval.status == "pending" and adapter.calls == 0
    return instance, output, approval, adapter


async def resolve(db, approval, status="approved", **kwargs):
    return await resolve_sandbox_approval(
        db, approval_id=approval.id, status=status, founder_id="founder-1",
        founder_minutes=2.5, resolution_key="resolve-1", event_schema=EVENT_SCHEMA,
        workflow=WORKFLOW, **kwargs,
    )


async def resume(db, approval, adapter=None, **kwargs):
    return await resume_sandbox_approval(
        db, approval_id=approval.id, workflow=kwargs.pop("workflow", WORKFLOW),
        policy=kwargs.pop("policy", POLICY), integration_registry=kwargs.pop("registry", REGISTRY),
        event_schema=EVENT_SCHEMA, canonical_agents=["acquisition", "governance"],
        canonical_handoffs=["governance"], canonical_actions=[
            "score_against_icp", "send_outreach", "pricing_change", "bypass_opt_out",
        ], synthetic_adapters={"sandbox_mail": adapter} if adapter else {},
    )


@pytest.mark.asyncio
async def test_approved_resolution_resumes_once_and_preserves_native_decision(async_db_session):
    instance, original, approval, adapter = await pending_approval(async_db_session)
    resolution = await resolve(async_db_session, approval)
    assert resolution.payload["autonomy"] == {"class": "A1", "founder_minutes": 2.5}
    assert resolution.payload["metadata"]["founder_id"] == "founder-1"
    assert resolution.native_decision == original
    assert await resolve(async_db_session, approval) is resolution
    resumed = await resume(async_db_session, approval, adapter)
    assert resumed.payload["approval"] == {"decision_id": "APR-1", "status": "approved"}
    assert resumed.native_decision == original
    assert resumed.payload["event_type"] == "transition_completed"
    assert (instance.current_state, instance.version, adapter.calls) == ("sent", 1, 1)
    assert await resume(async_db_session, approval, adapter) is resumed
    assert adapter.calls == 1
    Draft202012Validator(EVENT_SCHEMA).validate(export_sandbox_event(resumed))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "needs_more_evidence", "expired", "cancelled"])
async def test_non_executable_resolutions_never_resume(async_db_session, status):
    instance, _, approval, adapter = await pending_approval(async_db_session)
    event = await resolve(async_db_session, approval, status)
    assert event.payload["approval"]["status"] == status
    assert event.payload["result"] == "blocked"
    with pytest.raises(SandboxApprovalError, match="does not permit execution"):
        await resume(async_db_session, approval, adapter)
    assert adapter.calls == 0 and instance.current_state == "scored"
    with pytest.raises(SandboxApprovalError, match="only pending"):
        await resolve_sandbox_approval(
            async_db_session, approval_id=approval.id, status="approved",
            founder_id="founder-2", founder_minutes=1,
            resolution_key="different", event_schema=EVENT_SCHEMA, workflow=WORKFLOW,
        )


@pytest.mark.asyncio
async def test_founder_correction_is_separate_from_original_and_rechecked(async_db_session):
    instance, original, approval, adapter = await pending_approval(async_db_session)
    corrected = deepcopy(original)
    corrected["policy_intent"]["requested_total"] = 5
    resolution = await resolve(async_db_session, approval, "modified", corrected_decision=corrected)
    assert resolution.native_decision == original
    assert resolution.payload["autonomy"]["class"] == "A2"
    assert resolution.payload["metadata"]["corrected_decision"] == corrected
    resumed = await resume(async_db_session, approval, adapter)
    assert resumed.native_decision == original
    assert resumed.payload["metadata"]["founder_corrected_decision"] == corrected
    assert resumed.payload["autonomy"]["class"] == "A2"
    assert adapter.calls == 1 and instance.current_state == "sent"


@pytest.mark.asyncio
async def test_modified_hard_deny_cannot_be_founder_overridden(async_db_session):
    instance, original, approval, adapter = await pending_approval(async_db_session)
    corrected = native(action="bypass_opt_out", state="scored", risk="GREEN")
    await resolve(async_db_session, approval, "modified", corrected_decision=corrected)
    failure = await resume(async_db_session, approval, adapter)
    assert failure.payload["event_type"] == "failure_detected"
    assert failure.native_decision == original
    assert failure.payload["result"] == "blocked"
    assert instance.current_state == "scored" and adapter.calls == 0
    assert await resume(async_db_session, approval, adapter) is failure


@pytest.mark.asyncio
async def test_manual_execution_is_explicit_a3_with_synthetic_evidence(async_db_session):
    _, original, approval, adapter = await pending_approval(async_db_session)
    await resolve(async_db_session, approval, manual_evidence_ref="sandbox://founder/manual-1")
    event = await resume(async_db_session, approval, adapter)
    assert event.payload["autonomy"]["class"] == "A3"
    assert "sandbox://founder/manual-1" in event.payload["evidence_refs"]
    assert event.native_decision == original
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_approval_cannot_resume_on_stale_workflow(async_db_session):
    instance, _, approval, adapter = await pending_approval(async_db_session)
    await resolve(async_db_session, approval)
    instance.version += 1
    with pytest.raises(SandboxApprovalError, match="workflow changed"):
        await resume(async_db_session, approval, adapter)
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_resume_rechecks_registry_after_founder_approval(async_db_session):
    instance, original, approval, adapter = await pending_approval(async_db_session)
    await resolve(async_db_session, approval)
    registry = deepcopy(REGISTRY)
    registry["integrations"]["sandbox_mail"]["mode"] = "live"
    failure = await resume(async_db_session, approval, adapter, registry=registry)
    assert failure.payload["result"] == "blocked"
    assert failure.native_decision == original
    assert adapter.calls == 0 and instance.current_state == "scored"


@pytest.mark.asyncio
async def test_modified_claimed_authority_does_not_replace_input_evidence(async_db_session):
    instance, original, approval, adapter = await pending_approval(async_db_session)
    corrected = deepcopy(original)
    corrected["policy_intent"].update(
        requested_total=5, requires_authority=True, authority_verified=True,
    )
    await resolve(async_db_session, approval, "modified", corrected_decision=corrected)
    failure = await resume(async_db_session, approval, adapter)
    assert failure.payload["result"] == "blocked"
    assert failure.native_decision == original
    assert adapter.calls == 0 and instance.current_state == "scored"


@pytest.mark.asyncio
async def test_founder_identity_minutes_and_correction_are_required(async_db_session):
    _, _, approval, _ = await pending_approval(async_db_session)
    with pytest.raises(SandboxApprovalError, match="founder minutes"):
        await resolve_sandbox_approval(
            async_db_session, approval_id=approval.id, status="approved",
            founder_id="founder-1", founder_minutes=float("nan"),
            resolution_key="invalid", event_schema=EVENT_SCHEMA, workflow=WORKFLOW,
        )
    with pytest.raises(SandboxApprovalError, match="modified status"):
        await resolve(async_db_session, approval, "modified")
    assert approval.status == "pending"


@pytest.mark.asyncio
async def test_terminal_action_emits_once_and_blocks_second_transition(async_db_session):
    workflow = deepcopy(WORKFLOW)
    workflow["terminal_states"] = ["sent"]
    instance, _, approval, adapter = await pending_approval(async_db_session, workflow=workflow)
    await resolve_sandbox_approval(
        async_db_session, approval_id=approval.id, status="approved", founder_id="founder-1",
        founder_minutes=2.5, resolution_key="resolve-1", event_schema=EVENT_SCHEMA, workflow=workflow,
    )
    event = await resume(async_db_session, approval, adapter, workflow=workflow)
    assert event.payload["event_type"] == "terminal_outcome"
    assert instance.terminal and instance.current_state == "sent"
    assert await resume(async_db_session, approval, adapter, workflow=workflow) is event
    assert adapter.calls == 1
    assert await async_db_session.scalar(select(func.count()).select_from(CompanyOSSandboxEvent).where(
        CompanyOSSandboxEvent.event_type == "terminal_outcome",
    )) == 1


@pytest.mark.asyncio
async def test_db_rollback_retry_uses_stable_synthetic_idempotency_key(async_db_session, monkeypatch):
    workflow = deepcopy(WORKFLOW)
    workflow["terminal_states"] = ["sent"]
    instance, _, approval, _ = await pending_approval(async_db_session, workflow=workflow)
    await resolve_sandbox_approval(
        async_db_session, approval_id=approval.id, status="approved", founder_id="founder-1",
        founder_minutes=2.5, resolution_key="resolve-1", event_schema=EVENT_SCHEMA, workflow=workflow,
    )
    approval_id = approval.id
    instance_id = instance.id
    await async_db_session.commit()

    class IdempotentAdapter:
        def __init__(self):
            self.keys = []
            self.effects = set()

        async def execute(self, decision, *, idempotency_key):
            self.keys.append(idempotency_key)
            self.effects.add(idempotency_key)
            return "sandbox://mail/stable-receipt"

    adapter = IdempotentAdapter()
    original_append = approval_module._append

    async def fail_after_append(*args, **kwargs):
        await original_append(*args, **kwargs)
        raise SandboxPersistenceError("simulated database failure after adapter execution")

    monkeypatch.setattr(approval_module, "_append", fail_after_append)
    with pytest.raises(SandboxPersistenceError, match="simulated database failure"):
        await resume(async_db_session, approval, adapter, workflow=workflow)
    await async_db_session.rollback()
    monkeypatch.setattr(approval_module, "_append", original_append)

    approval = await async_db_session.get(CompanyOSSandboxApproval, approval_id)
    event = await resume(async_db_session, approval, adapter, workflow=workflow)
    assert event.payload["event_type"] == "terminal_outcome"
    assert len(adapter.keys) == 2 and len(adapter.effects) == 1
    assert adapter.keys[0] == adapter.keys[1] == f"stage2-approval-resume:{approval_id}"
    assert await async_db_session.scalar(select(func.count()).select_from(CompanyOSSandboxEvent).where(
        CompanyOSSandboxEvent.event_type == "terminal_outcome",
    )) == 1
    assert (await async_db_session.get(type(instance), instance_id)).terminal
