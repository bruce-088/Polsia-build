"""S2-P1.3 smoke-test remediation regression guards.

Covers Decision A (drafted_content's unforgeable, event_id-keyed self-referencing
evidence), Decision B (the action-vs-action_type hard-deny/RED bypass fix), the
shared draft-template validator (render_message reuse), and the dispatch-ledger
conflict/reconciliation fixes found across the 27-round plan review.
"""

from copy import deepcopy

import pytest
from sqlalchemy import select

from app.models.company_os_sandbox import CompanyOSSandboxApproval
from app.services.company_os_sandbox_approval_service import resolve_sandbox_approval
from app.services.company_os_sandbox_dispatch import find_dispatch, register_consumer
from app.services.company_os_synthetic_adapters import SyntheticAdapter, compute_ref
from tests.unit.company_os_stage2_facts import MESSAGE, eligible_world
from tests.unit.test_company_os_sandbox_coordinator import (
    EVENT_SCHEMA,
    FOLLOWUP_WORKFLOW,
    WORKFLOW,
    Adapter,
    invoke,
    native,
    pending_approval,
    resolve,
    resume,
    setup,
)

RUN_ID = "s2-test"
DRAFT = deepcopy(MESSAGE)  # {"template_id": "approved", "fills": {"name": "Pat"}}


def own_ref(event):
    return compute_ref(RUN_ID, "input", f"draft:{event.event_id}")


# --- Decision A: drafted_content evidence -----------------------------------

@pytest.mark.asyncio
async def test_drafted_content_mints_self_referencing_evidence(async_db_session):
    _, instance = await setup(async_db_session)
    output = {**native(), "drafted_content": deepcopy(DRAFT)}
    event = await invoke(async_db_session, instance, output, adapter=Adapter())
    assert event.payload["result"] == "executed_in_sandbox"
    assert own_ref(event) in event.payload["evidence_refs"]
    assert event.payload["metadata"]["draft_content"] == DRAFT


@pytest.mark.asyncio
async def test_null_drafted_content_accepted_same_as_absent(async_db_session):
    _, instance = await setup(async_db_session)
    event = await invoke(async_db_session, instance, {**native(), "drafted_content": None})
    assert event.payload["result"] == "executed_in_sandbox"
    assert "draft_content" not in event.payload["metadata"]


@pytest.mark.asyncio
async def test_denied_decision_never_mints_a_draft_ref(async_db_session):
    _, instance = await setup(async_db_session)
    output = {**native(risk="YELLOW"), "drafted_content": deepcopy(DRAFT)}
    event = await invoke(async_db_session, instance, output, adapter=Adapter())
    assert event.payload["metadata"]["gate"] == "transition"
    assert event.payload["result"] == "blocked"
    assert event.payload["evidence_refs"] == ["sandbox://input/p-1"]
    assert "draft_content" not in event.payload["metadata"]


@pytest.mark.asyncio
async def test_approval_request_merges_prior_draft_ref_from_same_instance(async_db_session):
    db = async_db_session
    _, instance = await setup(db)
    draft_event = await invoke(db, instance, {**native(), "drafted_content": deepcopy(DRAFT)}, key="draft-1", adapter=Adapter())
    assert own_ref(draft_event) in draft_event.payload["evidence_refs"]
    await db.commit()
    escalate = native(action="pricing_change", state="scored", risk="RED")
    approval_event = await invoke(db, instance, escalate, key="escalate-1", version=1)
    assert approval_event.payload["event_type"] == "approval_requested"
    assert own_ref(draft_event) in approval_event.payload["evidence_refs"]


@pytest.mark.asyncio
async def test_resolution_and_resume_copy_never_mint_their_own_duplicate_ref(async_db_session):
    db = async_db_session
    _, instance = await setup(db, initial_state="scored")
    adapter = Adapter()
    # Pure policy-limit escalation (no integration at all) -- drafted_content
    # cannot coexist with an execute-phase integration (S2P13-R27-02).
    output = {**native(
        action="score_against_icp", state="scored", risk="YELLOW",
        limit_name="max_discount_percent", requested_total=20,
    ), "drafted_content": deepcopy(DRAFT)}
    requested = await invoke(db, instance, output, adapter=adapter)
    original_ref = own_ref(requested)
    assert original_ref in requested.payload["evidence_refs"]
    approval = await db.scalar(select(CompanyOSSandboxApproval).where(
        CompanyOSSandboxApproval.request_event_id == requested.id))
    resolution = await resolve(db, approval)
    assert resolution.payload["evidence_refs"] == requested.payload["evidence_refs"]
    assert own_ref(resolution) not in resolution.payload["evidence_refs"]
    resumed = await resume(db, approval, adapter)
    assert original_ref in resumed.payload["evidence_refs"]
    assert own_ref(resumed) not in resumed.payload["evidence_refs"]


