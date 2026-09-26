"""Eligibility uses only current trusted records and the exact rendered payload."""

from copy import deepcopy

import pytest

from app.agents.company_os_compliance import (
    ComplianceContentError,
    evaluate_outbound_eligibility,
    evidence_hash,
    render_message,
)
from app.services.company_os_sandbox_dispatch import compliance_context
from tests.unit.company_os_stage2_facts import MESSAGE, SENDER, TEMPLATE, eligible_world

REGISTRY = {"integrations": {"mail": {
    "desired_use": ["acquisition_email"], "sandbox_sender": SENDER,
}}}


def context():
    world = eligible_world()
    payload = render_message(MESSAGE, TEMPLATE, SENDER, world.contacts["p-1"])
    return compliance_context(world, "p-1", {
        "integration": {"name": "mail", "use": "acquisition_email", "message": MESSAGE},
    }, REGISTRY, payload)


def test_complete_email_passes_without_mutating_evidence():
    original = context()
    before = deepcopy(original)
    result = evaluate_outbound_eligibility(original)
    assert result.eligible and result.evidence["failed_rule_ids"] == []
    assert original == before
    assert result.evidence["approvable"] is False
    assert next(r for r in result.evidence["evidence_hashes"]
                if r["record_type"] == "sender")["sha256"] == evidence_hash(SENDER)


@pytest.mark.parametrize("path,value,rule", [
    (("rendered_payload", "subject"), "You won a prize", "EMAIL-01"),
    (("rendered_payload", "body"), "Altered body", "EMAIL-01"),
    (("rendered_payload", "from_address"), "other@example.test", "EMAIL-01"),
    (("rendered_payload", "to_address"), "other@example.test", "EMAIL-01"),
    (("rendered_payload", "reply_to"), "other@example.test", "EMAIL-01"),
    (("rendered_payload", "sending_domain"), "other.test", "EMAIL-01"),
    (("rendered_payload", "postal_address"), None, "EMAIL-02"),
    (("rendered_payload", "ad_disclosure"), None, "EMAIL-02"),
    (("template", "opt_out_route", "valid_days"), 29, "EMAIL-03"),
    (("template", "opt_out_route", "fee_required"), True, "EMAIL-03"),
    (("template", "opt_out_route", "extra_data_required"), True, "EMAIL-03"),
    (("template", "opt_out_route", "covers_all_marketing"), False, "EMAIL-03"),
    (("template", "opt_out_route", "verified_at"), None, "EMAIL-03"),
    (("template", "revoked_at"), "2026-09-25T00:00:00Z", "EMAIL-01"),
    (("template", "subject_accuracy_verified"), {}, "EMAIL-01"),
    (("suppression", "suppressed"), True, "EMAIL-04"),
    (("sender", "dispatch_method"), "unknown", "BASE-01"),
    (("consent", "revoked_at"), "2026-09-25T00:00:00Z", "BASE-01"),
    (("consent", "seller"), "other", "BASE-01"),
    (("contact", "captured_at"), "2099-01-01T00:00:00Z", "BASE-01"),
    (("contact", "recipient_time_zone"), "not-a-zone", "BASE-01"),
])
def test_invalid_content_and_evidence_fail_closed(path, value, rule):
    c = context()
    target = c
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    result = evaluate_outbound_eligibility(c)
    assert not result.eligible and rule in result.evidence["failed_rule_ids"]
    assert result.evidence["proposed_remedy"]


@pytest.mark.parametrize("record,field", [
    ("sender", "sender_id"), ("sender", "from_address"), ("sender", "reply_to"),
    ("sender", "sending_domain"), ("sender", "postal_address"), ("sender", "seller_name"),
    ("sender", "dispatch_method"), ("sender", "captured_at"),
    ("contact", "recipient_id"), ("contact", "address"), ("contact", "recipient_location"),
    ("contact", "recipient_time_zone"), ("contact", "captured_at"),
    ("contact", "consent_verified"), ("consent", "seller"), ("consent", "channel"),
    ("consent", "purpose"), ("consent", "captured_at"), ("consent", "revoked_at"),
    ("suppression", "version"), ("suppression", "checked_at"), ("suppression", "suppressed"),
])
def test_each_missing_trusted_fact_fails_base(record, field):
    c = context()
    del c[record][field]
    result = evaluate_outbound_eligibility(c)
    assert not result.eligible and "BASE-01" in result.evidence["failed_rule_ids"]


