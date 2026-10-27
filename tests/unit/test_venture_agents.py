"""Venture-lifecycle agents: deterministic guards around model output."""
import json
from unittest.mock import patch

import pytest

from app.agents.commander.agent import CommanderAgent
from app.agents.crew_factory import VENTURE_AGENT_MAP, run_agent_for_task
from app.agents.launch.agent import LaunchAgent
from app.agents.marketing.agent import MarketingAgent
from app.agents.predictor.agent import PredictorAgent
from app.agents.research_validation.agent import ResearchValidationAgent
from app.agents.scaling.agent import ScalingAgent
from app.agents.tracker.agent import TrackerAgent
from app.agents.venture import lifecycle


def snapshot(phase="research_validation", status="active", passing=False):
    metrics = {}
    if passing:
        metrics = {
            c.metric: {"value": c.threshold, "verified": True, "source": "test"}
            for c in lifecycle.get_phase(phase).exit_gate
        }
    return {
        "id": 1,
        "name": "Test Venture",
        "idea": "Agent-run bookkeeping cleanup for Etsy sellers",
        "target_customer": "Etsy sellers",
        "business_model": "$49/mo",
        "phase": phase,
        "status": status,
        "metrics": metrics,
        "scores": {},
        "history": [],
    }


def task(snap=None, **metadata):
    return {
        "title": "Plan this cycle",
        "description": "Move the venture toward its gate.",
        "task_metadata": {"venture": snap or snapshot(), **metadata},
    }


@pytest.mark.parametrize("agent_type", sorted(VENTURE_AGENT_MAP))
def test_every_venture_agent_runs_in_mock_mode(agent_type):
    """With the global CLI mock (plain-text response), every agent degrades
    to a safe proposal instead of crashing."""
    result = run_agent_for_task(agent_type, task(), {})
    assert result["status"] == "proposal"
    assert result["summary"]


@pytest.mark.parametrize(
    ("agent_class", "phase"),
    [
        (ResearchValidationAgent, "research_validation"),
        (LaunchAgent, "launch"),
        (MarketingAgent, "marketing"),
        (ScalingAgent, "scaling"),
    ],
)
def test_phase_lead_only_delegates_to_its_workers(agent_class, phase):
    workers = lifecycle.lead_dispatchable(phase)
    outsider = next(a for a in ("ads_management", "governance", "revenue_ops", "orchestrator") if a not in workers)
    model_output = {
        "summary": "plan",
        "proposed_tasks": [
            {"agent_type": workers[0], "title": "Do the thing", "description": "brief"},
            {"agent_type": outsider, "title": "Out of bounds"},
            {"agent_type": workers[0], "title": ""},
            {"agent_type": "commander", "title": "Escalate upward"},
        ],
        "recommendation": "continue",
    }
    with patch.object(agent_class, "call_claude_structured", return_value=model_output):
        result = agent_class().run(task(snapshot(phase)), {})
    assert [t["agent_type"] for t in result["proposed_tasks"]] == [workers[0]]
    assert len(result["rejected_tasks"]) == 3
    assert result["phase"] == phase
    assert result["status"] == "proposal"


def test_phase_lead_caps_delegations():
    proposed = [{"agent_type": "competitor_research", "title": f"t{i}"} for i in range(9)]
    with patch.object(ResearchValidationAgent, "call_claude_structured", return_value={"proposed_tasks": proposed}):
        result = ResearchValidationAgent().run(task(), {})
    assert len(result["proposed_tasks"]) == 5
    assert len(result["rejected_tasks"]) == 4


def test_spend_and_unknown_budget_become_founder_decisions():
    output = {
        "experiments": [
            {"hypothesis": "free organic test", "budget_usd": 0},
            {"hypothesis": "paid test", "budget_usd": 150},
            {"hypothesis": "budget not stated"},
        ]
    }
    with patch.object(MarketingAgent, "call_claude_structured", return_value=output):
        result = MarketingAgent().run(task(snapshot("marketing")), {})
    flags = [e["requires_founder_approval"] for e in result["experiments"]]
    assert flags == [False, True, True]
    assert all(d["risk_level"] == "RED" for d in result["founder_decisions"])
    assert len(result["founder_decisions"]) == 2


