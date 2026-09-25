"""Stage B-II ledger, rollback, compliance and exception regression proofs."""

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.company_os_sandbox_coordinator as coordinator
from app.config import settings
from app.models.company_os_sandbox import (
    CompanyOSSandboxApproval,
    CompanyOSSandboxEvent,
    CompanyOSSandboxRun,
    CompanyOSWorkflowInstance,
)
from app.services.company_os_sandbox_service import (
    SandboxPersistenceError,
    append_sandbox_event,
    create_workflow_instance,
)
from app.services.company_os_synthetic_adapters import SyntheticAdapter
from tests import postgres
from tests.unit.company_os_stage2_facts import AT, eligible_world
from tests.unit.test_company_os_sandbox_coordinator import (
    EVENT_SCHEMA,
    WORKFLOW,
    invoke,
    native,
    pending_approval,
    resolve,
    resume,
    setup,
)

postgres_url = postgres.postgres_url


@pytest.fixture(autouse=True)
def sandbox(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_mode", True)


def send():
    return native(action="send_outreach", state="sent", risk="YELLOW", integration={
        "name": "sandbox_mail", "phase": "execute", "use": "acquisition_email",
    })


def adapter():
    return SyntheticAdapter(eligible_world(), "mail")


def action_key(instance):
    return f"stage2-action:{instance.sandbox_run_id}:{instance.id}:scored:send_outreach:p-1:0"


async def event_count(db):
    return await db.scalar(select(func.count()).select_from(CompanyOSSandboxEvent))


@pytest.mark.parametrize("failure", ["persistence", "caller", "timeout"])
async def test_direct_effect_survives_rollback_or_committed_failure(async_db_session, monkeypatch, failure):
    db = async_db_session
    _, instance = await setup(db, initial_state="scored")
    identity = instance.id
    await db.commit()
    provider = adapter()
    original = send()
    append = coordinator._append
    if failure == "persistence":
        async def fail(*args, **kwargs):
            await append(*args, **kwargs)
            raise SandboxPersistenceError("forced persistence failure")
        monkeypatch.setattr(coordinator, "_append", fail)
        with pytest.raises(SandboxPersistenceError, match="forced"):
            await invoke(db, instance, original, adapter=provider)
    else:
        if failure == "timeout":
            provider.world.inject_failure("mail", action_key(instance), after_effect=True)
        event = await invoke(db, instance, original, adapter=provider)
        if failure == "timeout":
            assert event.event_type == "failure_detected"
            assert event.payload["metadata"]["dispatch_attempted"] is True
            assert event.payload["metadata"]["dispatch_id"]
            await db.commit()
    if failure != "timeout":
        await db.rollback()
    monkeypatch.setattr(coordinator, "_append", append)
    instance = await db.get(CompanyOSWorkflowInstance, identity)
    # If called, this model produces an error event, making the success assertion fail.
    event = await invoke(db, instance, AssertionError("model must not run"), adapter=provider, key="retry")
    assert event.native_decision == original
    assert event.result == "executed_in_sandbox"
    assert event.payload["metadata"]["reconciled"] is True
    assert event.payload["metadata"]["adapter_idempotency_key"] == action_key_for_record(provider)
    assert len(provider.world.outcomes) == 1
    assert next(iter(provider.world.outcomes.values()))["rendered_payload"]["fills"] == {"name": "Pat"}
    assert await db.scalar(select(func.count()).select_from(CompanyOSSandboxEvent).where(
        CompanyOSSandboxEvent.result == "executed_in_sandbox",
    )) == 1


def action_key_for_record(provider):
    return next(iter(provider.world.dispatches.values()))["adapter_idempotency_key"]


async def test_approval_success_then_caller_rollback_reconciles(async_db_session):
    db = async_db_session
    instance, _, approval, _ = await pending_approval(db)
    await resolve(db, approval)
    approval_id, instance_id = approval.id, instance.id
    await db.commit()
    provider = adapter()
    first = await resume(db, approval, provider)
    receipt = first.payload["evidence_refs"][-1]
    await db.rollback()
    approval = await db.get(CompanyOSSandboxApproval, approval_id)
    provider.world.ingest_opt_out({"entity_id": "p-1"})
    second = await resume(db, approval, provider)
    assert second.payload["metadata"]["reconciled"] is True
    assert second.payload["evidence_refs"][-1] == receipt
    assert len(provider.world.outcomes) == 1
    assert (await db.get(CompanyOSWorkflowInstance, instance_id)).current_state == "sent"
    assert await resume(db, approval, provider) is second


@pytest.mark.parametrize("mutation", ["stop", "revoke"])
async def test_prepared_retry_freshly_checks_compliance(async_db_session, mutation):
    db = async_db_session
    _, instance = await setup(db, initial_state="scored")
    provider = adapter()
    provider.world.inject_failure("mail", action_key(instance))
    failure = await invoke(db, instance, send(), adapter=provider)
    assert failure.event_type == "failure_detected"
    assert next(iter(provider.world.dispatches.values()))["status"] == "prepared"
    if mutation == "stop":
        provider.world.ingest_opt_out({"entity_id": "p-1"})
    else:
        provider.world.templates["approved"]["revoked_at"] = AT
    event = await invoke(db, instance, AssertionError("no new model decision"), adapter=provider, key="retry")
    assert (event.risk_level, event.result, event.event_type) == ("RED", "blocked", "transition_attempted")
    assert event.payload["approval"] is None
    assert event.primary_classification == "acquisition"
    assert event.payload["compliance"]["approvable"] is False
    assert event.payload["compliance"]["evidence_hashes"]
    assert event.payload["compliance"]["proposed_remedy"]
    assert not provider.world.outcomes
    assert (instance.current_state, instance.version) == ("scored", 0)


@pytest.mark.parametrize("error", [TypeError, KeyError, ValueError])
@pytest.mark.parametrize("approved", [False, True])
async def test_adapter_programming_errors_propagate_without_failure_event(async_db_session, error, approved):
    db = async_db_session
    class Broken(SyntheticAdapter):
        async def execute(self, *args, **kwargs):
            raise error("programming error")
    provider = Broken(eligible_world(), "mail")
    if approved:
        instance, _, approval, _ = await pending_approval(db)
        await resolve(db, approval)
    else:
        _, instance = await setup(db, initial_state="scored")
    before = await event_count(db)
    instance_id = instance.id
    approval_id = approval.id if approved else None
    await db.commit()
    with pytest.raises(error, match="programming error"):
        if approved:
            await resume(db, approval, provider)
        else:
            await invoke(db, instance, send(), adapter=provider)
    assert await event_count(db) == before
    assert not provider.world.outcomes
    assert next(iter(provider.world.dispatches.values()))["status"] == "prepared"
    assert (instance.current_state, instance.version) == ("scored", 0)
    await db.rollback()
    repaired = SyntheticAdapter(provider.world, "mail")
    if approved:
        approval = await db.get(CompanyOSSandboxApproval, approval_id)
        completed = await resume(db, approval, repaired)
    else:
        instance = await db.get(CompanyOSWorkflowInstance, instance_id)
        completed = await invoke(db, instance, AssertionError("prepared decision must be reused"),
                                 adapter=repaired, key="repaired")
    assert completed.result == "executed_in_sandbox"
    assert len(provider.world.outcomes) == 1


async def test_approval_cannot_override_new_suppression(async_db_session):
    db = async_db_session
    instance, _, approval, _ = await pending_approval(db)
    await resolve(db, approval)
    provider = adapter()
    provider.world.ingest_opt_out({"entity_id": "p-1"})
    event = await resume(db, approval, provider)
    assert (event.risk_level, event.result) == ("RED", "blocked")
    assert event.payload["approval"] is None
    assert event.payload["metadata"]["override_attempt"] is True
    assert not provider.world.outcomes and instance.version == 0


@pytest.mark.parametrize("blocked_result,failed_rules", [("blocked", []), ("executed_in_sandbox", ["BASE-01"])])
async def test_blocked_event_cannot_apply_valid_red_transition(async_db_session, blocked_result, failed_rules):
    db = async_db_session
    _, instance = await setup(db, initial_state="scored")
    workflow = deepcopy(WORKFLOW)
    workflow["transitions"][1]["risk_level"] = "RED"
    payload = {
        "event_id": "blocked", "state_before": "scored", "state_after": "sent",
        "risk_level": "RED", "action": "send_outreach", "result": blocked_result,
        "event_type": "transition_completed", "compliance": {"failed_rule_ids": failed_rules},
    }
    with pytest.raises(SandboxPersistenceError, match="blocked events"):
        await append_sandbox_event(db, workflow_instance_id=instance.id, expected_version=0,
                                   idempotency_key="blocked", event_payload=payload, event_schema={},
                                   workflow_definition=workflow, native_decision=send())
    assert (instance.current_state, instance.version) == ("scored", 0)
    assert await event_count(db) == 0


async def test_direct_terminal_outcome_rejects_later_delivery(async_db_session):
    _, instance = await setup(async_db_session, initial_state="scored")
    workflow = deepcopy(WORKFLOW)
    workflow["terminal_states"] = ["sent"]
    provider = adapter()
    event = await invoke(async_db_session, instance, send(), adapter=provider, workflow=workflow)
    assert event.event_type == "terminal_outcome" and instance.terminal
    with pytest.raises(coordinator.SandboxCoordinatorError, match="terminal"):
        await invoke(async_db_session, instance, send(), adapter=provider, key="later", version=1, workflow=workflow)
    assert len(provider.world.outcomes) == 1


async def test_orphan_is_reported_once_and_never_replayed(async_db_session):
    db = async_db_session
    _, instance = await setup(db, initial_state="scored")
    identity = instance.id
    await db.commit()
    provider = adapter()
    await invoke(db, instance, send(), adapter=provider)
    await db.rollback()
    instance = await db.get(CompanyOSWorkflowInstance, identity)
    # Represent a later occurrence reached by another committed transition.
    instance.version = 2
    await db.flush()
    event = await invoke(db, instance, send(), adapter=provider, key="orphan", version=2)
    assert event.event_type == "failure_detected"
    assert event.payload["metadata"]["orphan_dispatch_id"]
    again = await invoke(db, instance, send(), adapter=provider, key="again", version=2)
    assert again.result == "blocked"
    assert again.payload["metadata"]["gate"] == "dispatch_review"
    assert "orphan_dispatch_id" not in again.payload["metadata"]
    workflow = deepcopy(WORKFLOW)
    workflow["transitions"].append({"from": "scored", "to": "research", "action": "score_against_icp", "risk_level": "GREEN"})
    progressed = await invoke(db, instance, native(state="research"), adapter=provider,
                              key="other-action", version=2, workflow=workflow)
    assert progressed.result == "executed_in_sandbox" and instance.current_state == "research"
    assert provider.world.review_blocked_recipients == {"p-1"}
    assert len(provider.world.outcomes) == 1


@pytest.mark.parametrize("recovery_first", [True, False])
async def test_each_queue_consumer_completes_its_own_transition(async_db_session, recovery_first):
    db = async_db_session
    run, origin = await setup(db, initial_state="scored")
    recovery = await create_workflow_instance(db, sandbox_run_id=run.id,
        workflow_id="integration_failure_recovery", entity_type="integration_operation", entity_id="queue-1", initial_state="scored")
    origin_id = origin.id
    await db.commit()
    provider = adapter()
    provider.world.receive("queued_message", "queued", {
        "entity_id": "queue-1", "workflow_id": "integration_failure_recovery", "item_id": "message-1", "recipient_id": "p-1",
        "original_action": "send_outreach", "original_adapter_key": "pending",
        "origin_workflow_id": "prospect_to_meeting", "origin_entity_id": "p-1",
    })
    snapshot = {"evidence_refs": ["sandbox://queue/1"], "primary_classification": "acquisition"}
    provider.world.inject_failure("mail", action_key(origin), after_effect=True)
    await invoke(db, origin, send(), adapter=provider, synthetic_input=snapshot)
    await db.commit()
    recovery_workflow = deepcopy(WORKFLOW)
    recovery_workflow.update(id="integration_failure_recovery", terminal_states=["completed"])
    recovery_workflow["states"].append("completed")
    recovery_workflow["transitions"][1].update(to="completed", action="score_against_icp")
    recovery_decision = send()
    recovery_decision.update(action="score_against_icp", state_after="completed")
    recovery_decision["policy_intent"]["action_type"] = "score_against_icp"
    schema = deepcopy(EVENT_SCHEMA)
    for field in ("workflow_id", "entity_id", "entity_type"):
        schema["properties"][field] = {"type": "string"}
    async def recover():
        return await invoke(db, recovery, recovery_decision, adapter=provider, key="recovery",
                            workflow=recovery_workflow, schema=schema, synthetic_input=snapshot)
    async def reconcile_origin():
        return await invoke(db, origin, AssertionError("no origin model call"), adapter=provider,
                            key="origin-retry", synthetic_input=snapshot)
    if recovery_first:
        recovered, reconciled = await recover(), await reconcile_origin()
    else:
        reconciled, recovered = await reconcile_origin(), await recover()
    assert recovered.event_type == "terminal_outcome"
    assert recovered.native_decision == recovery_decision
    assert reconciled.native_decision == send()
    assert recovered.payload["metadata"]["dispatch_id"] == reconciled.payload["metadata"]["dispatch_id"]
    assert reconciled.workflow_instance_id == origin_id
    assert len(provider.world.outcomes) == 1
    assert provider.world.queue["queue-1"]["status"] == "sent"
    assert await db.scalar(select(func.count()).select_from(CompanyOSSandboxEvent).where(
        CompanyOSSandboxEvent.result == "executed_in_sandbox")) == 2


@pytest_asyncio.fixture
async def postgres_sessions(postgres_url):
    """Isolated schema using the integration suite's Docker PostgreSQL fixture."""
    url = postgres_url
    schema = "stage2_" + uuid4().hex
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    from sqlalchemy import text
    tables = [CompanyOSSandboxRun.__table__, CompanyOSWorkflowInstance.__table__,
              CompanyOSSandboxEvent.__table__, CompanyOSSandboxApproval.__table__]
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            for table in tables:
                await connection.run_sync(lambda sync, table=table: table.create(sync))
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def test_concurrent_divergent_deliveries_serialize(postgres_sessions):
    sessions = postgres_sessions
    async with sessions() as db:
        _, instance = await setup(db, initial_state="scored")
        identity = instance.id
        await db.commit()
    provider = adapter()
    ready = asyncio.Event()
    arrivals = 0
    async def deliver(key, name):
        nonlocal arrivals
        async with sessions() as db:
            instance = await db.get(CompanyOSWorkflowInstance, identity)
            arrivals += 1
            if arrivals == 2:
                ready.set()
            await ready.wait()
            decision = send()
            decision["integration"]["message"]["fills"]["name"] = name
            try:
                result = await invoke(db, instance, decision, adapter=provider, key=key)
                await db.commit()
                return result.result
            except coordinator.SandboxCoordinatorError:
                await db.rollback()
                return "stale"
    results = await asyncio.wait_for(asyncio.gather(deliver("one", "One"), deliver("two", "Two")), 15)
    assert sorted(results) == ["executed_in_sandbox", "stale"]
    assert len(provider.world.outcomes) == 1
    async with sessions() as db:
        assert await event_count(db) == 1


async def test_later_legitimate_action_gets_new_key(async_db_session):
    db = async_db_session
    _, instance = await setup(db, initial_state="scored")
    provider = adapter()
    first = await invoke(db, instance, send(), adapter=provider)
    workflow = deepcopy(WORKFLOW)
    workflow["states"].append("sent_again")
    workflow["transitions"].append({
        "from": "sent", "to": "sent_again", "action": "send_outreach",
        "risk_level": "YELLOW", "integration": "sandbox_mail",
    })
    decision = send()
    decision["state_after"] = "sent_again"
    second = await invoke(db, instance, decision, adapter=provider, key="next", version=1, workflow=workflow)
    first_key = first.payload["metadata"]["adapter_idempotency_key"]
    second_key = second.payload["metadata"]["adapter_idempotency_key"]
    assert first_key.endswith(":p-1:0") and second_key.endswith(":p-1:1")
    assert first_key != second_key
    assert len(provider.world.outcomes) == 2


async def test_adapter_template_revocation_defense_is_red_compliance_block(async_db_session):
    class Revoking(SyntheticAdapter):
        async def execute(self, *args, **kwargs):
            self.world.templates["approved"]["revoked_at"] = AT
            return await super().execute(*args, **kwargs)
    _, instance = await setup(async_db_session, initial_state="scored")
    provider = Revoking(eligible_world(), "mail")
    event = await invoke(async_db_session, instance, send(), adapter=provider)
    assert (event.risk_level, event.result, event.event_type) == ("RED", "blocked", "transition_attempted")
    assert "EMAIL-01" in event.payload["compliance"]["failed_rule_ids"]
    assert not provider.world.outcomes
    assert instance.version == 0


async def test_missing_method_block_allows_fresh_valid_dispatch(async_db_session):
    from tests.unit.test_company_os_sandbox_coordinator import REGISTRY
    _, instance = await setup(async_db_session, initial_state="scored")
    registry = deepcopy(REGISTRY)
    del registry["integrations"]["sandbox_mail"]["sandbox_sender"]["dispatch_method"]
    provider = adapter()
    event = await invoke(async_db_session, instance, send(), adapter=provider, registry=registry)
    assert "BASE-01" in event.payload["compliance"]["failed_rule_ids"]
    retry = await invoke(async_db_session, instance, send(),
                         adapter=provider, key="retry")
    assert retry.result == "executed_in_sandbox"
    assert len(provider.world.outcomes) == 1 and instance.version == 1
    assert sorted(r["status"] for r in provider.world.dispatches.values()) == ["blocked", "executed"]


async def test_revoked_template_block_can_be_replaced_by_new_valid_decision(async_db_session):
    _, instance = await setup(async_db_session, initial_state="scored")
    provider = adapter()
    template = deepcopy(provider.world.templates["approved"])
    provider.world.templates["approved"]["revoked_at"] = AT
    blocked = await invoke(async_db_session, instance, send(), adapter=provider)
    assert blocked.result == "blocked" and not provider.world.outcomes
    template["template_id"] = "replacement"
    provider.world.templates["replacement"] = template
    decision = send()
    decision["integration"]["message"]["template_id"] = "replacement"
    completed = await invoke(async_db_session, instance, decision, adapter=provider, key="new-template")
    assert completed.result == "executed_in_sandbox"
    assert completed.native_decision == decision
    assert len(provider.world.outcomes) == 1
    assert blocked.payload["metadata"]["dispatch_id"] != completed.payload["metadata"]["dispatch_id"]


async def test_approval_compliance_block_does_not_collide_or_become_orphan(async_db_session):
    instance, _, approval, _ = await pending_approval(async_db_session)
    await resolve(async_db_session, approval)
    provider = adapter()
    provider.world.templates["approved"]["revoked_at"] = AT
    blocked = await resume(async_db_session, approval, provider)
    assert blocked.result == "blocked"
    provider.world.templates["approved"]["revoked_at"] = None
    rejected = await invoke(async_db_session, instance, send(), adapter=provider, key="direct")
    assert (rejected.payload["result"], rejected.payload["metadata"]["gate"]) == ("blocked", "approval")
    completed = await resume(async_db_session, approval, provider, retry=True)
    assert completed.result == "executed_in_sandbox"
    assert blocked.payload["metadata"]["dispatch_id"] != completed.payload["metadata"]["dispatch_id"]
    workflow = deepcopy(WORKFLOW)
    workflow["states"].append("finished")
    workflow["transitions"].append({"from": "sent", "to": "finished", "action": "score_against_icp", "risk_level": "GREEN"})
    event = await invoke(async_db_session, instance, native(state="finished"), adapter=provider,
                         key="later-transition", version=1, workflow=workflow)
    assert event.result == "executed_in_sandbox"
    assert instance.current_state == "finished"
    assert not provider.world.review_blocked_recipients
    assert len(provider.world.outcomes) == 1


@pytest.mark.parametrize("approved", [False, True])
async def test_non_email_execute_fails_closed_without_dispatch_ledger(async_db_session, approved):
    from tests.unit.test_company_os_sandbox_coordinator import REGISTRY
    registry = deepcopy(REGISTRY)
    registry["integrations"]["sandbox_mail"]["desired_use"] = ["customer_authority"]
    provider = SyntheticAdapter(eligible_world(), "authority")
    provider.world.customers["p-1"] = {"authority_verified": True}
    output = send()
    output["integration"] = {"name": "sandbox_mail", "phase": "execute", "use": "customer_authority"}
    _, instance = await setup(async_db_session, initial_state="scored")
    if approved:
        output["policy_intent"].update(limit_name="max_discount_percent", requested_total=20)
    event = await invoke(async_db_session, instance, output, adapter=provider, registry=registry)
    if approved:
        approval = await async_db_session.scalar(select(CompanyOSSandboxApproval).where(
            CompanyOSSandboxApproval.request_event_id == event.id))
        await resolve(async_db_session, approval)
        event = await resume(async_db_session, approval, provider, registry=registry)
    assert event.result == "blocked"
    assert event.payload["metadata"]["gate"] == "unsupported_execute"
    assert "non-email execute blocked" in event.payload["error"]
    assert not provider.world.outcomes and not provider.world.dispatches
    assert instance.version == 0


@pytest.mark.parametrize("workflow_id,entity_type", [
    ("integration_failure_recovery", "integration_operation"),
    ("estimate_followup", "estimate"), ("stale_lead_reactivation", "lead_batch"),
])
async def test_aggregate_queue_recipient_opt_out_has_specific_reason(async_db_session, workflow_id, entity_type):
    _, instance = await setup(async_db_session, initial_state="scored")
    instance.workflow_id, instance.entity_type, instance.entity_id = workflow_id, entity_type, "aggregate-1"
    await async_db_session.flush()
    workflow = deepcopy(WORKFLOW)
    workflow["id"] = workflow_id
    schema = deepcopy(EVENT_SCHEMA)
    for field in ("workflow_id", "entity_id", "entity_type"):
        schema["properties"][field] = {"type": "string"}
    provider = adapter()
    provider.world.receive("queued_message", "queued", {
        "entity_id": "aggregate-1", "workflow_id": workflow_id, "item_id": "message-1", "recipient_id": "p-1",
        "original_action": "send_outreach", "original_adapter_key": "pending",
    })
    provider.world.ingest_opt_out({"entity_id": "p-1"})
    event = await invoke(async_db_session, instance, send(), adapter=provider, schema=schema,
                         workflow=workflow, synthetic_input={"item_id": "message-1",
                         "primary_classification": "acquisition", "evidence_refs": ["sandbox://queued"]})
    assert event.result == "blocked" and event.risk_level == "RED"
    assert "email_optout_or_suppression_match" in event.payload["compliance"]["reason_codes"]
    assert event.payload["metadata"]["recipient_id"] == "p-1"
    assert provider.world.queue["aggregate-1"]["status"] == "blocked"
    provider.world.pending_opt_outs.clear()
    provider.world.suppressed_recipients.clear()
    provider.world.contacts["p-1"]["suppressed"] = False
    retry = await invoke(async_db_session, instance, send(), adapter=provider, key="queue-retry",
                         schema=schema, workflow=workflow, synthetic_input={"item_id": "message-1",
                         "primary_classification": "acquisition", "evidence_refs": ["sandbox://queued"]})
    assert retry.result == "blocked" and not provider.world.outcomes


@pytest.mark.parametrize("field,value", [("recipient_id", "other"), ("contact_email", "other@example.test"), ("method", "manual")])
async def test_mismatched_consent_blocks_coordinator(async_db_session, field, value):
    _, instance = await setup(async_db_session, initial_state="scored")
    provider = adapter()
    provider.world.consents["p-1"][field] = value
    event = await invoke(async_db_session, instance, send(), adapter=provider)
    assert event.result == "blocked" and event.risk_level == "RED"
    assert event.payload["compliance"]["failed_rule_ids"] == ["BASE-01"]
    assert instance.version == 0 and not provider.world.outcomes


async def test_email_registry_cannot_bypass_gate_with_non_email_requested_use(async_db_session):
    from tests.unit.test_company_os_sandbox_coordinator import REGISTRY
    _, instance = await setup(async_db_session, initial_state="scored")
    provider = adapter()
    registry = deepcopy(REGISTRY)
    registry["integrations"]["sandbox_mail"]["desired_use"].append("sms")
    output = send()
    output["integration"]["use"] = "sms"
    event = await invoke(async_db_session, instance, output, adapter=provider, registry=registry)
    assert event.result == "blocked" and event.risk_level == "RED"
    assert "BASE-01" in event.payload["compliance"]["failed_rule_ids"]
    assert not provider.world.outcomes


@pytest.mark.parametrize("approval_first", [True, False])
async def test_cross_path_timeout_never_sends_twice(async_db_session, approval_first):
    db = async_db_session
    instance, _, approval, _ = await pending_approval(db)
    provider = adapter()
    if approval_first:
        await resolve(db, approval)
        provider.world.inject_failure("mail", f"stage2-approval-resume:{approval.id}", after_effect=True)
        failure = await resume(db, approval, provider)
        assert failure.event_type == "failure_detected"
        await db.commit()
        rejected = await invoke(db, instance, send(), adapter=provider, key="direct-retry")
        assert (rejected.payload["result"], rejected.payload["metadata"]["gate"]) == ("blocked", "approval")
        completed = await resume(db, approval, provider, retry=True)
    else:
        provider.world.inject_failure("mail", action_key(instance), after_effect=True)
        failure = await invoke(db, instance, send(), adapter=provider, key="direct-send")
        assert failure.event_type == "failure_detected"
        await db.commit()
        await resolve(db, approval)
        completed = await resume(db, approval, provider)
    assert completed.result == "executed_in_sandbox"
    assert completed.payload["metadata"]["reconciled"] is True
    assert completed.payload["metadata"]["dispatch_id"] == failure.payload["metadata"]["dispatch_id"]
    assert len(provider.world.outcomes) == len(provider.world.dispatches) == 1
    assert await db.scalar(select(func.count()).select_from(CompanyOSSandboxEvent).where(
        CompanyOSSandboxEvent.result == "executed_in_sandbox")) == 1


@pytest.mark.parametrize("status", ["approved", "modified"])
async def test_authorized_approval_cannot_be_bypassed_by_lower_risk_direct_decision(async_db_session, status):
    instance, original, approval, _ = await pending_approval(async_db_session)
    corrected = deepcopy(original)
    corrected["policy_intent"]["requested_total"] = 5
    await resolve(async_db_session, approval, status,
                  **({"corrected_decision": corrected} if status == "modified" else {}))
    provider = adapter()
    rejected = await invoke(async_db_session, instance, send(), adapter=provider, key="bypass")
    assert (rejected.payload["result"], rejected.payload["metadata"]["gate"]) == ("blocked", "approval")
    assert instance.version == 0 and not provider.world.outcomes


@pytest.mark.parametrize("ordering", ["origin_first", "late_queue", "recovery_first"])
async def test_trusted_queue_identity_without_caller_item_id(async_db_session, ordering):
    db = async_db_session
    run, origin = await setup(db, initial_state="scored")
    recovery = await create_workflow_instance(db, sandbox_run_id=run.id,
        workflow_id="integration_failure_recovery", entity_type="integration_operation", entity_id="queue-1", initial_state="scored")
    provider = adapter()
    facts = {"entity_id": "queue-1", "workflow_id": "integration_failure_recovery",
             "item_id": "message-1", "recipient_id": "p-1", "original_action": "send_outreach",
             "origin_workflow_id": origin.workflow_id, "origin_entity_id": origin.entity_id}
    workflow = deepcopy(WORKFLOW)
    workflow.update(id="integration_failure_recovery", terminal_states=["sent"])
    schema = deepcopy(EVENT_SCHEMA)
    for field in ("workflow_id", "entity_id", "entity_type"):
        schema["properties"][field] = {"type": "string"}
    if ordering != "late_queue":
        provider.world.receive("queued_message", "queued", facts)
    if ordering == "recovery_first":
        recovered = await invoke(db, recovery, send(), adapter=provider, key="recovery", workflow=workflow, schema=schema)
        sent = await invoke(db, origin, send(), adapter=provider, key="origin")
    else:
        sent = await invoke(db, origin, send(), adapter=provider, key="origin")
        if ordering == "late_queue":
            provider.world.receive("queued_message", "queued", facts)
            assert provider.world.queue["queue-1"]["dispatch_id"] == sent.payload["metadata"]["dispatch_id"]
            assert provider.world.queue["queue-1"]["status"] == "sent"
        recovered = await invoke(db, recovery, send(), adapter=provider, key="recovery", workflow=workflow, schema=schema)
    assert sent.result == recovered.result == "executed_in_sandbox"
    assert sent.payload["metadata"]["dispatch_id"] == recovered.payload["metadata"]["dispatch_id"]
    assert len(provider.world.outcomes) == len(provider.world.dispatches) == 1
    assert provider.world.queue["queue-1"]["receipt"] == next(iter(provider.world.outcomes))


async def test_contradictory_item_id_cannot_select_another_message(async_db_session):
    _, instance = await setup(async_db_session, initial_state="scored")
    provider = adapter()
    provider.world.receive("queued_message", "queued", {
        "entity_id": "queue-1", "workflow_id": "integration_failure_recovery",
        "item_id": "trusted-message", "recipient_id": "p-1", "original_action": "send_outreach",
        "origin_workflow_id": instance.workflow_id, "origin_entity_id": instance.entity_id,
    })
    event = await invoke(async_db_session, instance, send(), adapter=provider,
                         synthetic_input={"item_id": "contradictory-message", "primary_classification": "acquisition",
                                          "evidence_refs": ["sandbox://input"]})
    assert event.result == "blocked"
    assert "BASE-01" in event.payload["compliance"]["failed_rule_ids"]
    assert not provider.world.outcomes and instance.version == 0


async def test_contradictory_item_id_cannot_reconcile_existing_effect(async_db_session):
    _, instance = await setup(async_db_session, initial_state="scored")
    provider = adapter()
    provider.world.receive("queued_message", "queued", {
        "entity_id": "queue-1", "workflow_id": "integration_failure_recovery",
        "item_id": "trusted-message", "recipient_id": "p-1", "original_action": "send_outreach",
        "origin_workflow_id": instance.workflow_id, "origin_entity_id": instance.entity_id,
    })
    provider.world.inject_failure("mail", action_key(instance), after_effect=True)
    await invoke(async_db_session, instance, send(), adapter=provider)
    blocked = await invoke(async_db_session, instance, send(), adapter=provider, key="contradiction",
                           synthetic_input={"item_id": "other", "primary_classification": "acquisition",
                                            "evidence_refs": ["sandbox://input"]})
    assert blocked.result == "blocked" and blocked.risk_level == "RED"
    assert "BASE-01" in blocked.payload["compliance"]["failed_rule_ids"]
    assert instance.version == 0 and len(provider.world.outcomes) == 1


@pytest.mark.parametrize("approved", [False, True])
async def test_email_integration_read_never_reaches_a_willing_adapter(async_db_session, approved):
    _, instance = await setup(async_db_session)
    provider = adapter()
    output = native(action="pricing_change" if approved else "score_against_icp", state="research",
                    risk="RED" if approved else "GREEN", external_write=False,
                    integration={"name": "sandbox_mail", "phase": "read", "use": "acquisition_email"})
    event = await invoke(async_db_session, instance, output, adapter=provider)
    if approved:
        approval = await async_db_session.scalar(select(CompanyOSSandboxApproval).where(
            CompanyOSSandboxApproval.request_event_id == event.id))
        await resolve(async_db_session, approval)
        event = await resume(async_db_session, approval, provider)
    assert event.result in {"failed", "blocked"} and event.event_type == "failure_detected"
    assert "governed execute" in event.payload["error"]
    assert instance.version == 0 and not provider.world.outcomes


@pytest.mark.parametrize("workflow_id,entity_type,queue_workflow", [
    ("unknown", "prospect", "unknown"),
    ("prospect_to_meeting", "customer_account", "prospect_to_meeting"),
    ("integration_failure_recovery", "integration_operation", "estimate_followup"),
])
async def test_unknown_or_wrongly_bound_recipient_scope_is_base_block(async_db_session, workflow_id, entity_type, queue_workflow):
    _, instance = await setup(async_db_session, initial_state="scored")
    instance.workflow_id, instance.entity_type = workflow_id, entity_type
    await async_db_session.flush()
    provider = adapter()
    provider.world.receive("queued_message", "queued-scope", {
        "entity_id": instance.entity_id, "workflow_id": queue_workflow, "item_id": "scope-item",
        "recipient_id": "p-1", "original_action": "send_outreach",
        "origin_workflow_id": workflow_id, "origin_entity_id": instance.entity_id,
    })
    workflow, schema = deepcopy(WORKFLOW), deepcopy(EVENT_SCHEMA)
    workflow["id"] = workflow_id
    for field in ("workflow_id", "entity_type"):
        schema["properties"][field] = {"type": "string"}
    event = await invoke(async_db_session, instance, send(), adapter=provider, workflow=workflow, schema=schema)
    assert event.result == "blocked" and event.risk_level == "RED"
    assert "BASE-01" in event.payload["compliance"]["failed_rule_ids"]
    assert not provider.world.outcomes and instance.version == 0


@pytest.mark.parametrize("path", ["direct", "resume"])
@pytest.mark.parametrize("reason", ["multiple unfinished dispatches require review",
                                    "dispatch adapter is no longer registered"])
async def test_review_required_dispatch_conditions_are_recorded_not_raised(
        async_db_session, monkeypatch, path, reason):
    import app.services.company_os_sandbox_approval_service as approvals
    from app.services.company_os_synthetic_adapters import SyntheticCapabilityError

    async def review_needed(*args, **kwargs):
        raise SyntheticCapabilityError(reason)

    provider = adapter()
    if path == "direct":
        _, instance = await setup(async_db_session, initial_state="scored")
        monkeypatch.setattr(coordinator, "find_dispatch", review_needed)
        event = await invoke(async_db_session, instance, AssertionError("model must not run"), adapter=provider)
    else:
        instance, _, approval, _ = await pending_approval(async_db_session)
        await resolve(async_db_session, approval)
        monkeypatch.setattr(approvals, "find_dispatch", review_needed)
        event = await resume(async_db_session, approval, provider)
        assert approval.last_failure_event_id == event.id
        assert event.payload["approval"] == {"decision_id": approval.decision_id, "status": "approved"}
        assert event.payload["metadata"]["approval_id"] == approval.id
        assert event.payload["metadata"]["attempt"] == 1
    assert (event.event_type, event.result) == ("failure_detected", "blocked")
    assert event.payload["metadata"]["gate"] == "dispatch_review"
    assert reason in event.payload["error"]
    assert instance.version == 0 and not provider.world.outcomes


@pytest.mark.parametrize("status,awaiting", [("approved", True), ("modified", True), ("rejected", False)])
async def test_dispatch_bound_to_approval_waits_for_resume_even_if_action_changed(async_db_session, status, awaiting):
    instance, original, approval, _ = await pending_approval(async_db_session)
    fix = deepcopy(original)
    fix["policy_intent"]["requested_total"] = 5
    kwargs = {"corrected_decision": fix} if status == "modified" else {}
    await resolve(async_db_session, approval, status, **kwargs)
    corrected = deepcopy(original)
    corrected["action"] = "some_corrected_action"
    record = {"consumers": {instance.id: {"approval_id": approval.id, "consumer_decision": corrected}}}
    assert await coordinator._awaiting_resume(async_db_session, instance, record) is awaiting
