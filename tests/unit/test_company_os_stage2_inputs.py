"""The runtime loader preserves canonical facts and derives only verified eligibility."""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime

import pytest

from app.config import settings
from app.services.company_os_stage2_inputs import (
    Stage2InputError,
    apply_stage2_signal,
    build_stage2_snapshot,
    load_stage2_inputs,
)
from tests.unit.test_company_os_stage2_required_paths import FIXTURES, MANIFEST, REGISTRY


@pytest.fixture(autouse=True)
def sandbox(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_mode", True)


def load(fixtures=None, registry=None):
    fixtures = FIXTURES if fixtures is None else fixtures
    return load_stage2_inputs(fixtures, REGISTRY if registry is None else registry,
                              fixtures["approved_templates"], run_id="loader-proof",
                              compliance_policy={"version": MANIFEST["compliance_policy_version"],
                                                 "sha256": MANIFEST["compliance_policy_sha256"]})


def test_loader_preserves_all_source_inputs_and_derives_eligibility():
    fixtures, registry = deepcopy(FIXTURES), deepcopy(REGISTRY)
    world = load(fixtures, registry)
    assert fixtures == FIXTURES and registry == REGISTRY
    assert world.input_cases == {case["id"]: case for case in FIXTURES["cases"]}
    for case in FIXTURES["cases"]:
        contact = case["contact_record"]
        loaded = world.contacts[contact["recipient_id"]]
        for field, value in contact.items():
            assert loaded[field] == value
        assert loaded["consent_verified"] == (case["consent_record"] is not None)
        assert world.consents[contact["recipient_id"]] == (case["consent_record"] or {})
    assert world.is_suppressed("l-syn-02")
    assert world.leads["l-syn-01"]["eligible"] is True
    assert len(world.signals["l-syn-03"]) == 1  # repeated delivery is deduplicated
    assert world.customers["c-syn-02"]["authority_verified"] is False
    assert world.payments["c-syn-03"]["status"] == "failed"
    assert not world.contacts["l-syn-stale-01"]["consent_verified"]
    assert world.suppression_snapshots["l-syn-stale-01"]["checked_at"] is None
    assert world.templates == {t["template_id"]: t for t in FIXTURES["approved_templates"]}
    assert world.queue["io-syn-01"]["original_adapter_key"] is None
    assert world.queue["io-syn-01"]["original_action"] == "send_outreach"
    assert world.action_failures == {("prospect_to_meeting", "p-syn-03", "send_outreach"): True}
    assert not world.outcomes and not world.dispatches


@pytest.mark.parametrize("field,value", [
    ("recipient_id", "other"), ("contact_email", "other@example.invalid"),
    ("method", "manual"), ("revoked_at", "2026-09-24T15:00:00Z"),
    ("captured_at", "2099-01-01T00:00:00Z"), ("method", None),
])
def test_loader_never_infers_consent_from_unrelated_or_incomplete_record(field, value):
    fixtures = deepcopy(FIXTURES)
    consent = fixtures["cases"][0]["consent_record"]
    if value is None:
        del consent[field]
    else:
        consent[field] = value
    assert load(fixtures).contacts["p-syn-01"]["consent_verified"] is False


@pytest.mark.parametrize("field", ["captured_at", "address", "contact_email", "recipient_id"])
def test_missing_contact_facts_are_rejected_instead_of_filled(field):
    fixtures = deepcopy(FIXTURES)
    del fixtures["cases"][0]["contact_record"][field]
    with pytest.raises(Stage2InputError):
        load(fixtures)


def test_missing_sender_capture_time_is_not_replaced_with_frozen_clock():
    registry = deepcopy(REGISTRY)
    del registry["integrations"]["sendgrid"]["sandbox_sender"]["captured_at"]
    with pytest.raises(Stage2InputError, match="sender captured_at"):
        load(registry=registry)


@pytest.mark.parametrize("field", ["origin_workflow_id", "origin_entity_id", "original_action"])
def test_missing_queue_identity_is_rejected(field):
    fixtures = deepcopy(FIXTURES)
    case = next(c for c in fixtures["cases"] if c["id"] == "sandbox-recovery")
    del case["inputs"][0]["facts"][field]
    with pytest.raises(Stage2InputError, match="queue origin"):
        load(fixtures)


def test_action_failure_does_not_need_or_guess_a_database_key():
    world = load()
    world.arm_action_failure("prospect_to_meeting", "other", "send_outreach", "mail", "wrong")
    assert not world._after_effect_once
    world.arm_action_failure("prospect_to_meeting", "p-syn-03", "send_outreach", "mail", "actual-db-derived-key")
    assert world._after_effect_once == {("mail", "actual-db-derived-key")}
    assert not world.action_failures
    world.arm_action_failure("prospect_to_meeting", "p-syn-03", "send_outreach", "mail", "another-key")
    assert world._after_effect_once == {("mail", "actual-db-derived-key")}


def test_load_stage2_inputs_unchanged_after_split():
    fixtures = deepcopy(FIXTURES)
    world = build_stage2_snapshot(
        fixtures, REGISTRY, fixtures["approved_templates"], run_id="loader-proof",
        compliance_policy={"version": MANIFEST["compliance_policy_version"],
                           "sha256": MANIFEST["compliance_policy_sha256"]},
    )
    assert not world.signals and not world.queue and not world.action_failures
    assert not world.payments and not world.leads
    assert len(world.contacts) == len(fixtures["cases"])
    now = datetime.fromisoformat(fixtures["frozen_clock"])
    for case in fixtures["cases"]:
        for signal in case["inputs"]:
            apply_stage2_signal(world, case, signal, now)
    assert asdict(world) == asdict(load())
    assert fixtures == FIXTURES