@pytest.mark.asyncio
async def test_founder_correction_mints_a_fresh_ref_distinct_from_the_original(async_db_session):
    db = async_db_session
    _, instance = await setup(db, initial_state="scored")
    adapter = Adapter()
    output = {**native(
        action="score_against_icp", state="scored", risk="YELLOW",
        limit_name="max_discount_percent", requested_total=20,
    ), "drafted_content": deepcopy(DRAFT)}
    requested = await invoke(db, instance, output, adapter=adapter)
    original_ref = own_ref(requested)
    approval = await db.scalar(select(CompanyOSSandboxApproval).where(
        CompanyOSSandboxApproval.request_event_id == requested.id))
    corrected = deepcopy(output)
    corrected["drafted_content"] = {"template_id": "approved", "fills": {"name": "Alex"}}
    resolution = await resolve(db, approval, "modified", corrected_decision=corrected)
    assert own_ref(resolution) not in resolution.payload["evidence_refs"]
    resumed = await resume(db, approval, adapter)
    fresh_ref = own_ref(resumed)
    assert fresh_ref in resumed.payload["evidence_refs"]
    assert fresh_ref != original_ref
    assert resumed.payload["metadata"]["draft_content"] == corrected["drafted_content"]


@pytest.mark.asyncio
async def test_resume_rejects_corrected_draft_with_unknown_template(async_db_session):
    db = async_db_session
    _, instance = await setup(db, initial_state="scored")
    adapter = Adapter()
    output = {**native(
        action="score_against_icp", state="scored", risk="YELLOW",
        limit_name="max_discount_percent", requested_total=20,
    ), "drafted_content": deepcopy(DRAFT)}
    requested = await invoke(db, instance, output, adapter=adapter)
    approval = await db.scalar(select(CompanyOSSandboxApproval).where(
        CompanyOSSandboxApproval.request_event_id == requested.id))
    corrected = deepcopy(output)
    corrected["drafted_content"] = {"template_id": "unknown", "fills": {}}
    await resolve(db, approval, "modified", corrected_decision=corrected)
    failure = await resume(db, approval, adapter)
    assert failure.payload["event_type"] == "failure_detected"
    assert "drafted_content" in failure.payload["error"]
    assert own_ref(failure) not in failure.payload.get("evidence_refs", [])


@pytest.mark.asyncio
async def test_drafted_content_cannot_coexist_with_an_execute_phase_integration(async_db_session):
    """S2P13-R27-02: a decision must never claim drafted_content as evidence for
    content while a DIFFERENT execute-phase integration.message is what actually
    gets sent -- the two could silently diverge (e.g. a founder correcting one
    but not the other). drafted_content is evidence for a later send, never the
    send itself."""
    _, instance = await setup(async_db_session, initial_state="scored")
    output = native(
        action="send_outreach", state="sent", risk="YELLOW",
        integration={"name": "sandbox_mail", "phase": "execute", "use": "acquisition_email"},
        limit_name="max_discount_percent", requested_total=20,
    )
    output["drafted_content"] = deepcopy(DRAFT)
    event = await invoke(async_db_session, instance, output, adapter=Adapter())
    assert event.payload["metadata"]["gate"] == "contract"
    assert event.payload["result"] == "failed"
    assert "drafted_content" in event.payload["error"]


# --- Decision B: literal action checked alongside action_type ---------------

@pytest.mark.asyncio
async def test_coordinator_blocks_hard_deny_despite_red_action_type_mislabel(async_db_session):
    _, instance = await setup(async_db_session)
    output = native(action="bypass_opt_out", state="research", risk="RED",
                     action_type="pricing_change", founder_approval_status="approved")
    event = await invoke(async_db_session, instance, output)
    assert event.payload["result"] == "blocked"
    assert event.payload["metadata"]["gate"] == "policy"


@pytest.mark.asyncio
async def test_resume_blocks_hard_deny_despite_red_action_type_mislabel(async_db_session):
    instance, original, approval, adapter = await pending_approval(async_db_session)
    corrected = native(action="bypass_opt_out", state="scored", risk="RED", action_type="pricing_change")
    await resolve(async_db_session, approval, "modified", corrected_decision=corrected)
    failure = await resume(async_db_session, approval, adapter)
    assert failure.payload["event_type"] == "failure_detected"
    assert "cannot override policy block" in failure.payload["error"]
    assert adapter.calls == 0


