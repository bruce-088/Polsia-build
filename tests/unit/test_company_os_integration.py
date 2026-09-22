"""Tests for Company OS integration-state enforcement."""

import pytest

from app.agents.company_os_integration import (
    CompanyOSIntegrationError,
    evaluate_integration_capability,
    require_integration_capability,
    validate_integration_registry,
)


@pytest.fixture
def registry():
    return {
        "modes": ["disabled", "documented", "sandbox", "read_only", "write_limited", "live"],
        "integrations": {
            "sendgrid": {
                "desired_use": ["acquisition_email"],
                "mode": "disabled",
                "verified": False,
                "external_writes": False,
            },
            "stripe": {
                "desired_use": ["billing", "mrr"],
                "mode": "read_only",
                "verified": True,
                "external_writes": False,
            },
            "github": {
                "desired_use": ["pull_requests"],
                "mode": "documented",
                "verified": True,
                "external_writes": True,
                "scope": "acqivo-company-os only",
            },
            "sandbox_mail": {
                "desired_use": ["transactional_email"],
                "mode": "sandbox",
                "verified": True,
                "external_writes": True,
            },
        },
    }


@pytest.mark.parametrize("phase", ["draft", "propose"])
def test_disabled_integration_allows_offline_preparation_only(registry, phase):
    decision = evaluate_integration_capability(
        registry, integration="sendgrid", phase=phase, requested_use="acquisition_email"
    )
    assert decision.allowed is True
    assert decision.external_write is False


def test_disabled_integration_cannot_execute(registry):
    decision = evaluate_integration_capability(
        registry, integration="sendgrid", phase="execute", requested_use="acquisition_email"
    )
    assert decision.allowed is False


def test_read_only_integration_can_read_but_not_write(registry):
    read = evaluate_integration_capability(
        registry, integration="stripe", phase="read", requested_use="mrr"
    )
    write = evaluate_integration_capability(
        registry, integration="stripe", phase="execute", requested_use="billing"
    )
    assert read.allowed is True
    assert write.allowed is False


def test_documented_mode_blocks_write_despite_verified_write_flag(registry):
    decision = evaluate_integration_capability(
        registry,
        integration="github",
        phase="execute",
        requested_use="pull_requests",
        requested_scope="acqivo-company-os only",
    )
    assert decision.allowed is False
    assert decision.external_write is False


def test_scope_must_match_exactly(registry):
    decision = evaluate_integration_capability(
        registry,
        integration="github",
        phase="propose",
        requested_use="pull_requests",
        requested_scope="all repositories",
    )
    assert decision.allowed is False


def test_unregistered_use_and_integration_fail_closed(registry):
    bad_use = evaluate_integration_capability(
        registry, integration="stripe", phase="read", requested_use="customer_export"
    )
    missing = evaluate_integration_capability(
        registry, integration="calendar", phase="execute"
    )
    assert bad_use.allowed is False
    assert missing.allowed is False


def test_verified_sandbox_write_is_distinct_from_live_write(registry):
    decision = require_integration_capability(
        registry,
        integration="sandbox_mail",
        phase="execute",
        requested_use="transactional_email",
    )
    assert decision.allowed is True
    assert decision.mode == "sandbox"


def test_execution_boundary_raises_when_denied(registry):
    with pytest.raises(CompanyOSIntegrationError, match="capability denied"):
        require_integration_capability(
            registry,
            integration="sendgrid",
            phase="execute",
            requested_use="acquisition_email",
        )


def test_registry_rejects_unknown_mode(registry):
    registry["integrations"]["sendgrid"]["mode"] = "credentials_present"
    with pytest.raises(CompanyOSIntegrationError, match="not canonical"):
        validate_integration_registry(registry)
