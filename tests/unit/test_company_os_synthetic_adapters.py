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
from tests.unit.company_os_stage2_facts import eligible_world
from tests.unit.test_company_os_sandbox_coordinator import EVENT_SCHEMA, POLICY, REGISTRY, native


def world():
    return SyntheticWorld(run_id="stage2-fixture", frozen_at=datetime(2026, 9, 25, tzinfo=UTC))


@pytest.mark.asyncio
async def test_prospect_reply_and_meeting_are_synthetic_and_replay_safe():
    state = world()
    state.contacts["p-1"] = {"consent_verified": True, "suppressed": False}
    outbound = SyntheticAdapter(state, "mail")
    decision = {"action": "send_outreach"}
    receipt = await outbound.execute(decision, idempotency_key="outbound-1", run_id=state.run_id, recipient_id="p-1")
    assert receipt == await outbound.execute(decision, idempotency_key="outbound-1", run_id=state.run_id, recipient_id="p-1")
    assert len(state.outcomes) == 1 and state.outcomes[receipt]["external_side_effect"] is False
    reply = {"entity_id": "p-1", "meeting_confirmed": True}
    assert state.receive("reply", "reply-1", reply) == state.receive("reply", "reply-1", reply)
    meeting = await SyntheticAdapter(state, "calendar").execute(
        {"action": "book_or_handoff_meeting"}, idempotency_key="booking-1", run_id=state.run_id, recipient_id="p-1",
    )
    assert state.outcomes[meeting]["kind"] == "calendar"
    with pytest.raises(SyntheticCapabilityError, match="different native decision"):
        await outbound.execute({"action": "other"}, idempotency_key="outbound-1", run_id=state.run_id, recipient_id="p-1")


@pytest.mark.asyncio
async def test_opt_out_or_missing_consent_blocks_all_later_messages():
    state = world()
    state.contacts["lead-1"] = {"consent_verified": True, "suppressed": False}
    mail = SyntheticAdapter(state, "mail")
    await mail.execute({"action": "send_recovery_message"}, idempotency_key="initial", run_id=state.run_id, recipient_id="lead-1")
    state.receive("opt_out", "stop-1", {"entity_id": "lead-1"})
    assert state.contacts["lead-1"]["suppressed"] is True
    with pytest.raises(SyntheticCapabilityError, match="ineligible or opted out"):
        await mail.execute({"action": "send_recovery_message"}, idempotency_key="followup", run_id=state.run_id, recipient_id="lead-1")
    assert len(state.outcomes) == 1
    with pytest.raises(SyntheticCapabilityError, match="different facts"):
        state.receive("opt_out", "stop-1", {"entity_id": "lead-1", "opt_out": False})
    state.contacts["lead-2"] = {"consent_verified": False}
    with pytest.raises(SyntheticCapabilityError, match="ineligible"):
        await SyntheticAdapter(state, "mail").execute(
            {"action": "send_recovery_message"}, idempotency_key="no-consent", run_id=state.run_id, recipient_id="lead-2",
        )


@pytest.mark.asyncio
async def test_customer_authority_lead_intake_and_payment_failure_signals():
    state = world()
    state.receive("authority", "customer-intake", {"entity_id": "c-1", "authority_verified": False})
    with pytest.raises(SyntheticCapabilityError, match="authority"):
        await SyntheticAdapter(state, "authority").execute(
            {"action": "activate_approved_workflows"}, idempotency_key="premature", run_id=state.run_id, recipient_id="c-1",
        )
    state.receive("authority", "customer-authorized", {"entity_id": "c-1", "authority_verified": True})
    await SyntheticAdapter(state, "authority").execute(
        {"action": "activate_approved_workflows"}, idempotency_key="activation", run_id=state.run_id, recipient_id="c-1",
    )
    state.receive("lead", "inbound-lead", {"entity_id": "l-1", "eligible": True})
    await SyntheticAdapter(state, "lead").execute(
        {"action": "route_to_authoritative_next_step"}, idempotency_key="route", run_id=state.run_id, recipient_id="l-1",
    )
    state.receive("payment", "payment-failed", {"entity_id": "c-1", "status": "failed"})
    payment = await SyntheticAdapter(state, "payment").execute(
        {"action": "record_payment_signal"}, idempotency_key="signal", run_id=state.run_id, recipient_id="c-1",
    )
    assert state.outcomes[payment]["external_side_effect"] is False
    assert len(state.outcomes) == 3


