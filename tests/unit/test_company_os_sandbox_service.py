"""Tests for authoritative, append-only Stage 2 sandbox persistence."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.company_os_sandbox import CompanyOSSandboxEvent
from app.services.company_os_sandbox_service import (
    SandboxConflictError,
    SandboxPersistenceError,
    append_sandbox_event,
    create_sandbox_run,
    create_workflow_instance,
    export_sandbox_event,
    list_sandbox_events,
)

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
        "primary_classification": {"enum": ["acquisition", "failure_recovery"]},
        "workflow_id": {"type": "string", "minLength": 1},
        "entity_type": {"enum": ["prospect", "workflow_run"]},
        "entity_id": {"type": "string", "minLength": 1},
        "event_type": {"enum": [
            "input_received", "transition_completed", "failure_detected",
            "terminal_outcome",
        ]},
        "agent_type": {"type": "string", "minLength": 1},
        "action": {"type": "string", "minLength": 1},
        "risk_level": {"enum": ["GREEN", "YELLOW", "RED"]},
        "state_before": {"type": ["string", "null"]},
        "state_after": {"type": ["string", "null"]},
        "result": {"enum": ["observed", "executed_in_sandbox", "failed"]},
        "external_side_effect": {"const": False},
        "evidence_refs": {
            "type": "array", "minItems": 1, "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "autonomy": {
            "type": "object", "required": ["class", "founder_minutes"],
            "properties": {
                "class": {"enum": ["A0", "A1", "A2", "A3"]},
                "founder_minutes": {"type": "number", "minimum": 0},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}

WORKFLOW = {
    "id": "prospect_to_meeting",
    "states": ["created", "qualified", "contacted", "closed"],
    "terminal_states": ["closed"],
    "transitions": [
        {
            "from": "created",
            "to": "qualified",
            "action": "qualify_prospect",
            "risk_level": "GREEN",
        },
        {
            "from": "qualified",
            "to": "contacted",
            "action": "contact_prospect",
            "risk_level": "GREEN",
        },
        {
            "from": "created",
            "to": "closed",
            "action": "qualify_prospect",
            "risk_level": "GREEN",
        },
    ],
}


def event_payload(
    event_id: str = "event-1",
    *,
    state_before: str | None = "created",
    state_after: str | None = "qualified",
    event_type: str = "transition_completed",
) -> dict:
    return {
        "schema_version": "0.1.0",
        "event_id": event_id,
        "occurred_at": "2026-09-24T12:00:00Z",
        "primary_classification": "acquisition",
        "workflow_id": "prospect_to_meeting",
        "entity_type": "prospect",
        "entity_id": "prospect-1",
        "event_type": event_type,
        "agent_type": "revenue_ops",
        "action": "qualify_prospect",
        "risk_level": "GREEN",
        "state_before": state_before,
        "state_after": state_after,
        "result": "executed_in_sandbox",
        "external_side_effect": False,
        "evidence_refs": ["sandbox://receipt/1"],
        "autonomy": {"class": "A0", "founder_minutes": 0},
    }


async def make_instance(db):
    run = await create_sandbox_run(
        db,
        run_id="stage2-run-1",
        company_slug="acqivo",
        protocol_version="0.1.0",
        input_manifest={"company_os_commit": "a" * 40},
    )
    instance = await create_workflow_instance(
        db,
        sandbox_run_id=run.id,
        workflow_id="prospect_to_meeting",
        entity_type="prospect",
        entity_id="prospect-1",
        initial_state="created",
    )
    return run, instance


@pytest.mark.asyncio
async def test_create_run_and_unique_workflow_identity(async_db_session):
    run, _ = await make_instance(async_db_session)
    with pytest.raises(SandboxConflictError, match="identity already exists"):
        await create_workflow_instance(
            async_db_session,
            sandbox_run_id=run.id,
            workflow_id="prospect_to_meeting",
            entity_type="prospect",
            entity_id="prospect-1",
            initial_state="created",
        )


@pytest.mark.asyncio
async def test_valid_event_persists_and_exports_exact_schema_payload(async_db_session):
    run, instance = await make_instance(async_db_session)
    supplied = event_payload()
    event = await append_sandbox_event(
        async_db_session,
        workflow_instance_id=instance.id,
        expected_version=0,
        idempotency_key="delivery-1",
        event_payload=supplied,
        event_schema=EVENT_SCHEMA,
        workflow_definition=WORKFLOW,
        native_decision={"action": "qualify_prospect"},
    )

    exported = export_sandbox_event(event)
    assert exported == {
        **supplied,
        "run_id": "stage2-run-1",
        "company_slug": "acqivo",
        "sequence": 1,
    }
    Draft202012Validator(
        EVENT_SCHEMA, format_checker=FormatChecker()
    ).validate(exported)
    assert "idempotency_key" not in exported
    assert "native_decision" not in exported
    assert instance.current_state == "qualified"
    assert instance.version == 1
    assert (await list_sandbox_events(async_db_session, sandbox_run_id=run.id)) == [event]


@pytest.mark.asyncio
async def test_stale_version_rejects_without_state_or_event_mutation(async_db_session):
    run, instance = await make_instance(async_db_session)
    with pytest.raises(SandboxConflictError, match="stale workflow version"):
        await append_sandbox_event(
            async_db_session,
            workflow_instance_id=instance.id,
            expected_version=1,
            idempotency_key="delivery-stale",
            event_payload=event_payload(),
            event_schema=EVENT_SCHEMA,
            workflow_definition=WORKFLOW,
            native_decision={"action": "qualify_prospect"},
        )
    assert instance.current_state == "created"
    assert instance.version == 0
    assert await async_db_session.scalar(
        select(func.count()).select_from(CompanyOSSandboxEvent).where(
            CompanyOSSandboxEvent.sandbox_run_id == run.id
        )
    ) == 0


@pytest.mark.asyncio
async def test_state_before_mismatch_rejects_without_mutation(async_db_session):
    _, instance = await make_instance(async_db_session)
    with pytest.raises(SandboxConflictError, match="state_before"):
        await append_sandbox_event(
            async_db_session,
            workflow_instance_id=instance.id,
            expected_version=0,
            idempotency_key="delivery-wrong-state",
            event_payload=event_payload(state_before="researched"),
            event_schema=EVENT_SCHEMA,
            workflow_definition=WORKFLOW,
            native_decision={"action": "qualify_prospect"},
        )
    assert (instance.current_state, instance.version) == ("created", 0)


@pytest.mark.asyncio
async def test_invalid_schema_and_missing_evidence_reject_without_mutation(
    async_db_session,
):
    _, instance = await make_instance(async_db_session)
    invalid = event_payload()
    invalid["evidence_refs"] = []
    with pytest.raises(SandboxPersistenceError, match="does not validate"):
        await append_sandbox_event(
            async_db_session,
            workflow_instance_id=instance.id,
            expected_version=0,
            idempotency_key="delivery-invalid",
            event_payload=invalid,
            event_schema=EVENT_SCHEMA,
            workflow_definition=WORKFLOW,
            native_decision={"action": "qualify_prospect"},
        )
    assert (instance.current_state, instance.version) == ("created", 0)


@pytest.mark.asyncio
async def test_duplicate_idempotency_key_rejected(async_db_session):
    _, instance = await make_instance(async_db_session)
    await append_sandbox_event(
        async_db_session,
        workflow_instance_id=instance.id,
        expected_version=0,
        idempotency_key="delivery-1",
        event_payload=event_payload(),
        event_schema=EVENT_SCHEMA,
        workflow_definition=WORKFLOW,
        native_decision={"action": "qualify_prospect"},
    )
    with pytest.raises(SandboxConflictError, match="duplicate"):
        await append_sandbox_event(
            async_db_session,
            workflow_instance_id=instance.id,
            expected_version=1,
            idempotency_key="delivery-1",
            event_payload=event_payload(
                "event-2", state_before="qualified", state_after="contacted"
            ),
            event_schema=EVENT_SCHEMA,
            workflow_definition=WORKFLOW,
            native_decision={"action": "contact_prospect"},
        )


@pytest.mark.asyncio
async def test_database_constraints_reject_duplicate_sequence_and_event_id(
    async_db_session,
):
    run, instance = await make_instance(async_db_session)
    first = await append_sandbox_event(
        async_db_session,
        workflow_instance_id=instance.id,
        expected_version=0,
        idempotency_key="delivery-1",
        event_payload=event_payload(),
        event_schema=EVENT_SCHEMA,
        workflow_definition=WORKFLOW,
        native_decision={"action": "qualify_prospect"},
    )
    duplicate = CompanyOSSandboxEvent(
        sandbox_run_id=run.id,
        workflow_instance_id=instance.id,
        event_id=first.event_id,
        sequence=first.sequence,
        idempotency_key="delivery-other",
        primary_classification="acquisition",
        event_type="input_received",
        result="observed",
        risk_level="GREEN",
        state_before="qualified",
        state_after="qualified",
        external_side_effect=False,
        payload=deepcopy(first.payload),
        native_decision={"action": "observe"},
    )
    async_db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await async_db_session.flush()


@pytest.mark.asyncio
async def test_terminal_event_sets_terminal_and_second_terminal_is_blocked(
    async_db_session,
):
    _, instance = await make_instance(async_db_session)
    terminal = event_payload(
        event_type="terminal_outcome",
        state_before="created",
        state_after="closed",
    )
    await append_sandbox_event(
        async_db_session,
        workflow_instance_id=instance.id,
        expected_version=0,
        idempotency_key="terminal-1",
        event_payload=terminal,
        event_schema=EVENT_SCHEMA,
        workflow_definition=WORKFLOW,
        native_decision={"action": "close"},
    )
    assert instance.terminal is True
    assert (instance.current_state, instance.version) == ("closed", 1)
    with pytest.raises(SandboxConflictError, match="terminal workflow"):
        await append_sandbox_event(
            async_db_session,
            workflow_instance_id=instance.id,
            expected_version=1,
            idempotency_key="terminal-2",
            event_payload=event_payload(
                "event-2",
                event_type="terminal_outcome",
                state_before="closed",
                state_after="closed",
            ),
            event_schema=EVENT_SCHEMA,
            workflow_definition=WORKFLOW,
            native_decision={"action": "close_again"},
        )


@pytest.mark.asyncio
async def test_native_decision_or_failure_is_required_exclusively(async_db_session):
    _, instance = await make_instance(async_db_session)
    for decision, failure in ((None, None), ({"ok": True}, {"error": "bad"})):
        with pytest.raises(SandboxPersistenceError, match="exactly one"):
            await append_sandbox_event(
                async_db_session,
                workflow_instance_id=instance.id,
                expected_version=0,
                idempotency_key="evidence",
                event_payload=event_payload(),
                event_schema=EVENT_SCHEMA,
                workflow_definition=WORKFLOW,
                native_decision=decision,
                native_failure=failure,
            )


def test_service_surface_has_no_update_or_delete_paths():
    import app.services.company_os_sandbox_service as service

    public_names = {name for name in dir(service) if not name.startswith("_")}
    assert not any(name.startswith(("update_", "delete_")) for name in public_names)


@pytest.mark.asyncio
async def test_unregistered_transition_rejects_without_mutation(async_db_session):
    run, instance = await make_instance(async_db_session)
    invalid = event_payload(state_after="contacted")
    with pytest.raises(SandboxPersistenceError, match="invalid workflow transition"):
        await append_sandbox_event(
            async_db_session,
            workflow_instance_id=instance.id,
            expected_version=0,
            idempotency_key="unregistered-transition",
            event_payload=invalid,
            event_schema=EVENT_SCHEMA,
            workflow_definition=WORKFLOW,
            native_decision={"action": "qualify_prospect"},
        )
    assert (instance.current_state, instance.version) == ("created", 0)
    assert await async_db_session.scalar(
        select(func.count()).select_from(CompanyOSSandboxEvent).where(
            CompanyOSSandboxEvent.sandbox_run_id == run.id
        )
    ) == 0


@pytest.mark.asyncio
async def test_terminal_outcome_requires_canonical_terminal_state(async_db_session):
    _, instance = await make_instance(async_db_session)
    invalid = event_payload(
        event_type="terminal_outcome",
        state_before="created",
        state_after="created",
    )
    with pytest.raises(SandboxPersistenceError, match="canonical terminal state"):
        await append_sandbox_event(
            async_db_session,
            workflow_instance_id=instance.id,
            expected_version=0,
            idempotency_key="invalid-terminal",
            event_payload=invalid,
            event_schema=EVENT_SCHEMA,
            workflow_definition=WORKFLOW,
            native_decision={"action": "qualify_prospect"},
        )
    assert instance.terminal is False
