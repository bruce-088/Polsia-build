"""Stage 2 synthetic services preserve consent, evidence, and retry identity."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from app.services.company_os_sandbox_coordinator import coordinate_sandbox_action
from app.services.company_os_sandbox_service import create_sandbox_run, create_workflow_instance
from app.services.company_os_synthetic_adapters import (
    SyntheticAdapter,
    SyntheticCapabilityError,
    SyntheticWorld,
)
from tests.unit.test_company_os_sandbox_coordinator import EVENT_SCHEMA, POLICY, REGISTRY, native


def world():
    return SyntheticWorld(run_id="stage2-fixture", frozen_at=datetime(2026, 9, 25, tzinfo=UTC))


@pytest.mark.asyncio
async def test_prospect_reply_and_meeting_are_synthetic_and_replay_safe():
    state = world()
    state.contacts["p-1"] = {"consent_verified": True, "suppressed": False}
    outbound = SyntheticAdapter(state, "mail", "p-1")
    decision = {"action": "send_outreach"}
    receipt = await outbound.execute(decision, idempotency_key="outbound-1")
    assert receipt == await outbound.execute(decision, idempotency_key="outbound-1")
    assert len(state.outcomes) == 1 and state.outcomes[receipt]["external_side_effect"] is False
    reply = {"entity_id": "p-1", "meeting_confirmed": True}
    assert state.receive("reply", "reply-1", reply) == state.receive("reply", "reply-1", reply)
    meeting = await SyntheticAdapter(state, "calendar", "p-1").execute(
        {"action": "book_or_handoff_meeting"}, idempotency_key="booking-1",
    )
    assert state.outcomes[meeting]["kind"] == "calendar"
    with pytest.raises(SyntheticCapabilityError, match="different native decision"):
        await outbound.execute({"action": "other"}, idempotency_key="outbound-1")


@pytest.mark.asyncio
async def test_opt_out_or_missing_consent_blocks_all_later_messages():
    state = world()
    state.contacts["lead-1"] = {"consent_verified": True, "suppressed": False}
    mail = SyntheticAdapter(state, "mail", "lead-1")
    await mail.execute({"action": "send_recovery_message"}, idempotency_key="initial")
    state.receive("opt_out", "stop-1", {"entity_id": "lead-1"})
    assert state.contacts["lead-1"]["suppressed"] is True
    with pytest.raises(SyntheticCapabilityError, match="ineligible or opted out"):
        await mail.execute({"action": "send_recovery_message"}, idempotency_key="followup")
    assert len(state.outcomes) == 1
    with pytest.raises(SyntheticCapabilityError, match="different facts"):
        state.receive("opt_out", "stop-1", {"entity_id": "lead-1", "opt_out": False})
    state.contacts["lead-2"] = {"consent_verified": False}
    with pytest.raises(SyntheticCapabilityError, match="ineligible"):
        await SyntheticAdapter(state, "mail", "lead-2").execute(
            {"action": "send_recovery_message"}, idempotency_key="no-consent",
        )


@pytest.mark.asyncio
async def test_customer_authority_lead_intake_and_payment_failure_signals():
    state = world()
    state.receive("authority", "customer-intake", {"entity_id": "c-1", "authority_verified": False})
    with pytest.raises(SyntheticCapabilityError, match="authority"):
        await SyntheticAdapter(state, "authority", "c-1").execute(
            {"action": "activate_approved_workflows"}, idempotency_key="premature",
        )
    state.receive("authority", "customer-authorized", {"entity_id": "c-1", "authority_verified": True})
    await SyntheticAdapter(state, "authority", "c-1").execute(
        {"action": "activate_approved_workflows"}, idempotency_key="activation",
    )
    state.receive("lead", "inbound-lead", {"entity_id": "l-1", "eligible": True})
    await SyntheticAdapter(state, "lead", "l-1").execute(
        {"action": "route_to_authoritative_next_step"}, idempotency_key="route",
    )
    state.receive("payment", "payment-failed", {"entity_id": "c-1", "status": "failed"})
    payment = await SyntheticAdapter(state, "payment", "c-1").execute(
        {"action": "record_payment_signal"}, idempotency_key="signal",
    )
    assert state.outcomes[payment]["external_side_effect"] is False
    assert len(state.outcomes) == 3


@pytest.mark.asyncio
async def test_provider_failure_and_lost_receipt_recover_with_no_duplicate_effect():
    state = world()
    state.contacts["p-1"] = {"consent_verified": True, "suppressed": False}
    adapter = SyntheticAdapter(state, "mail", "p-1")
    decision = {"action": "send_outreach"}
    state.inject_failure("mail", "before")
    with pytest.raises(SyntheticCapabilityError, match="provider failure"):
        await adapter.execute(decision, idempotency_key="before")
    assert state.outcomes == {}
    state.inject_failure("mail", "after", after_effect=True)
    with pytest.raises(SyntheticCapabilityError, match="receipt timeout"):
        await adapter.execute(decision, idempotency_key="after")
    assert len(state.outcomes) == 1
    ref = await adapter.execute(decision, idempotency_key="after")
    assert ref in state.outcomes and len(state.outcomes) == 1


@pytest.mark.asyncio
async def test_coordinator_uses_concrete_adapter_and_blocks_opted_out_contact(async_db_session):
    state = world()
    state.contacts["p-1"] = {"consent_verified": True, "suppressed": False}
    run = await create_sandbox_run(
        async_db_session, run_id="synthetic-path", company_slug="acqivo",
        protocol_version="0.1.0", input_manifest={"fixture": "synthetic-only"},
    )
    workflow = {
        "id": "prospect_to_meeting", "states": ["ready_to_send", "sent"],
        "terminal_states": [], "transitions": [{
            "from": "ready_to_send", "to": "sent", "action": "send_outreach",
            "risk_level": "YELLOW", "integration": "sandbox_mail",
        }],
    }
    schema = deepcopy(EVENT_SCHEMA)
    schema["properties"]["entity_id"] = {"type": "string", "minLength": 1}
    adapter = SyntheticAdapter(state, "mail", "p-1")

    async def decide(_):
        return native(
            action="send_outreach", state="sent", risk="YELLOW",
            integration={"name": "sandbox_mail", "phase": "execute", "use": "send_outreach"},
        )

    async def invoke(entity_id, key):
        instance = await create_workflow_instance(
            async_db_session, sandbox_run_id=run.id, workflow_id="prospect_to_meeting",
            entity_type="prospect", entity_id=entity_id, initial_state="ready_to_send",
        )
        event = await coordinate_sandbox_action(
            async_db_session, workflow_instance_id=instance.id, expected_version=0,
            idempotency_key=key,
            synthetic_input={"evidence_refs": [f"sandbox://input/{entity_id}"],
                             "primary_classification": "acquisition"},
            decide=decide, workflow=workflow, policy=POLICY,
            integration_registry=REGISTRY, event_schema=schema,
            canonical_agents=["acquisition", "governance"], canonical_handoffs=["governance"],
            canonical_actions=["send_outreach"], synthetic_adapters={"sandbox_mail": adapter},
        )
        return instance, event

    first, sent = await invoke("p-1", "first")
    assert first.current_state == "sent" and sent.payload["result"] == "executed_in_sandbox"
    assert sent.payload["evidence_refs"][-1] in state.outcomes
    state.receive("opt_out", "stop", {"entity_id": "p-1"})
    second, blocked = await invoke("p-2", "second")
    assert second.current_state == "ready_to_send"
    assert blocked.payload["result"] == "failed"
    assert blocked.payload["metadata"]["gate"] == "integration"
    assert len(state.outcomes) == 1