def test_advance_is_blocked_when_gate_has_not_passed():
    with patch.object(LaunchAgent, "call_claude_structured", return_value={"recommendation": "advance"}):
        result = LaunchAgent().run(task(snapshot("launch")), {})
    assert result["recommendation"] == "continue"
    assert any("advance blocked" in note for note in result["guard_notes"])
    assert not any("Advance" in d["decision"] for d in result["founder_decisions"])


def test_advance_survives_only_as_a_founder_decision_when_gate_passes():
    with patch.object(LaunchAgent, "call_claude_structured", return_value={"recommendation": "advance"}):
        result = LaunchAgent().run(task(snapshot("launch", passing=True)), {})
    assert result["recommendation"] == "advance"
    assert {"decision": "Advance to marketing", "why": "Launched gate passed on verified metrics", "risk_level": "RED"} in result[
        "founder_decisions"
    ]


def test_model_cannot_claim_gate_passed():
    """The gate is recomputed from snapshot metrics, ignoring model claims."""
    snap = snapshot("launch")
    snap["gate"] = {"passed": True}
    with patch.object(LaunchAgent, "call_claude_structured", return_value={"recommendation": "advance", "gate": {"passed": True}}):
        result = LaunchAgent().run(task(snap), {})
    assert result["gate"]["passed"] is False
    assert result["recommendation"] == "continue"


def test_kill_and_pivot_are_founder_decisions():
    with patch.object(ResearchValidationAgent, "call_claude_structured", return_value={"recommendation": "kill"}):
        result = ResearchValidationAgent().run(task(), {})
    assert result["recommendation"] == "kill"
    assert {"decision": "Kill this venture", "why": "recommended by agent", "risk_level": "RED"} in result["founder_decisions"]


def test_ideation_mode_dedupes_and_caps_ideas():
    ideas = [{"name": f"Idea {i % 12}", "one_liner": "x"} for i in range(15)]
    ideas.append({"one_liner": "no name"})
    with patch.object(ResearchValidationAgent, "call_claude_structured", return_value={"ideas": ideas}):
        result = ResearchValidationAgent().run({"title": "Ideate", "task_metadata": {"mode": "ideate"}}, {})
    assert result["mode"] == "ideate"
    assert len(result["ideas"]) == 10
    assert len({i["name"] for i in result["ideas"]}) == 10
    assert set(result["ideas"][0]) >= {"name", "smoke_test", "riskiest_assumption"}


def test_commander_dispatches_only_to_phase_lead_tracker_and_predictor():
    output = {
        "dispatch": [
            {"agent_type": "launch", "title": "Plan the launch"},
            {"agent_type": "tracker", "title": "Refresh metrics"},
            {"agent_type": "email_outreach", "title": "Send emails"},
            {"agent_type": "marketing", "title": "Skip ahead"},
        ],
        "phase_recommendation": "advance",
    }
    with patch.object(CommanderAgent, "call_claude_structured", return_value=output):
        result = CommanderAgent().run(task(snapshot("launch")), {})
    assert [d["agent_type"] for d in result["dispatch"]] == ["launch", "tracker"]
    assert {r["agent_type"] for r in result["rejected_dispatch"]} == {"email_outreach", "marketing"}
    assert result["phase_recommendation"] == "continue"


@pytest.mark.parametrize("status", ["paused", "killed", "graduated"])
def test_commander_does_nothing_for_inactive_ventures(status):
    with patch.object(CommanderAgent, "call_claude_structured") as call:
        result = CommanderAgent().run(task(snapshot(status=status)), {})
    call.assert_not_called()
    assert result["dispatch"] == []