@pytest.mark.asyncio
async def test_provider_failure_and_lost_receipt_recover_with_no_duplicate_effect():
    state = world()
    state.contacts["p-1"] = {"consent_verified": True, "suppressed": False}
    adapter = SyntheticAdapter(state, "mail")
    decision = {"action": "send_outreach"}
    state.inject_failure("mail", "before")
    with pytest.raises(SyntheticCapabilityError, match="provider failure"):
        await adapter.execute(decision, idempotency_key="before", run_id=state.run_id, recipient_id="p-1")
    assert state.outcomes == {}
    state.inject_failure("mail", "after", after_effect=True)
    with pytest.raises(SyntheticCapabilityError, match="receipt timeout"):
        await adapter.execute(decision, idempotency_key="after", run_id=state.run_id, recipient_id="p-1")
    assert len(state.outcomes) == 1
    ref = await adapter.execute(decision, idempotency_key="after", run_id=state.run_id, recipient_id="p-1")
    assert ref in state.outcomes and len(state.outcomes) == 1


@pytest.mark.asyncio
async def test_coordinator_uses_concrete_adapter_and_blocks_opted_out_contact(async_db_session):
    state = eligible_world()
    run = await create_sandbox_run(
        async_db_session, run_id=state.run_id, company_slug="acqivo",
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
    adapter = SyntheticAdapter(state, "mail")

    async def decide(_):
        return native(
            action="send_outreach", state="sent", risk="YELLOW",
            integration={"name": "sandbox_mail", "phase": "execute", "use": "acquisition_email"},
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
    assert blocked.payload["result"] == "blocked"
    assert blocked.payload["risk_level"] == "RED"
    assert blocked.payload["metadata"]["gate"] == "compliance"
    assert len(state.outcomes) == 1


@pytest.mark.asyncio
async def test_run_and_recipient_are_explicit_and_isolated():
    state = world()
    state.set_contact("eligible", {"address": "ok@example.test", "consent_verified": True})
    state.set_contact("blocked", {"address": "no@example.test", "consent_verified": True})
    state.receive("opt_out", "stop", {"entity_id": "blocked"})
    adapter = SyntheticAdapter(state, "mail")
    with pytest.raises(SyntheticCapabilityError, match="run"):
        await adapter.execute({}, idempotency_key="key", run_id="other", recipient_id="eligible")
    for recipient in ("blocked", "unknown", ["eligible"]):
        with pytest.raises(SyntheticCapabilityError):
            await adapter.execute({"recipient_id": "eligible"}, idempotency_key="key",
                                  run_id=state.run_id, recipient_id=recipient)
    assert not state.outcomes
    receipt = await adapter.execute({}, idempotency_key="key", run_id=state.run_id,
                                    recipient_id="eligible")
    assert state.outcomes[receipt]["recipient_id"] == "eligible"


@pytest.mark.parametrize("channel,facts", [
    ("opt_out", {"entity_id": "unknown"}),
    ("reply", {"entity_id": "unknown", "body": " STOP "}),
    ("mail", {"entity_id": "unknown", "unsubscribe": True}),
    ("mail", {"address": " SHARED@EXAMPLE.TEST ", "opt_out": True}),
])
@pytest.mark.asyncio
async def test_unknown_stop_is_retained_and_applied_to_new_contacts(channel, facts):
    state = world()
    state.receive(channel, "stop", facts)
    state.receive(channel, "stop", facts)
    state.receive("contact", "create", {
        "entity_id": "unknown", "address": "shared@example.test", "consent_verified": True,
    })
    state.set_contact("alias", {"address": " SHARED@example.test ", "consent_verified": True})
    for recipient in ("unknown", "alias"):
        with pytest.raises(SyntheticCapabilityError, match="opted out"):
            await SyntheticAdapter(state, "mail").execute(
                {}, idempotency_key=recipient, run_id=state.run_id, recipient_id=recipient,
            )
    assert not state.outcomes
    state.set_contact("unknown", {"address": "new@example.test", "consent_verified": True})
    assert state.is_suppressed("unknown")
    assert "new@example.test" in state.suppressed_recipients


def test_address_only_stop_and_signal_history_are_deduplicated_snapshots():
    state = world()
    state.receive("opt_out", "one", {"address": " A@EXAMPLE.TEST "})
    state.receive("opt_out", "two", {"address": "a@example.test"})
    assert state.suppressed_recipients == {"a@example.test"}
    for channel in ("lead", "payment", "reply", "mail"):
        facts = {"entity_id": "p-1", "status": "first"}
        state.receive(channel, "first", facts)
        state.receive(channel, "first", facts)
        facts["status"] = "mutated"
        state.receive(channel, "second", {"entity_id": "p-1", "status": "second"})
    assert len(state.signals["p-1"]) == 8
    assert [s["facts"]["status"] for s in state.signals["p-1"]] == ["first", "second"] * 4
    assert state.payments["p-1"]["status"] == state.mail["p-1"]["status"] == "second"


def test_queue_and_dispatch_preserve_message_and_consumer_identity():
    state = world()
    state.set_contact("p-1", {"address": "a@example.test", "consent_verified": True})
    facts = {"entity_id": "io-1", "item_id": "message-1", "recipient_id": "p-1",
             "original_action": "send", "original_adapter_key": "stable"}
    state.receive("queued_message", "queue", facts)
    assert state.queue["io-1"]["status"] == "queued"
    for invalid in ({**facts, "entity_id": "io-2"},
                    {**facts, "item_id": "other", "recipient_id": ["p-1"]}):
        with pytest.raises(SyntheticCapabilityError):
            state.receive("queued_message", "invalid", invalid)
    decision = {"action": "send", "state_after": "sent"}
    payload = {"body": "original"}
    consumers = {1: {"expected_state_before": "ready", "expected_version": 0,
                     "consumer_decision": decision}}
    record = state.prepare_dispatch(
        message_key="message-1", recipient_id="p-1", adapter_kind="mail",
        adapter_idempotency_key="stable", origin_workflow_instance_id=1,
        origin_state_before="ready", origin_workflow_version=0,
        frozen_native_decision=decision, rendered_payload=payload, consumers=consumers,
    )
    decision["action"] = "changed"
    payload["body"] = "changed"
    consumers.clear()
    assert record["frozen_native_decision"]["action"] == "send"
    assert record["rendered_payload"]["body"] == "original"
    assert record["consumers"][1]["consumer_decision"]["state_after"] == "sent"
    assert record["status"] == "prepared" and record["receipt"] is None
    assert "completed" not in record
    assert record["dispatch_id"] == state.dispatch_id("message-1")
    assert record["dispatch_id"] != SyntheticWorld("other", state.frozen_at).dispatch_id("message-1")


@pytest.mark.parametrize("body", ["STOP.", "Stop emailing me", "please unsubscribe me",
                                  "Please OPT-OUT!", "optout now", "Please remove me.", "CANCEL this"])
@pytest.mark.parametrize("channel", ["reply", "mail"])
@pytest.mark.parametrize("known", [True, False])
def test_opt_out_phrases_keep_known_and_unknown_contacts_suppressed(body, channel, known):
    from tests.unit.company_os_stage2_facts import eligible_world
    state = eligible_world()
    facts = state.contacts["p-1"].copy()
    if not known:
        del state.contacts["p-1"]
    state.receive(channel, "stop-phrase", {"entity_id": "p-1", "body": body})
    assert "entity:p-1" in state.pending_opt_outs
    if not known:
        state.set_contact("p-1", facts)
    assert state.is_suppressed("p-1")
    assert state.contacts["p-1"]["address"] in state.suppressed_recipients