# --- Draft-template validation (render_message reuse) -----------------------

@pytest.mark.asyncio
async def test_unknown_draft_template_is_a_contract_denial(async_db_session):
    adapter = Adapter()
    _, instance = await setup(async_db_session)
    output = {**native(), "drafted_content": {"template_id": "unknown", "fills": {}}}
    event = await invoke(async_db_session, instance, output, adapter=adapter)
    assert event.payload["metadata"]["gate"] == "contract"
    assert event.payload["result"] == "failed"
    assert "drafted_content" in event.payload["error"]


@pytest.mark.asyncio
async def test_revoked_draft_template_is_a_contract_denial(async_db_session):
    adapter = Adapter()
    adapter.world.templates["approved"]["revoked_at"] = "2026-09-26T00:00:00+00:00"
    _, instance = await setup(async_db_session)
    event = await invoke(async_db_session, instance, {**native(), "drafted_content": deepcopy(DRAFT)}, adapter=adapter)
    assert event.payload["metadata"]["gate"] == "contract"
    assert event.payload["result"] == "failed"


@pytest.mark.asyncio
async def test_draft_fills_not_matching_template_placeholders_is_a_contract_denial(async_db_session):
    adapter = Adapter()
    _, instance = await setup(async_db_session)
    output = {**native(), "drafted_content": {"template_id": "approved", "fills": {"wrong_key": "x"}}}
    event = await invoke(async_db_session, instance, output, adapter=adapter)
    assert event.payload["metadata"]["gate"] == "contract"
    assert event.payload["result"] == "failed"


@pytest.mark.asyncio
async def test_draft_validation_fails_closed_without_a_registered_world(async_db_session):
    _, instance = await setup(async_db_session)
    output = {**native(), "drafted_content": deepcopy(DRAFT)}
    event = await invoke(async_db_session, instance, output)  # no adapter -> no world
    assert event.payload["metadata"]["gate"] == "contract"
    assert event.payload["result"] == "failed"
    assert "trusted template registry" in event.payload["error"]


# --- Dispatch-ledger conflict/reconciliation fixes ---------------------------

@pytest.mark.asyncio
async def test_different_approvals_executed_dispatch_blocks_further_sends_to_recipient(async_db_session):
    """V20-01/V22-01: once one approval's send executes for a recipient, a second,
    distinct approval's attempt to send to the same recipient is blocked for review
    rather than silently allowed to risk a duplicate send. Uses the real
    SyntheticAdapter (not the stub Adapter used elsewhere in this file) so
    inject_failure's after-effect/ledger mechanics actually run."""
    db = async_db_session
    _, instance = await setup(db, initial_state="scored")
    adapter = SyntheticAdapter(eligible_world(), "mail")
    outreach = native(
        action="send_outreach", state="sent", risk="YELLOW",
        integration={"name": "sandbox_mail", "phase": "execute", "use": "acquisition_email"},
        limit_name="max_discount_percent", requested_total=20,
    )
    requested_a = await invoke(db, instance, outreach, adapter=adapter, key="delivery-a",
                                synthetic_input={"evidence_refs": ["sandbox://input/p-1"],
                                                  "primary_classification": "acquisition",
                                                  "approval_decision_id": "APR-1"})
    approval_a = await db.scalar(select(CompanyOSSandboxApproval).where(
        CompanyOSSandboxApproval.request_event_id == requested_a.id))
    followup = native(
        action="send_followup", state="sent", risk="YELLOW",
        integration={"name": "sandbox_mail", "phase": "execute", "use": "acquisition_email"},
        limit_name="max_discount_percent", requested_total=20,
    )
    requested_b = await invoke(db, instance, followup, adapter=adapter, key="delivery-b", workflow=FOLLOWUP_WORKFLOW,
                                synthetic_input={"evidence_refs": ["sandbox://input/p-1"],
                                                  "primary_classification": "acquisition",
                                                  "approval_decision_id": "APR-2"})
    approval_b = await db.scalar(select(CompanyOSSandboxApproval).where(
        CompanyOSSandboxApproval.request_event_id == requested_b.id))
    await resolve_sandbox_approval(
        db, approval_id=approval_a.id, status="approved", founder_id="founder-1",
        founder_minutes=2.5, resolution_key="resolve-1", event_schema=EVENT_SCHEMA, workflow=WORKFLOW,
    )
    await resolve_sandbox_approval(
        db, approval_id=approval_b.id, status="approved", founder_id="founder-1",
        founder_minutes=2.5, resolution_key="resolve-2", event_schema=EVENT_SCHEMA, workflow=WORKFLOW,
    )
    adapter.world.inject_failure("mail", f"stage2-approval-resume:{approval_a.id}", after_effect=True)
    failure_a = await resume(db, approval_a, adapter)
    assert failure_a.payload["event_type"] == "failure_detected"
    await db.commit()
    blocked_b = await resume(db, approval_b, adapter, workflow=FOLLOWUP_WORKFLOW)
    assert blocked_b.payload["result"] == "blocked"
    assert blocked_b.payload["metadata"]["gate"] == "dispatch_review"


