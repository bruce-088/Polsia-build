"""Tests for native Company OS Stage 1 decision mode."""

import json
from unittest.mock import patch

import pytest

from app.agents.company_os_contract import (
    CompanyOSContractError,
    parse_stage1_decision,
    stage1_json_schema,
    validate_stage1_decision,
)
from app.agents.crew_factory import run_agent_for_task
from app.agents.base_agent import claude_structured_output_available


def valid_decision() -> dict:
    return {
        "scenario_id": "SIM-001",
        "action": "score_against_icp",
        "risk_level": "GREEN",
        "state": "scored",
        "founder_approval": False,
        "handoff_to": "market_intelligence",
        "actions_taken": [],
        "actions_proposed": ["record_icp_score"],
        "assumptions": [],
        "notes": "Simulation only.",
    }


def contract_task() -> dict:
    return {
        "title": "Stage 1 scenario",
        "description": "Blind scenario and Company OS context.",
        "task_metadata": {
            "response_contract": "company_os_stage1",
            "scenario_id": "SIM-001",
        },
    }


def test_parse_stage1_decision_accepts_clean_complete_json():
    payload = valid_decision()
    assert parse_stage1_decision(json.dumps(payload), "SIM-001") == payload


def test_stage1_json_schema_is_strict_and_scenario_bound():
    schema = stage1_json_schema("SIM-001")
    assert schema["additionalProperties"] is False
    assert schema["properties"]["scenario_id"]["const"] == "SIM-001"
    assert set(schema["required"]) == set(valid_decision()) - {"notes"}


@pytest.mark.parametrize(
    "raw",
    [
        "preamble " + json.dumps(valid_decision()),
        "```json\n" + json.dumps(valid_decision()) + "\n```",
        "not json",
    ],
)
def test_parse_stage1_decision_rejects_wrapped_or_malformed_output(raw):
    with pytest.raises(CompanyOSContractError, match="clean JSON") as captured:
        parse_stage1_decision(raw, "SIM-001")
    assert captured.value.raw_output == raw


def test_validate_stage1_decision_does_not_repair_missing_fields():
    payload = valid_decision()
    del payload["founder_approval"]
    with pytest.raises(CompanyOSContractError, match="missing required fields"):
        validate_stage1_decision(payload, "SIM-001")


def test_validate_stage1_decision_rejects_scenario_mismatch():
    with pytest.raises(CompanyOSContractError, match="scenario_id mismatch"):
        validate_stage1_decision(valid_decision(), "SIM-999")


def test_parse_preserves_raw_json_when_semantic_validation_fails():
    raw = json.dumps({**valid_decision(), "scenario_id": "SIM-999"})
    with pytest.raises(CompanyOSContractError, match="scenario_id mismatch") as captured:
        parse_stage1_decision(raw, "SIM-001")
    assert captured.value.raw_output == raw


def test_crew_factory_uses_native_contract_mode(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.setenv(
        "CLAUDE_CLI_MOCK_RESPONSE",
        json.dumps({"result": json.dumps(valid_decision())}),
    )
    result = run_agent_for_task("social_media", contract_task(), {"company": {"name": "Acqivo"}})
    assert result == valid_decision()


def test_crew_factory_preserves_normal_agent_path():
    with patch("app.agents.social_media.agent.SocialMediaAgent.run") as mock_run:
        mock_run.return_value = {"summary": "native", "tweets": []}
        result = run_agent_for_task(
            "social_media",
            {"title": "Draft posts", "task_metadata": {}},
            {"company": {"name": "Acqivo"}},
        )
    assert result["summary"] == "native"


def test_real_contract_mode_uses_provider_structured_output(monkeypatch):
    monkeypatch.delenv("CLAUDE_CLI_MOCK", raising=False)
    with patch("app.agents.social_media.agent.SocialMediaAgent._run_claude_structured") as call:
        call.return_value = (valid_decision(), '{"structured_output":{}}')
        result = run_agent_for_task("social_media", contract_task(), {})
    assert result == valid_decision()
    assert call.call_args.args[1]["additionalProperties"] is False


def test_structured_semantic_failure_preserves_provider_evidence(monkeypatch):
    monkeypatch.delenv("CLAUDE_CLI_MOCK", raising=False)
    bad = {**valid_decision(), "scenario_id": "SIM-999"}
    evidence = '{"structured_output":{"scenario_id":"SIM-999"}}'
    with patch(
        "app.agents.social_media.agent.SocialMediaAgent._run_claude_structured",
        return_value=(bad, evidence),
    ):
        with pytest.raises(CompanyOSContractError, match="scenario_id mismatch") as captured:
            run_agent_for_task("social_media", contract_task(), {})
    assert captured.value.raw_output == evidence


def test_structured_capability_check_is_fail_closed():
    claude_structured_output_available.cache_clear()
    completed = type("Completed", (), {"returncode": 0, "stdout": "usage: claude"})()
    with patch("app.agents.base_agent.subprocess.run", return_value=completed):
        assert claude_structured_output_available() is False
    claude_structured_output_available.cache_clear()


def test_crew_factory_rejects_unknown_contract():
    task = {"title": "test", "task_metadata": {"response_contract": "unknown"}}
    with pytest.raises(ValueError, match="Unknown response contract"):
        run_agent_for_task("social_media", task, {})
