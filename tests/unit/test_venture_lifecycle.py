"""Deterministic venture lifecycle rules: gates, overrides, delegation, rubric."""
import math

import pytest

from app.agents.crew_factory import AGENT_MAP, VENTURE_AGENT_MAP
from app.agents.venture import lifecycle


def verified(value):
    return {"value": value, "verified": True, "source": "test"}


def passing_metrics(phase_key):
    metrics = {}
    for criterion in lifecycle.get_phase(phase_key).exit_gate:
        metrics[criterion.metric] = verified(criterion.threshold)
    return metrics


def test_phase_order_and_next_phase():
    assert lifecycle.PHASE_ORDER == ("research_validation", "launch", "marketing", "scaling")
    assert lifecycle.next_phase("research_validation") == "launch"
    assert lifecycle.next_phase("marketing") == "scaling"
    assert lifecycle.next_phase("scaling") is None
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.next_phase("growth")


def test_every_phase_is_wired_to_registered_agents():
    for key in lifecycle.PHASE_ORDER:
        phase = lifecycle.get_phase(key)
        assert phase.lead_agent in VENTURE_AGENT_MAP
        assert phase.exit_gate, f"{key} needs an exit gate"
        assert "Advanc" in " ".join(phase.founder_decisions) or key == "scaling"
        for worker in phase.worker_agents:
            assert worker in AGENT_MAP, f"{worker} is not an operating agent"
    for shared in (lifecycle.COMMANDER, lifecycle.TRACKER, lifecycle.PREDICTOR):
        assert shared in VENTURE_AGENT_MAP


def test_rubric_weights_sum_to_one():
    assert math.isclose(sum(w for w, _ in lifecycle.IDEA_RUBRIC.values()), 1.0)


@pytest.mark.parametrize("phase_key", lifecycle.PHASE_ORDER)
def test_gate_passes_only_on_verified_threshold_metrics(phase_key):
    result = lifecycle.evaluate_gate(phase_key, passing_metrics(phase_key))
    assert result["passed"] is True
    assert {c["status"] for c in result["criteria"]} == {"pass"}


def test_gate_fails_closed_on_missing_unverified_and_failing():
    metrics = passing_metrics("research_validation")
    del metrics["committed_signals"]
    metrics["smoke_test_visitors"] = {"value": 500, "verified": False, "source": "tracker"}
    metrics["smoke_test_conversion_rate"] = verified(0.01)
    result = lifecycle.evaluate_gate("research_validation", metrics)
    assert result["passed"] is False
    assert result["missing"] == ["committed_signals"]
    assert result["unverified"] == ["smoke_test_visitors"]
    assert result["failing"] == ["smoke_test_conversion_rate"]


def test_unverified_value_that_fails_is_reported_as_fail():
    metrics = passing_metrics("launch")
    metrics["paying_customers"] = {"value": 1, "verified": False}
    result = lifecycle.evaluate_gate("launch", metrics)
    assert result["failing"] == ["paying_customers"]


def test_upper_bound_criteria_use_less_than_or_equal():
    metrics = passing_metrics("launch")
    metrics["critical_open_defects"] = verified(1)
    assert lifecycle.evaluate_gate("launch", metrics)["failing"] == ["critical_open_defects"]


@pytest.mark.parametrize("bad", ["12", None, float("nan"), float("inf"), [], {}])
def test_non_numeric_metric_values_count_as_missing(bad):
    metrics = passing_metrics("launch")
    metrics["paying_customers"] = {"value": bad, "verified": True}
    assert lifecycle.evaluate_gate("launch", metrics)["missing"] == ["paying_customers"]


def test_bool_values_count_as_zero_or_one():
    metrics = passing_metrics("launch")
    metrics["mvp_live"] = verified(True)
    assert lifecycle.evaluate_gate("launch", metrics)["passed"] is True


def test_overrides_change_thresholds_only():
    metrics = passing_metrics("marketing")
    metrics["mrr_usd"] = verified(1500)
    assert lifecycle.evaluate_gate("marketing", metrics)["passed"] is False
    assert lifecycle.evaluate_gate("marketing", metrics, {"mrr_usd": 1000})["passed"] is True
    gate = lifecycle.effective_gate("marketing", {"mrr_usd": 1000})
    assert [c.op for c in gate] == [c.op for c in lifecycle.get_phase("marketing").exit_gate]


@pytest.mark.parametrize(
    "overrides",
    [
        {"research_validation": {"not_a_gate_metric": 1}},
        {"launch": {"paying_customers": "three"}},
        {"nonexistent_phase": {"mrr_usd": 1}},
        {"launch": 5},
        ["launch"],
    ],
)
def test_invalid_overrides_are_rejected(overrides):
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.validate_gate_overrides(overrides)


def test_delegation_rules():
    assert lifecycle.commander_dispatchable("launch") == ("launch", "tracker", "predictor")
    assert "ads_management" not in lifecycle.lead_dispatchable("research_validation")
    assert "ads_management" in lifecycle.lead_dispatchable("marketing")


def test_score_idea_is_deterministic_weighted_sum():
    perfect = {name: {"score": 5} for name in lifecycle.IDEA_RUBRIC}
    assert lifecycle.score_idea(perfect)["weighted_score"] == 100.0
    mixed = {
        "pain_intensity": 4,
        "willingness_to_pay": 3,
        "online_reachability": 5,
        "automation_fit": 4,
        "speed_to_signal": 2,
        "compliance_safety": 5,
        "differentiation": 1,
    }
    # 0.2*0.8 + 0.2*0.6 + 0.15*1 + 0.2*0.8 + 0.1*0.4 + 0.1*1 + 0.05*0.2 = 0.74
    result = lifecycle.score_idea(mixed)
    assert result["weighted_score"] == 74.0
    assert result["complete"] is True


def test_score_idea_counts_missing_and_out_of_range_as_zero():
    result = lifecycle.score_idea({"pain_intensity": 9, "automation_fit": "high"})
    assert result["weighted_score"] == 0.0
    assert result["complete"] is False
    assert set(result["missing"]) == set(lifecycle.IDEA_RUBRIC)


def test_phase_brief_mentions_gate_and_founder_decisions():
    brief = lifecycle.phase_brief("marketing", {"mrr_usd": 1000})
    assert "mrr_usd >= 1000" in brief
    assert "Founder (RED) decisions" in brief