@pytest.mark.asyncio
async def test_closed_competing_approvals_prepared_dispatch_does_not_block_resume(async_db_session):
    """V23-01: a prepared dispatch tied to an approval that is no longer open must
    not permanently raise for an unrelated, still-open approval on the same
    recipient. The current resolve/resume lifecycle freezes status at resolution
    (never re-closes an approved one), so this exercises find_dispatch directly
    with a hand-registered consumer under a rejected approval id -- the same
    defensive shape the fix guards against."""
    db = async_db_session
    instance, output, approval_a, adapter = await pending_approval(db, workflow=FOLLOWUP_WORKFLOW)
    followup = native(
        action="send_followup", state="sent", risk="YELLOW",
        integration={"name": "sandbox_mail", "phase": "execute", "use": "acquisition_email"},
        limit_name="max_discount_percent", requested_total=20,
    )
    requested_b = await invoke(db, instance, followup, adapter=adapter, key="delivery-b", workflow=FOLLOWUP_WORKFLOW,
                                synthetic_input={"evidence_refs": ["sandbox://input/p-1"],
                                                  "primary_classification": "acquisition",
                                                  "approval_decision_id": "APR-2"})
    approval_b = await db.scalar(select(CompanyOSSandboxApproval).where(
        CompanyOSSandboxApproval.request_event_id == requested_b.id))
    await resolve(db, approval_b, "rejected")
    world = adapter.world
    record = world.prepare_dispatch(
        message_key="closed-competitor", recipient_id=instance.entity_id, adapter_kind="mail",
        adapter_idempotency_key="closed-competitor-key",
        origin_workflow_instance_id=instance.id, origin_state_before=instance.current_state,
        origin_workflow_version=instance.version, frozen_native_decision=followup,
        rendered_payload={}, consumers={},
    )
    register_consumer(record, instance, followup, approval_id=approval_b.id)
    pending = await find_dispatch(
        db, instance, {"sandbox_mail": adapter}, approval_id=approval_a.id,
        snapshot=approval_a.input_snapshot, action=output["action"],
    )
    assert pending is None
    assert instance.entity_id not in world.review_blocked_recipients


@pytest.mark.asyncio
async def test_modified_correction_cannot_launder_an_unrelated_direct_dispatch(async_db_session):
    """S2P13-R27-01: a founder correction that retargets the action must never be
    allowed to reconcile against an unrelated, already-executed direct dispatch
    for a DIFFERENT original action -- that dispatch was never gated by this
    approval's own policy/transition checks, and the early reconciliation branch
    bypasses them entirely."""
    db = async_db_session
    _, instance = await setup(db, initial_state="scored")
    adapter = SyntheticAdapter(eligible_world(), "mail")
    outreach = native(
        action="send_outreach", state="sent", risk="YELLOW",
        integration={"name": "sandbox_mail", "phase": "execute", "use": "acquisition_email"},
        limit_name="max_discount_percent", requested_total=20,
    )
    requested = await invoke(db, instance, outreach, adapter=adapter, key="delivery-a")
    approval = await db.scalar(select(CompanyOSSandboxApproval).where(
        CompanyOSSandboxApproval.request_event_id == requested.id))
    # An unrelated, already-executed direct send for a DIFFERENT action, same recipient.
    followup = native(
        action="send_followup", state="sent", risk="YELLOW",
        integration={"name": "sandbox_mail", "phase": "execute", "use": "acquisition_email"},
    )
    record = adapter.world.prepare_dispatch(
        message_key="direct-followup", recipient_id=instance.entity_id, adapter_kind="mail",
        adapter_idempotency_key="direct-followup-key",
        origin_workflow_instance_id=instance.id, origin_state_before=instance.current_state,
        origin_workflow_version=instance.version, frozen_native_decision=followup,
        rendered_payload={}, consumers={},
    )
    record.update(status="executed", receipt="sandbox://mail/direct-followup-receipt")
    register_consumer(record, instance, followup, approval_id=None)
    corrected = {**outreach, "action": "send_followup"}
    await resolve_sandbox_approval(
        db, approval_id=approval.id, status="modified", founder_id="founder-1",
        founder_minutes=2.5, resolution_key="resolve-1", event_schema=EVENT_SCHEMA,
        workflow=WORKFLOW, corrected_decision=corrected,
    )
    reviewed = await resume(db, approval, adapter)
    assert reviewed.payload["event_type"] == "failure_detected"
    assert "unfinished dispatch differs" in reviewed.payload["error"]


