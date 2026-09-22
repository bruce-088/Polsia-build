"""Tests for deterministic Company OS workflow validation."""

import pytest

from app.agents.company_os_workflow import (
    CompanyOSWorkflowError,
    require_valid_transition,
    validate_workflow_definition,
)


@pytest.fixture
def workflow():
    return {
        "id": "prospect_to_meeting",
        "states": ["research", "scored", "nurture", "ready_to_send", "sent", "opted_out"],
        "terminal_states": ["opted_out"],
        "transitions": [
            {
                "from": "research",
                "to": "scored",
                "action": "score_against_icp",
                "risk_level": "GREEN",
                "requirements": ["public evidence preserved"],
            },
            {
                "from": "scored",
                "to": "nurture",
                "action": "deprioritize_prospect",
                "risk_level": "GREEN",
            },
            {
                "from": "ready_to_send",
                "to": "sent",
                "action": "send_outreach",
                "risk_level": "YELLOW",
                "integration": "sendgrid",
            },
            {
                "from": "sent",
                "to": "opted_out",
                "action": "record_opt_out",
                "risk_level": "GREEN",
            },
        ],
    }


def test_accepts_exact_registered_transition(workflow):
    result = require_valid_transition(
        workflow,
        current_state="research",
        action="score_against_icp",
        proposed_state="scored",
        proposed_risk="GREEN",
    )
    assert result.workflow_id == "prospect_to_meeting"
    assert result.requirements == ("public evidence preserved",)


def test_returns_canonical_integration_metadata(workflow):
    result = require_valid_transition(
        workflow,
        current_state="ready_to_send",
        action="send_outreach",
        proposed_state="sent",
    )
    assert result.integration == "sendgrid"
    assert result.risk_level == "YELLOW"


@pytest.mark.parametrize(
    ("current", "action", "target"),
    [
        ("research", "invent_transition", "scored"),
        ("research", "score_against_icp", "nurture"),
        ("scored", "deprioritize_prospect", "sent"),
    ],
)
def test_rejects_nonexistent_transition_without_repair(workflow, current, action, target):
    with pytest.raises(CompanyOSWorkflowError, match="transition is not registered"):
        require_valid_transition(
            workflow, current_state=current, action=action, proposed_state=target
        )


def test_rejects_unregistered_state(workflow):
    with pytest.raises(CompanyOSWorkflowError, match="proposed state is not registered"):
        require_valid_transition(
            workflow,
            current_state="research",
            action="score_against_icp",
            proposed_state="made_up",
        )


def test_rejects_transition_from_terminal_state(workflow):
    with pytest.raises(CompanyOSWorkflowError, match="terminal state cannot transition"):
        require_valid_transition(
            workflow,
            current_state="opted_out",
            action="send_outreach",
            proposed_state="sent",
        )


def test_rejects_risk_mismatch(workflow):
    with pytest.raises(CompanyOSWorkflowError, match="risk mismatch"):
        require_valid_transition(
            workflow,
            current_state="ready_to_send",
            action="send_outreach",
            proposed_state="sent",
            proposed_risk="GREEN",
        )


def test_definition_rejects_outgoing_transition_from_terminal(workflow):
    workflow["transitions"].append(
        {
            "from": "opted_out",
            "to": "sent",
            "action": "resume_sending",
            "risk_level": "GREEN",
        }
    )
    with pytest.raises(CompanyOSWorkflowError, match="cannot have outgoing transitions"):
        validate_workflow_definition(workflow)


def test_definition_rejects_duplicate_transition(workflow):
    workflow["transitions"].append(dict(workflow["transitions"][0]))
    with pytest.raises(CompanyOSWorkflowError, match="duplicate transition"):
        validate_workflow_definition(workflow)
