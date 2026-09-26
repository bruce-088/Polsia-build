"""Pure Stage 2 BASE-01 and EMAIL-01..04 evaluation of trusted snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from string import Formatter
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

EMAIL_USES = {"acquisition_email", "transactional_email"}
REASONS = {
    "BASE-01": "evidence_missing_or_scope_unknown",
    "EMAIL-01": "email_header_or_subject_invalid",
    "EMAIL-02": "email_address_or_ad_disclosure_missing",
    "EMAIL-03": "email_optout_mechanism_invalid",
    "EMAIL-04": "email_optout_or_suppression_match",
}


class ComplianceContentError(ValueError):
    """A message cannot be rendered solely from approved template fields."""


@dataclass(frozen=True)
class ComplianceResult:
    eligible: bool
    evidence: dict[str, Any]


def evidence_hash(record: Any) -> str:
    return hashlib.sha256(json.dumps(
        record, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def registered_channel(config: dict[str, Any], requested_use: str | None) -> str | None:
    """Only an explicitly registered email use establishes email scope."""
    if isinstance(requested_use, str) and requested_use in config.get("desired_use", []) and requested_use in EMAIL_USES:
        return "email"
    return None


def is_email_integration(request: Any, registry: dict[str, Any]) -> bool:
    """True for any request naming an integration registered for an email use, in any phase."""
    if not isinstance(request, dict) or not isinstance(request.get("name"), str):
        return False
    config = registry["integrations"].get(request["name"], {})
    return any(use in EMAIL_USES for use in config.get("desired_use", []))


def is_email_execute(request: Any, registry: dict[str, Any]) -> bool:
    """Classify by registered capability, never let a requested use bypass the gate."""
    return is_email_integration(request, registry) and request.get("phase") == "execute"


def render_message(message: dict, template: dict, sender: dict, contact: dict) -> dict:
    """Render literal named placeholders, never format expressions or headers."""
    if not isinstance(sender, dict) or not isinstance(contact, dict):
        raise ComplianceContentError("sender and contact records must be objects")
    if not isinstance(message, dict) or set(message) != {"template_id", "fills"}:
        raise ComplianceContentError("template_id and fills are required")
    if not isinstance(template, dict) or message["template_id"] != template.get("template_id"):
        raise ComplianceContentError("unknown template")
    fills = message["fills"]
    placeholders = template.get("placeholders")
    if (not isinstance(fills, dict) or not isinstance(placeholders, list)
            or any(not isinstance(p, str) or not p.isidentifier() for p in placeholders)
            or set(fills) != set(placeholders)
            or any(not isinstance(v, str) for v in fills.values())):
        raise ComplianceContentError("fills must exactly match declared placeholders")
    body = template.get("body_template")
    if not isinstance(body, str):
        raise ComplianceContentError("template body missing")
    try:
        parts = list(Formatter().parse(body))
    except ValueError as exc:
        raise ComplianceContentError("invalid template placeholders") from exc
    if (any(name is not None and (name not in placeholders or spec or conversion)
            for _, name, spec, conversion in parts)
            or {name for _, name, _, _ in parts if name is not None} != set(placeholders)):
        raise ComplianceContentError("template contains undeclared or expressive placeholders")
    rendered_body = "".join(literal + (fills[name] if name is not None else "")
                            for literal, name, _, _ in parts)
    return {
        "template_id": message["template_id"], "fills": deepcopy(fills),
        "from_address": sender.get("from_address"), "to_address": contact.get("address"),
        "reply_to": sender.get("reply_to"), "sending_domain": sender.get("sending_domain"),
        "subject": template.get("subject"), "body": rendered_body,
        "ad_disclosure": template.get("ad_disclosure"),
        "postal_address": sender.get("postal_address"),
        "opt_out_route": deepcopy(template.get("opt_out_route")),
    }


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _enum(value: Any, options: frozenset[str]) -> str | None:
    """A malformed (non-string, e.g. list) fact must fail the predicate, never raise."""
    return value if isinstance(value, str) and value in options else None


def _record(value: Any) -> dict[str, Any]:
    """A nested trusted-record fact must be a mapping; any other shape is malformed."""
    return value if isinstance(value, dict) else {}


def _dispatch_method(value: Any) -> str | None:
    """A dispatch/consent method fact must be a known string; any other shape is malformed."""
    return _enum(value, frozenset({"manual", "automated"}))


RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:[0-5]\d:[0-5]\d(\.\d+)?(Z|[+-]\d{2}:[0-5]\d)")
CANONICAL_ZONES = frozenset(available_timezones())


def _time(value: Any) -> datetime | None:
    try:
        parsed = (datetime.fromisoformat(value)
                  if isinstance(value, str) and RFC3339.fullmatch(value) else None)
        return parsed if parsed is not None and parsed.tzinfo is not None else None
    except ValueError:
        return None


def evaluate_outbound_eligibility(context: dict[str, Any]) -> ComplianceResult:
    """Evaluate without mutating input, reading a clock, or consulting services."""
    c = deepcopy(context)
    sender, contact = _record(c.get("sender")), _record(c.get("contact"))
    consent, suppression = _record(c.get("consent")), _record(c.get("suppression"))
    template, payload = _record(c.get("template")), _record(c.get("rendered_payload"))
    now = _time(c.get("evaluated_at"))
    zone = contact.get("recipient_time_zone")
    try:
        zone_info = ZoneInfo(zone) if isinstance(zone, str) and zone in CANONICAL_ZONES else None
    except (ZoneInfoNotFoundError, ValueError):
        zone_info = None
    zone = zone if zone_info is not None else None
    local_time = now.astimezone(zone_info).isoformat() if now and zone_info else None
    location = contact.get("recipient_location") or {}
    location = location if isinstance(location, dict) else {}
    def timestamp(value):
        parsed = _time(value)
        return now is not None and parsed is not None and parsed <= now

    consent_matches = (
        _text(c.get("recipient_id")) and consent.get("recipient_id") == c.get("recipient_id")
        and _text(consent.get("contact_email")) and _text(contact.get("address"))
        and consent["contact_email"].strip().casefold() == contact["address"].strip().casefold()
        and _dispatch_method(consent.get("method")) is not None
        and _dispatch_method(consent.get("method")) == _dispatch_method(sender.get("dispatch_method"))
    )
    suppressed = suppression.get("suppressed") is not False
    trusted = (
        c.get("channel") == "email" and c.get("purpose") == "commercial"
        and _dispatch_method(sender.get("dispatch_method")) is not None
        and all(_text(sender.get(k)) for k in (
            "sender_id", "from_address", "reply_to", "sending_domain", "postal_address", "seller_name"))
        and timestamp(sender.get("captured_at")) and timestamp(contact.get("captured_at"))
        and _text(c.get("recipient_id")) and contact.get("recipient_id") == c.get("recipient_id")
        and _text(contact.get("address")) and "@" in contact["address"]
        and all(_text(location.get(k)) for k in ("country", "state")) and local_time is not None
        and consent_matches and consent.get("seller") == sender.get("seller_name")
        and consent.get("channel") == "email" and consent.get("purpose") == "commercial"
        and timestamp(consent.get("captured_at")) and "revoked_at" in consent
        and consent["revoked_at"] is None and contact.get("consent_verified") is True
        and timestamp(suppression.get("checked_at")) and _text(suppression.get("version"))
        and not suppressed and bool(payload) and c.get("scope_valid", True) is True
        and c.get("policy_version") == "0.1.0"
        and isinstance(c.get("policy_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", c["policy_sha256"]) is not None
    )
    checks = {"BASE-01": bool(trusted)}
    if c.get("channel") == "email":
        try:
            expected = render_message(
                {"template_id": payload.get("template_id"), "fills": payload.get("fills")},
                template, sender, contact,
            )
        except ComplianceContentError:
            expected = None
        verified = _record(template.get("subject_accuracy_verified"))
        approved = (timestamp(template.get("approved_at")) and "revoked_at" in template
                    and template["revoked_at"] is None and _text(verified.get("by"))
                    and timestamp(verified.get("at")))
        checks["EMAIL-01"] = bool(
            expected is not None and payload == expected and approved
            and _text(payload.get("subject"))
            and all(_text(sender.get(k)) for k in ("from_address", "reply_to", "sending_domain"))
            and sender["from_address"].rsplit("@", 1)[-1].lower() == sender["sending_domain"].lower()
        )
        body = payload.get("body") if isinstance(payload.get("body"), str) else ""
        checks["EMAIL-02"] = bool(
            _text(sender.get("postal_address")) and sender["postal_address"] in body
            and payload.get("postal_address") == sender["postal_address"]
            and _text(template.get("ad_disclosure")) and template["ad_disclosure"] in body
            and payload.get("ad_disclosure") == template["ad_disclosure"]
        )
        route = _record(template.get("opt_out_route"))
        days = route.get("valid_days")
        route_type = _enum(route.get("type"), frozenset({"reply", "web"}))
        checks["EMAIL-03"] = bool(
            route_type is not None and _text(route.get("target"))
            and route["target"] in body and payload.get("opt_out_route") == route
            and type(days) is int and days >= 30 and route.get("covers_all_marketing") is True
            and route.get("fee_required") is False and route.get("extra_data_required") is False
            and timestamp(route.get("verified_at"))
            and (route_type != "reply" or route["target"] == sender.get("reply_to"))
        )
        checks["EMAIL-04"] = not suppressed

    failed = [rule for rule, passed in checks.items() if not passed]
    records = {
        "sender": sender, "recipient": contact, "consent": consent,
        "suppression": suppression, "template": template, "rendered_payload": payload,
    }
    # Evidence must always be recordable: malformed facts become null, and BASE-01 already fails for them.
    def text_or_none(value):
        return value if _text(value) else None

    def time_or_none(value):
        return value if _time(value) is not None else None

    sha = c.get("policy_sha256")
    evidence = {
        "policy_version": text_or_none(c.get("policy_version")),
        "policy_sha256": sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-fA-F]{64}", sha) else None,
        "channel": text_or_none(c.get("channel")), "method": text_or_none(sender.get("dispatch_method")),
        "purpose": text_or_none(c.get("purpose")), "recipient_ref": text_or_none(c.get("recipient_id")),
        "sender_ref": text_or_none(sender.get("sender_id")),
        "suppression_version": text_or_none(suppression.get("version")),
        "jurisdiction": {k: text_or_none(location.get(k)) if isinstance(location, dict) else None
                         for k in ("country", "state")},
        "recipient_time_zone": zone, "recipient_local_time": local_time,
        "evaluated_at": time_or_none(c.get("evaluated_at")),
        "suppression_checked_at": time_or_none(suppression.get("checked_at")),
        "evidence_hashes": [
            {"record_type": kind, "record_id": kind + ":" + str(c.get("recipient_id")),
             "sha256": evidence_hash(record) if record else None}
            for kind, record in records.items()
        ],
        "rule_results": [
            {"rule_id": rule, "passed": passed, "reason_code": None if passed else REASONS[rule]}
            for rule, passed in checks.items()
        ],
        "failed_rule_ids": failed, "reason_codes": [REASONS[rule] for rule in failed],
        "proposed_remedy": "Provide verified current evidence and an approved template; honor all opt-outs.",
        "approvable": False,
    }
    # Unknown records must always be represented by a BASE-01 failure.
    if any(not record for record in records.values()) and "BASE-01" not in failed:
        evidence["failed_rule_ids"].insert(0, "BASE-01")
        evidence["reason_codes"].insert(0, REASONS["BASE-01"])
        evidence["rule_results"][0].update(passed=False, reason_code=REASONS["BASE-01"])
    return ComplianceResult(not evidence["failed_rule_ids"], evidence)