@pytest.mark.asyncio
async def test_pending_dispatch_with_different_message_content_does_not_reconcile(async_db_session):
    """S2P13-R28-01: a pending dispatch matching on action/state/integration
    identity but carrying DIFFERENT message content must not reconcile --
    otherwise the resume event could attribute content that was never
    actually sent to the founder's approval."""
    db = async_db_session
    _, instance = await setup(db, initial_state="scored")
    adapter = SyntheticAdapter(eligible_world(), "mail")
    outreach = native(
        action="send_outreach", state="sent", risk="YELLOW",
        integration={"name": "sandbox_mail", "phase": "execute", "use": "acquisition_email"},
        limit_name="max_discount_percent", requested_total=20,
    )
    requested = await invoke(db, instance, outreach, adapter=adapter, key="delivery-a")
    approval = await db.scalar(select(CompanyOSSandboxApproval).where(
        CompanyOSSandboxApproval.request_event_id == requested.id))
    # An unrelated direct send for the SAME action/state/integration identity
    # but DIFFERENT message content.
    different_content = deepcopy(outreach)
    different_content["integration"]["message"] = {"template_id": "approved", "fills": {"name": "Someone Else"}}
    record = adapter.world.prepare_dispatch(
        message_key="direct-same-action", recipient_id=instance.entity_id, adapter_kind="mail",
        adapter_idempotency_key="direct-same-action-key",
        origin_workflow_instance_id=instance.id, origin_state_before=instance.current_state,
        origin_workflow_version=instance.version, frozen_native_decision=different_content,
        rendered_payload={}, consumers={},
    )
    record.update(status="executed", receipt="sandbox://mail/different-content-receipt")
    register_consumer(record, instance, different_content, approval_id=None)
    await resolve(db, approval)
    reviewed = await resume(db, approval, adapter)
    assert reviewed.payload["event_type"] == "failure_detected"
    assert "unfinished dispatch differs" in reviewed.payload["error"]


@pytest.mark.asyncio
async def test_modified_correction_with_hard_deny_flag_is_validated_before_reconciling(async_db_session):
    """S2P13-R29-01: a founder correction that keeps action/state/message
    identical to an already-executed, unrelated direct dispatch but adds a
    hard-denied policy flag must still be rejected by policy evaluation --
    the reconciliation shortcut must never bypass validation of the
    founder's OWN effective decision in favor of reusing a ledger entry."""
    db = async_db_session
    _, instance = await setup(db, initial_state="scored")
    adapter = SyntheticAdapter(eligible_world(), "mail")
    outreach = native(
        action="send_outreach", state="sent", risk="YELLOW",
        integration={"name": "sandbox_mail", "phase": "execute", "use": "acquisition_email"},
        limit_name="max_discount_percent", requested_total=20,
    )
    requested = await invoke(db, instance, outreach, adapter=adapter, key="delivery-a")
    approval = await db.scalar(select(CompanyOSSandboxApproval).where(
        CompanyOSSandboxApproval.request_event_id == requested.id))
    # An already-executed direct dispatch, identical action/state/message.
    record = adapter.world.prepare_dispatch(
        message_key="direct-same-everything", recipient_id=instance.entity_id, adapter_kind="mail",
        adapter_idempotency_key="direct-same-everything-key",
        origin_workflow_instance_id=instance.id, origin_state_before=instance.current_state,
        origin_workflow_version=instance.version, frozen_native_decision=outreach,
        rendered_payload={}, consumers={},
    )
    record.update(status="executed", receipt="sandbox://mail/same-everything-receipt")
    register_consumer(record, instance, outreach, approval_id=None)
    corrected = deepcopy(outreach)
    corrected["policy_intent"]["policy_flags"] = ["bypass_opt_out"]
    await resolve(db, approval, "modified", corrected_decision=corrected)
    blocked = await resume(db, approval, adapter)
    assert blocked.payload["event_type"] == "failure_detected"
    assert "cannot override policy block" in blocked.payload["error"]
