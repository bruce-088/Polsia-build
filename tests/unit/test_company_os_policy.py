"""Tests for the central Company OS policy gate."""

import pytest

from app.agents.company_os_policy import (
    CompanyOSPolicyError,
    evaluate_action_policy,
    require_action_allowed,
)


@pytest.fixture
def policy():
    return {
        "limits": {"max_discount_percent": 10, "max_paid_media_budget_adjustment_percent": 15},
        "red_action_types": ["pricing_change", "first_live_integration_activation"],
        "hard_denies": ["bypass_opt_out", "fabricate_consent"],
        "non_approvable_blocks": [
            "message_contact_with_unverified_consent_or_eligibility",
            "execute_external_write_through_disabled_or_unverified_integration",
        ],
    }


def test_allows_low_risk_non_write(policy):
    result = evaluate_action_policy(
        {"action_type": "draft_message", "risk_level": "GREEN", "external_write": False},
        policy,
    )
    assert result.disposition == "allow"


def test_red_action_requires_verified_founder_approval(policy):
    intent = {"action_type": "pricing_change", "risk_level": "GREEN", "external_write": False}
    result = evaluate_action_policy(intent, policy)
    assert result.disposition == "requires_founder"
    assert result.effective_risk == "RED"
    intent["founder_approval_status"] = "approved"
    assert evaluate_action_policy(intent, policy).disposition == "allow"


def test_hard_deny_cannot_be_overridden_by_founder(policy):
    result = evaluate_action_policy(
        {
            "action_type": "bypass_opt_out",
            "risk_level": "RED",
            "external_write": True,
            "integration_mode": "live",
            "founder_approval_status": "approved",
        },
        policy,
    )
    assert result.disposition == "block"
    assert result.founder_approval_required is False


def test_evaluates_full_request_and_prevents_limit_splitting(policy):
    result = evaluate_action_policy(
        {
            "action_type": "adjust_paid_media_budget",
            "risk_level": "YELLOW",
            "external_write": False,
            "limit_name": "max_paid_media_budget_adjustment_percent",
            "requested_total": 22,
            "proposed_first_step": 15,
        },
        policy,
    )
    assert result.disposition == "requires_founder"
    assert "22 > 15" in result.reasons[0]


@pytest.mark.parametrize("mode", [None, "disabled", "documented", "read_only", "unverified"])
def test_blocks_external_write_without_write_capable_integration(policy, mode):
    result = evaluate_action_policy(
        {
            "action_type": "send_message",
            "risk_level": "GREEN",
            "external_write": True,
            "integration_mode": mode,
        },
        policy,
    )
    assert result.disposition == "block"


def test_blocks_unverified_authority_or_consent(policy):
    base = {
        "action_type": "send_message",
        "risk_level": "GREEN",
        "external_write": True,
        "integration_mode": "sandbox",
    }
    authority = evaluate_action_policy(
        {**base, "requires_authority": True, "authority_verified": False}, policy
    )
    consent = evaluate_action_policy(
        {**base, "requires_consent": True, "consent_verified": None}, policy
    )
    assert authority.disposition == "block"
    assert consent.disposition == "block"


def test_unknown_bound_escalates_and_execution_boundary_fails_closed(policy):
    intent = {
        "action_type": "issue_credit",
        "risk_level": "YELLOW",
        "external_write": False,
        "limit_name": "max_support_credit_usd",
        "requested_total": 20,
    }
    assert evaluate_action_policy(intent, policy).disposition == "requires_founder"
    with pytest.raises(CompanyOSPolicyError, match="not authorized"):
        require_action_allowed(intent, policy)