@pytest.mark.parametrize("field", [
    "sender", "contact", "consent", "suppression", "template", "rendered_payload",
    "channel", "purpose", "policy_sha256", "policy_version", "evaluated_at", "recipient_id",
])
def test_missing_record_or_scope_is_unknown(field):
    c = context()
    del c[field]
    result = evaluate_outbound_eligibility(c)
    assert not result.eligible and "BASE-01" in result.evidence["failed_rule_ids"]


@pytest.mark.parametrize("channel", ["sms", "voice", "voicemail", "unknown", None])
def test_non_email_scope_is_never_allowed(channel):
    c = context()
    c["channel"] = channel
    result = evaluate_outbound_eligibility(c)
    assert not result.eligible and result.evidence["failed_rule_ids"] == ["BASE-01"]


@pytest.mark.parametrize("fills", [{}, {"name": "Pat", "extra": "value"}, {"name": 12}])
def test_template_fills_must_match_declared_placeholders(fills):
    with pytest.raises(ComplianceContentError):
        render_message({"template_id": "approved", "fills": fills}, TEMPLATE, SENDER, {})


def test_template_format_expressions_cannot_read_attributes():
    template = deepcopy(TEMPLATE)
    template["body_template"] = "{name.__class__}"
    with pytest.raises(ComplianceContentError):
        render_message(MESSAGE, template, SENDER, {})


@pytest.mark.parametrize("field,value", [
    ("recipient_id", "different"), ("contact_email", "different@example.test"),
    ("method", "manual"), ("recipient_id", None), ("contact_email", None), ("method", None),
])
def test_consent_must_match_recipient_address_and_method(field, value):
    c = context()
    if value is None:
        del c["consent"][field]
    else:
        c["consent"][field] = value
    result = evaluate_outbound_eligibility(c)
    assert not result.eligible
    assert result.evidence["failed_rule_ids"] == ["BASE-01"]


def test_consent_address_comparison_is_normalized():
    c = context()
    c["consent"]["contact_email"] = "  P-1@EXAMPLE.TEST  "
    assert evaluate_outbound_eligibility(c).eligible


@pytest.mark.parametrize("record,field,value", [
    ("contact", "recipient_time_zone", 5),
    ("contact", "recipient_time_zone", "Not/AZone"),
    ("contact", "recipient_time_zone", "america/new_york"),
    ("contact", "recipient_time_zone", "posixrules"),
    ("suppression", "checked_at", "2026-09-25 00:00:00+00:00"),
    ("contact", "recipient_location", {"country": "US", "state": ""}),
    ("contact", "recipient_location", "Florida"),
    ("sender", "dispatch_method", ""),
    ("sender", "dispatch_method", []),
    ("consent", "method", []),
    ("sender", "sender_id", 7),
    ("suppression", "version", ""),
    ("suppression", "checked_at", "yesterday"),
    (None, "evaluated_at", 12),
    (None, "policy_sha256", "not-a-hash"),
])
def test_malformed_facts_still_produce_a_schema_valid_block(record, field, value):
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads(Path("tests/fixtures/company_os/acqivo/schemas/sandbox_event.schema.json").read_text())
    ctx = context()
    (ctx if record is None else ctx[record])[field] = value
    result = evaluate_outbound_eligibility(ctx)
    assert not result.eligible and "BASE-01" in result.evidence["failed_rule_ids"]
    Draft202012Validator(schema["properties"]["compliance"], format_checker=FormatChecker()).validate(result.evidence)
