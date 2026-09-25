"""Deterministic, in-memory Stage 2 integrations; never construct live clients.

The harness owns one world per sandbox run. These adapters emit synthetic
receipts; the governed coordinator remains responsible for policy, approvals,
workflow transitions, and the immutable event log.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.agents.company_os_compliance import ComplianceContentError, render_message
from app.config import settings


class SyntheticCapabilityError(ValueError):
    """A synthetic operation lacks evidence or cannot execute safely."""


class SyntheticComplianceBlock(SyntheticCapabilityError):
    """Defense-in-depth refusal; never an integration failure."""


@dataclass
class SyntheticWorld:
    """Run-scoped synthetic entities and immutable evidence references."""

    run_id: str
    frozen_at: datetime
    contacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    customers: dict[str, dict[str, Any]] = field(default_factory=dict)
    leads: dict[str, dict[str, Any]] = field(default_factory=dict)
    payments: dict[str, dict[str, Any]] = field(default_factory=dict)
    replies: dict[str, dict[str, Any]] = field(default_factory=dict)
    mail: dict[str, dict[str, Any]] = field(default_factory=dict)
    signals: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    suppressed_recipients: set[str] = field(default_factory=set)
    pending_opt_outs: dict[str, dict[str, Any]] = field(default_factory=dict)
    queue: dict[str, dict[str, Any]] = field(default_factory=dict)
    dispatches: dict[str, dict[str, Any]] = field(default_factory=dict)
    consents: dict[str, dict[str, Any]] = field(default_factory=dict)
    suppression_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    compliance_policy: dict[str, str] = field(default_factory=dict)
    review_blocked_recipients: set[str] = field(default_factory=set)
    templates: dict[str, dict[str, Any]] = field(default_factory=dict)
    outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)
    _receipts: dict[tuple[str, str, str], tuple[str, str]] = field(default_factory=dict)
    _inbound: dict[tuple[str, str], str] = field(default_factory=dict)
    _fail_once: set[tuple[str, str]] = field(default_factory=set)
    _after_effect_once: set[tuple[str, str]] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not self.run_id or self.frozen_at.tzinfo is None:
            raise SyntheticCapabilityError("run ID and timezone-aware frozen clock required")

    def _guard(self) -> None:
        if not settings.sandbox_mode:
            raise SyntheticCapabilityError("sandbox mode required")

    def _ref(self, category: str, key: str) -> str:
        digest = hashlib.sha256(f"{self.run_id}:{category}:{key}".encode()).hexdigest()[:24]
        return f"sandbox://{self.run_id}/{category}/{digest}"

    def ingest_opt_out(self, facts: dict[str, Any]) -> None:
        """Retain every available identity, including unresolved STOP signals."""
        self._guard()
        identities = self._identities(facts)
        contact = self.contacts.get(facts.get("entity_id")) if isinstance(facts.get("entity_id"), str) else None
        addresses = {normalize_address(facts.get(key)) for key in ("address", "number", "phone")}
        if contact:
            addresses.add(normalize_address(contact.get("address")))
            contact["suppressed"] = True
        self.suppressed_recipients.update(addresses - {""})
        record = deepcopy(facts)
        record["captured_at"] = self.frozen_at.isoformat()
        # Retaining identity links also protects contacts whose address changes.
        for identity in identities or {"unresolved:" + _fingerprint(facts)}:
            self.pending_opt_outs.setdefault(identity, record)

    @staticmethod
    def _identities(facts: dict[str, Any]) -> set[str]:
        result = set()
        if isinstance(facts.get("entity_id"), str) and facts["entity_id"]:
            result.add("entity:" + facts["entity_id"])
        for key in ("address", "number", "phone"):
            value = normalize_address(facts.get(key))
            if value:
                result.add("address:" + value)
        return result

    def set_contact(self, entity_id: str, facts: dict[str, Any]) -> None:
        self._guard()
        was_suppressed = self.is_suppressed(entity_id)
        self.contacts[entity_id] = deepcopy(facts)
        if was_suppressed or self.is_suppressed(entity_id):
            self.contacts[entity_id]["suppressed"] = True
            address = normalize_address(facts.get("address"))
            if address:
                self.suppressed_recipients.add(address)

    def is_suppressed(self, recipient_id: str) -> bool:
        contact = self.contacts.get(recipient_id, {})
        identities = self._identities({**contact, "entity_id": recipient_id})
        return (contact.get("suppressed") is True or contact.get("flagged") is True
                or bool(identities & self.pending_opt_outs.keys())
                or any(identity.removeprefix("address:") in self.suppressed_recipients
                       for identity in identities if identity.startswith("address:")))

    def dispatch_id(self, message_key: str) -> str:
        return _fingerprint({"run_id": self.run_id, "message_key": message_key})

    def prepare_dispatch(
        self, *, message_key: str, recipient_id: str, adapter_kind: str,
        adapter_idempotency_key: str, origin_workflow_instance_id: int,
        origin_state_before: str, origin_workflow_version: int,
        frozen_native_decision: dict[str, Any], rendered_payload: dict[str, Any],
        consumers: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        """Snapshot execution evidence; callers validate each consumer's decision.

        Database events, never this ledger, determine consumer completion.
        """
        self._guard()
        dispatch_id = self.dispatch_id(message_key)
        record = deepcopy({
            "dispatch_id": dispatch_id, "run_id": self.run_id, "message_key": message_key,
            "recipient_id": recipient_id, "adapter_kind": adapter_kind,
            "adapter_idempotency_key": adapter_idempotency_key,
            "origin_workflow_instance_id": origin_workflow_instance_id,
            "origin_state_before": origin_state_before,
            "origin_workflow_version": origin_workflow_version,
            "frozen_native_decision": frozen_native_decision,
            "rendered_payload": rendered_payload, "payload_sha256": _fingerprint(rendered_payload),
            "status": "prepared", "receipt": None, "consumers": consumers,
        })
        previous = self.dispatches.get(dispatch_id)
        if previous is not None:
            immutable = set(record) - {"status", "receipt", "consumers"}
            if any(previous[key] != record[key] for key in immutable):
                raise SyntheticCapabilityError("dispatch message identity reused with different evidence")
            return previous
        self.dispatches[dispatch_id] = record
        return record

    def receive(self, channel: str, delivery_id: str, facts: dict[str, Any]) -> str:
        """Deduplicate deliveries while preserving append-only signal history."""
        self._guard()
        if channel not in {"reply", "mail", "opt_out", "lead", "payment", "authority", "contact", "queued_message"}:
            raise SyntheticCapabilityError("unknown inbound channel")
        if not delivery_id or not isinstance(facts, dict):
            raise SyntheticCapabilityError("inbound ID and facts required")
        fingerprint = _fingerprint(facts)
        key = (channel, delivery_id)
        if key in self._inbound:
            if self._inbound[key] != fingerprint:
                raise SyntheticCapabilityError("inbound delivery ID reused with different facts")
            return self._ref("input", f"{channel}:{delivery_id}")
        entity_id = facts.get("entity_id")
        opt_out = channel == "opt_out" or (channel in {"reply", "mail"} and (
            facts.get("opt_out") is True or facts.get("unsubscribe") is True
            or facts.get("unsubscribe_marker") is True
            or str(facts.get("body", "")).strip().lower() in {"stop", "unsubscribe"}))
        if not isinstance(entity_id, str) or not entity_id:
            if not opt_out:
                raise SyntheticCapabilityError("inbound entity ID required")
            entity_id = normalize_address(facts.get("address") or facts.get("number") or facts.get("phone"))
        if channel == "queued_message":
            if (not all(isinstance(facts.get(k), str) and facts[k] for k in
                        ("item_id", "recipient_id", "original_action", "original_adapter_key"))
                    or facts["recipient_id"] not in self.contacts):
                raise SyntheticCapabilityError("evidence_missing_or_scope_unknown")
            if entity_id in self.queue or any(item["item_id"] == facts["item_id"] for item in self.queue.values()):
                raise SyntheticCapabilityError("queue item already bound")
            self.queue[entity_id] = {k: deepcopy(facts[k]) for k in
                                     ("item_id", "recipient_id", "original_action", "original_adapter_key")}
            self.queue[entity_id].update(status="queued", receipt=None, captured_at=self.frozen_at.isoformat())
        elif channel == "contact":
            self.set_contact(entity_id, facts)
        elif channel == "authority":
            self.customers[entity_id] = deepcopy(facts)
        elif channel == "lead":
            self.leads[entity_id] = deepcopy(facts)
        elif channel == "payment":
            self.payments[entity_id] = deepcopy(facts)
        elif channel == "mail":
            self.mail[entity_id] = deepcopy(facts)
        elif channel == "reply":
            self.replies[delivery_id] = deepcopy(facts)
        if opt_out:
            self.ingest_opt_out(facts)
        if channel in {"lead", "payment", "reply", "mail"}:
            self.signals.setdefault(entity_id, []).append({
                "channel": channel, "delivery_id": delivery_id, "facts": deepcopy(facts),
                "captured_at": self.frozen_at.isoformat(),
            })
        self._inbound[key] = fingerprint
        return self._ref("input", f"{channel}:{delivery_id}")

    def inject_failure(self, adapter: str, idempotency_key: str, *, after_effect: bool = False) -> None:
        """Fail one execution; after_effect simulates a lost receipt/timeout."""
        self._guard()
        target = (adapter, idempotency_key)
        (self._after_effect_once if after_effect else self._fail_once).add(target)


class SyntheticAdapter:
    """Concrete Company OS adapter bound to a synthetic run.

    Replaying a key returns its first receipt, even if database persistence of
    the corresponding coordinator event failed after the synthetic effect.
    """

    KINDS = {"mail", "calendar", "authority", "lead", "payment"}

    def __init__(self, world: SyntheticWorld, kind: str):
        if kind not in self.KINDS:
            raise SyntheticCapabilityError("registered synthetic kind required")
        self.world, self.kind = world, kind

    async def execute(self, decision: dict[str, Any], *, idempotency_key: str, run_id: str, recipient_id: str) -> str:
        self.world._guard()
        if not idempotency_key or not isinstance(decision, dict):
            raise SyntheticCapabilityError("native decision and adapter idempotency key required")
        if run_id != self.world.run_id:
            raise SyntheticCapabilityError("adapter run differs from world run")
        if not isinstance(recipient_id, str) or not recipient_id:
            raise SyntheticCapabilityError("evidence_missing_or_scope_unknown")
        signature = _fingerprint({"recipient": recipient_id, "payload": decision.get("rendered_payload", decision)})
        slot = (self.kind, idempotency_key)
        previous = self.world._receipts.get((self.kind, recipient_id, idempotency_key))
        if previous is not None:
            if previous[0] != signature:
                raise SyntheticCapabilityError("adapter key reused for different native decision")
            return previous[1]
        if slot in self.world._fail_once:
            self.world._fail_once.remove(slot)
            raise SyntheticCapabilityError("injected synthetic provider failure")

        if self.kind == "mail":
            contact = self.world.contacts.get(recipient_id)
            if contact is None or contact.get("consent_verified") is not True or self.world.is_suppressed(recipient_id):
                raise SyntheticComplianceBlock("synthetic contact ineligible or opted out")
            payload = decision.get("rendered_payload")
            if payload is not None:
                template = self.world.templates.get(payload.get("template_id"), {})
                sender = {k: payload.get(k) for k in
                          ("from_address", "reply_to", "sending_domain", "postal_address")}
                try:
                    rendered = render_message(
                        {"template_id": payload.get("template_id"), "fills": payload.get("fills")},
                        template, sender, contact,
                    )
                except ComplianceContentError as exc:
                    raise SyntheticComplianceBlock("template is invalid") from exc
                if (not template.get("approved_at") or template.get("revoked_at") is not None
                        or not template.get("subject_accuracy_verified") or rendered != payload):
                    raise SyntheticComplianceBlock("template is revoked or changed")
        elif self.kind == "authority":
            customer = self.world.customers.get(recipient_id)
            if customer is None or customer.get("authority_verified") is not True:
                raise SyntheticCapabilityError("customer authority missing")
        elif self.kind == "lead":
            lead = self.world.leads.get(recipient_id)
            if lead is None or lead.get("eligible") is not True:
                raise SyntheticCapabilityError("lead eligibility missing")
        elif self.kind == "payment":
            payment = self.world.payments.get(recipient_id)
            if payment is None or payment.get("status") not in {"paid", "failed"}:
                raise SyntheticCapabilityError("payment signal missing")
        elif self.kind == "calendar":
            if not any(reply.get("entity_id") == recipient_id and reply.get("meeting_confirmed") is True
                       for reply in self.world.replies.values()):
                raise SyntheticCapabilityError("meeting has no authoritative confirmation")

        receipt = self.world._ref(self.kind, f"{recipient_id}:{idempotency_key}")
        self.world._receipts[(self.kind, recipient_id, idempotency_key)] = (signature, receipt)
        self.world.outcomes[receipt] = {
            "kind": self.kind, "entity_id": recipient_id, "recipient_id": recipient_id,
            "rendered_payload": deepcopy(decision.get("rendered_payload", decision)),
            "action": decision.get("action"), "occurred_at": self.world.frozen_at.isoformat(),
            "external_side_effect": False,
        }
        for record in self.world.dispatches.values():
            if (record["adapter_kind"] == self.kind and record["recipient_id"] == recipient_id
                    and record["adapter_idempotency_key"] == idempotency_key):
                record.update(status="executed", receipt=receipt)
                for item in self.world.queue.values():
                    if item["item_id"] == record["message_key"]:
                        item.update(status="sent", receipt=receipt, original_adapter_key=idempotency_key)
        if slot in self.world._after_effect_once:
            self.world._after_effect_once.remove(slot)
            raise SyntheticCapabilityError("injected receipt timeout after synthetic effect")
        return receipt


def _fingerprint(value: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SyntheticCapabilityError("synthetic facts must be JSON compatible") from exc
    return hashlib.sha256(encoded.encode()).hexdigest()


def normalize_address(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""
