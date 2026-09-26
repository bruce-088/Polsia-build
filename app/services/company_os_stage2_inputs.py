"""Load canonical synthetic inputs without manufacturing missing evidence.

The caller supplies the run identity and pinned compliance-policy identity.
All original cases are retained, including scripted founder choices and retries;
loading them does not authorize or execute workflow transitions.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from app.services.company_os_synthetic_adapters import (
    SyntheticCapabilityError,
    SyntheticWorld,
    normalize_address,
)


class Stage2InputError(ValueError):
    """Canonical inputs cannot be mapped without missing or conflicting facts."""


def _timestamp(value: Any, now: datetime) -> bool:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
        return parsed is not None and parsed.tzinfo is not None and parsed <= now
    except ValueError:
        return False


def _consent_verified(contact: dict, consent: dict, sender: dict, now: datetime) -> bool:
    """Derive eligibility exclusively from matching, current consent evidence."""
    return bool(
        consent and consent.get("recipient_id") == contact["recipient_id"]
        and normalize_address(consent.get("contact_email")) == normalize_address(contact["address"])
        and normalize_address(contact.get("contact_email")) == normalize_address(contact["address"])
        and consent.get("method") in {"manual", "automated"}
        and consent["method"] == sender.get("dispatch_method")
        and consent.get("seller") == sender.get("seller_name")
        and consent.get("channel") == "email" and consent.get("purpose") == "commercial"
        and "revoked_at" in consent and consent["revoked_at"] is None
        and _timestamp(consent.get("captured_at"), now)
    )


def build_stage2_snapshot(
    fixtures: dict[str, Any], registry: dict[str, Any], templates: list[dict[str, Any]],
    *, run_id: str, compliance_policy: dict[str, str],
) -> SyntheticWorld:
    """Return a run world from canonical snapshots, leaving every input untouched.

    Missing consent and suppression facts remain missing (negative fixtures).
    Required contact/sender timestamps and queue bindings must be explicit.
    Queue adapter keys stay unknown until dispatch derives them from DB identity.
    """
    try:
        now = datetime.fromisoformat(fixtures["frozen_clock"])
        if now.tzinfo is None:
            raise Stage2InputError("frozen_clock must include a timezone")
        sender = registry["integrations"]["sendgrid"]["sandbox_sender"]
        if not _timestamp(sender.get("captured_at"), now):
            raise Stage2InputError("sender captured_at is missing or invalid")
        world = SyntheticWorld(run_id, now)
        world.compliance_policy = deepcopy(compliance_policy)
        for template in templates:
            template_id = template["template_id"]
            if template_id in world.templates:
                raise Stage2InputError("duplicate template identity")
            world.templates[template_id] = deepcopy(template)
        for case in fixtures["cases"]:
            if case["id"] in world.input_cases:
                raise Stage2InputError("duplicate case identity")
            world.input_cases[case["id"]] = deepcopy(case)
            contact = deepcopy(case["contact_record"])
            recipient = contact["recipient_id"]
            if not all(isinstance(contact.get(k), str) and contact[k] for k in
                       ("recipient_id", "address", "contact_email")) or not _timestamp(contact.get("captured_at"), now):
                raise Stage2InputError("contact identity, address or captured_at is missing or invalid")
            if recipient in world.contacts:
                raise Stage2InputError("duplicate recipient snapshot")
            consent = deepcopy(case.get("consent_record") or {})
            contact["consent_verified"] = _consent_verified(contact, consent, sender, now)
            world.set_contact(recipient, contact)
            world.consents[recipient] = consent
            world.suppression_snapshots[recipient] = deepcopy(case.get("suppression_snapshot") or {})
        return world
    except (KeyError, TypeError, SyntheticCapabilityError) as exc:
        raise Stage2InputError(f"invalid canonical Stage 2 input: {exc}") from exc


def apply_stage2_signal(
    world: SyntheticWorld, case: dict[str, Any], signal: dict[str, Any], now: datetime,
) -> None:
    """Apply one arriving signal using the canonical loader's existing rules.

    ``now`` is the caller's frozen clock; signal application uses the world's
    frozen clock exactly as the eager loader did.
    """
    try:
        channel, facts = signal["type"], deepcopy(signal["facts"])
        if channel == "failure_injection":
            identity = tuple(facts[k] for k in ("workflow_id", "entity_id", "action"))
            if (any(not isinstance(v, str) or not v for v in identity)
                    or type(facts.get("after_effect")) is not bool
                    or identity in world.action_failures):
                raise Stage2InputError("invalid or duplicate action failure injection")
            world.action_failures[identity] = facts["after_effect"]
        elif channel in {"authority", "lead", "reply", "mail", "payment", "opt_out", "contact", "queued_message"}:
            # The enclosing case is an explicit identity source for unqualified signals.
            facts.setdefault("entity_id", case["entity_id"])
            if channel == "queued_message" and (
                facts.get("entity_id") != case["entity_id"] or facts.get("workflow_id") != case["workflow_id"]
                or any(not isinstance(facts.get(k), str) or not facts[k] for k in
                       ("origin_workflow_id", "origin_entity_id", "original_action"))
            ):
                raise Stage2InputError("queue origin and consumer bindings are required")
            world.receive(channel, signal["delivery_id"], facts)
            if channel == "lead":
                recipient = facts["entity_id"]
                world.leads[recipient]["eligible"] = (
                    world.contacts.get(recipient, {}).get("consent_verified") is True
                    and not world.is_suppressed(recipient)
                )
        elif channel not in {"prospect", "estimate", "lead_batch", "founder_resolution", "retry"}:
            raise Stage2InputError(f"unsupported canonical input type: {channel}")
        # Non-provider facts remain verbatim in input_cases for the decision driver.
    except (KeyError, TypeError, SyntheticCapabilityError) as exc:
        raise Stage2InputError(f"invalid canonical Stage 2 input: {exc}") from exc


def load_stage2_inputs(
    fixtures: dict[str, Any], registry: dict[str, Any], templates: list[dict[str, Any]],
    *, run_id: str, compliance_policy: dict[str, str],
) -> SyntheticWorld:
    """Preserve eager loading: build every snapshot, then apply signals in file order."""
    world = build_stage2_snapshot(
        fixtures, registry, templates, run_id=run_id, compliance_policy=compliance_policy,
    )
    try:
        now = datetime.fromisoformat(fixtures["frozen_clock"])
        for case in fixtures["cases"]:
            for signal in case["inputs"]:
                apply_stage2_signal(world, case, signal, now)
    except (KeyError, TypeError, SyntheticCapabilityError) as exc:
        raise Stage2InputError(f"invalid canonical Stage 2 input: {exc}") from exc
    return world