def test_tracker_requires_numbers_sources_and_never_verifies():
    output = {
        "metric_updates": [
            {"metric": "smoke_test_visitors", "value": 240, "source": "plausible.io export 2026-10-20", "evidence": "240 uniques"},
            {"metric": "committed_signals", "value": 2, "source": ""},
            {"metric": "Bad Name", "value": 1, "source": "x"},
            {"metric": "paying_customers", "value": "a few", "source": "x"},
            {"metric": "mrr_usd", "value": 120, "source": "stripe", "verified": True},
        ]
    }
    with patch.object(TrackerAgent, "call_claude_structured", return_value=output):
        result = TrackerAgent().run(task(evidence=["analytics export"]), {})
    assert [u["metric"] for u in result["metric_updates"]] == ["smoke_test_visitors", "mrr_usd"]
    assert all(u["verified"] is False for u in result["metric_updates"])
    assert len(result["rejected_updates"]) == 3


def test_predictor_computes_weighted_score_itself():
    scores = {name: {"score": 5, "rationale": "r", "confidence": "high"} for name in lifecycle.IDEA_RUBRIC}
    scores["differentiation"] = {"score": 0, "rationale": "crowded", "confidence": "sure"}
    output = {"scores": scores, "weighted_score": 100}
    with patch.object(PredictorAgent, "call_claude_structured", return_value=output):
        result = PredictorAgent().run(task(mode="score_idea"), {})
    assert result["idea_score"]["weighted_score"] == 95.0
    assert result["idea_score"]["criteria"]["differentiation"]["confidence"] == "low"
    assert result["forecast"] is True


@pytest.mark.parametrize(("raw", "expected"), [(1.7, 1.0), (-0.2, 0.0), (0.35, 0.35), ("likely", None)])
def test_predictor_forecast_clamps_probability(raw, expected):
    with patch.object(PredictorAgent, "call_claude_structured", return_value={"gate_pass_probability": raw, "horizon_days": 30}):
        result = PredictorAgent().run(task(), {})
    assert result["gate_pass_probability"] == expected
    assert result["horizon_days"] == 30
    assert result["mode"] == "forecast"


def test_prompts_carry_venture_and_gate_context():
    with patch.object(CommanderAgent, "call_claude_structured", return_value={}) as call:
        CommanderAgent().run(task(snapshot("marketing")), {})
    prompt = call.call_args.args[0]
    assert "Test Venture" in prompt
    assert "Repeatable acquisition" in prompt
    assert "mrr_usd: missing" in prompt
    assert "You produce proposals only" in prompt


@pytest.mark.parametrize(
    ("agent_type", "agent_class", "role_phrase"),
    [
        ("commander", CommanderAgent, "Never change a venture's phase"),
        ("tracker", TrackerAgent, "mark a metric verified"),
        ("research_validation", ResearchValidationAgent, "fabricate demand evidence"),
        ("marketing", MarketingAgent, "Never start paid spend"),
    ],
)
def test_company_os_mode_uses_bounded_venture_roles(agent_type, agent_class, role_phrase, monkeypatch):
    monkeypatch.delenv("CLAUDE_CLI_MOCK", raising=False)
    payload = {
        "scenario_id": "SIM-V01",
        "action": "request_founder_approval",
        "risk_level": "RED",
        "state": "awaiting_approval",
        "founder_approval": True,
        "handoff_to": "founder",
        "actions_taken": [],
        "actions_proposed": ["request_approval"],
        "assumptions": [],
    }
    venture_task = {
        "title": "Company OS scenario",
        "description": "Blind facts.",
        "task_metadata": {"response_contract": "company_os_stage1", "scenario_id": "SIM-V01"},
    }
    with patch.object(
        agent_class, "_run_claude_structured", return_value=(payload, json.dumps({"structured_output": payload}))
    ) as call:
        assert run_agent_for_task(agent_type, venture_task, {}) == payload
    assert role_phrase in " ".join(call.call_args.args[0].split())
