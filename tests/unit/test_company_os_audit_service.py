"""Tests for immutable Company OS action records and append-only events."""

import pytest

from app.services.company_os_audit_service import (
    CompanyOSAuditError,
    append_action_event,
    create_action_record,
    list_action_events,
)


async def make_record(db, status="proposed", approval="not_required"):
    return await create_action_record(
        db,
        company_slug="acqivo",
        agent_type="revenue_ops",
        proposed_action="send_followup",
        decision_envelope={"scenario_id": "SIM-011", "action": "send_followup"},
        policy_decision={"disposition": "allow"},
        workflow_transition={"from": "waiting", "to": "followup_due"},
        integration_state={"integration": "sendgrid", "mode": "disabled"},
        approval_state=approval,
        initial_execution_status=status,
    )


@pytest.mark.asyncio
async def test_record_preserves_original_decision_evidence(async_db_session):
    record = await make_record(async_db_session)
    assert record.id is not None
    assert record.decision_envelope["action"] == "send_followup"
    assert record.initial_execution_status == "proposed"


@pytest.mark.asyncio
async def test_events_are_appended_in_order(async_db_session):
    record = await make_record(async_db_session)
    await append_action_event(
        async_db_session,
        action_record_id=record.id,
        event_type="policy_authorized",
        execution_status="authorized",
        approval_state="not_required",
    )
    await append_action_event(
        async_db_session,
        action_record_id=record.id,
        event_type="execution_started",
        execution_status="executing",
        approval_state="not_required",
    )
    events = await list_action_events(async_db_session, record.id)
    assert [event.execution_status for event in events] == ["authorized", "executing"]
    assert record.initial_execution_status == "proposed"


@pytest.mark.asyncio
async def test_cannot_claim_success_without_evidence(async_db_session):
    record = await make_record(async_db_session, status="authorized")
    await append_action_event(
        async_db_session,
        action_record_id=record.id,
        event_type="execution_started",
        execution_status="executing",
        approval_state="not_required",
    )
    with pytest.raises(CompanyOSAuditError, match="success requires"):
        await append_action_event(
            async_db_session,
            action_record_id=record.id,
            event_type="execution_succeeded",
            execution_status="succeeded",
            approval_state="not_required",
        )


@pytest.mark.asyncio
async def test_failure_requires_details(async_db_session):
    record = await make_record(async_db_session, status="authorized")
    await append_action_event(
        async_db_session,
        action_record_id=record.id,
        event_type="execution_started",
        execution_status="executing",
        approval_state="not_required",
    )
    with pytest.raises(CompanyOSAuditError, match="failure requires details"):
        await append_action_event(
            async_db_session,
            action_record_id=record.id,
            event_type="execution_failed",
            execution_status="failed",
            approval_state="not_required",
        )


@pytest.mark.asyncio
async def test_pending_approval_cannot_become_authorized(async_db_session):
    record = await make_record(async_db_session, status="awaiting_approval", approval="pending")
    with pytest.raises(CompanyOSAuditError, match="requires approved"):
        await append_action_event(
            async_db_session,
            action_record_id=record.id,
            event_type="approval_updated",
            execution_status="authorized",
            approval_state="pending",
        )


@pytest.mark.asyncio
async def test_terminal_status_cannot_transition(async_db_session):
    record = await make_record(async_db_session, status="blocked", approval="rejected")
    with pytest.raises(CompanyOSAuditError, match="invalid execution transition"):
        await append_action_event(
            async_db_session,
            action_record_id=record.id,
            event_type="execution_started",
            execution_status="executing",
            approval_state="approved",
        )
