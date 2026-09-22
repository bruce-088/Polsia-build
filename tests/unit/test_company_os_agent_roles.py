"""Tests for explicit Company OS fulfillment and governance agents."""

import json
from unittest.mock import patch

import pytest

from app.agents.crew_factory import AGENT_MAP, run_agent_for_task
from app.agents.governance.agent import GovernanceAgent
from app.agents.revenue_ops.agent import RevenueOperationsAgent


def decision(scenario_id="SIM-009"):
    return {
        "scenario_id": scenario_id,
        "action": "request_missing_authority",
        "risk_level": "GREEN",
        "state": "blocked",
        "founder_approval": False,
        "handoff_to": "customer",
        "actions_taken": [],
        "actions_proposed": ["request_authority"],
        "assumptions": [],
    }


@pytest.mark.parametrize("agent_type", ["revenue_ops", "governance"])
def test_explicit_agent_types_are_registered(agent_type):
    assert agent_type in AGENT_MAP


@pytest.mark.parametrize(
    ("agent_type", "agent_class", "role_phrase"),
    [
        ("revenue_ops", RevenueOperationsAgent, "Never quote unauthorized prices"),
        ("governance", GovernanceAgent, "Never approve your own RED action"),
    ],
)
def test_company_os_mode_includes_bounded_role(agent_type, agent_class, role_phrase):
    payload = decision()
    task = {
        "title": "Company OS scenario",
        "description": "Blind facts and canonical Company OS.",
        "task_metadata": {"response_contract": "company_os_stage1", "scenario_id": "SIM-009"},
    }
    with patch.object(agent_class, "call_claude", return_value=json.dumps(payload)) as call:
        assert run_agent_for_task(agent_type, task, {}) == payload
    assert role_phrase in call.call_args.args[0]


@pytest.mark.parametrize(
    ("agent_type", "module_path", "class_name"),
    [
        ("revenue_ops", "app.agents.revenue_ops.agent", "RevenueOperationsAgent"),
        ("governance", "app.agents.governance.agent", "GovernanceAgent"),
    ],
)
def test_normal_mode_is_proposal_only(agent_type, module_path, class_name):
    with patch(f"{module_path}.{class_name}.call_claude_structured") as call:
        call.return_value = {"summary": "reviewed", "recommended_actions": ["draft"]}
        result = run_agent_for_task(
            agent_type,
            {"title": "Review", "description": "No external action."},
            {"company": {"name": "Acqivo"}},
        )
    assert result["status"] == "proposal"
    assert "actions_taken" not in result
