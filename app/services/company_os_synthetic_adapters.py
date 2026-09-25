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

from app.config import settings


class SyntheticCapabilityError(ValueError):
    """A synthetic operation lacks evidence or cannot execute safely."""


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
    outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)
    _receipts: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
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

    def receive(self, channel: str, delivery_id: str, facts: dict[str, Any]) -> str:
        """Deduplicate an inbound delivery, retaining the original evidence."""
        self._guard()
        if channel not in {"reply", "opt_out", "lead", "payment", "authority"}:
            raise SyntheticCapabilityError("unknown inbound channel")
        if not delivery_id or not isinstance(facts, dict):
            raise SyntheticCapabilityError("inbound ID and facts required")
        key = (channel, delivery_id)
        if key in self._inbound:
            if self._inbound[key] != _fingerprint(facts):
                raise SyntheticCapabilityError("inbound delivery ID reused with different facts")
            return self._ref("input", f"{channel}:{delivery_id}")
        entity_id = facts.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            raise SyntheticCapabilityError("inbound entity ID required")
        if channel in {"reply", "opt_out"} and entity_id not in self.contacts:
            raise SyntheticCapabilityError("unknown contact")
        if channel == "authority":
            self.customers[entity_id] = deepcopy(facts)
        elif channel == "lead":
            self.leads[entity_id] = deepcopy(facts)
        elif channel == "payment":
            self.payments[entity_id] = deepcopy(facts)
        else:
            self.replies[delivery_id] = deepcopy(facts)
            if channel == "opt_out" or facts.get("opt_out") is True:
                self.contacts[entity_id]["suppressed"] = True
        self._inbound[key] = _fingerprint(facts)
        return self._ref("input", f"{channel}:{delivery_id}")

    def inject_failure(self, adapter: str, idempotency_key: str, *, after_effect: bool = False) -> None:
        """Fail one execution; after_effect simulates a lost receipt/timeout."""
        self._guard()
        target = (adapter, idempotency_key)
        (self._after_effect_once if after_effect else self._fail_once).add(target)


class SyntheticAdapter:
    """Concrete Company OS adapter bound to a synthetic run and entity.

    Replaying a key returns its first receipt, even if database persistence of
    the corresponding coordinator event failed after the synthetic effect.
    """

    KINDS = {"mail", "calendar", "authority", "lead", "payment"}

    def __init__(self, world: SyntheticWorld, kind: str, entity_id: str):
        if kind not in self.KINDS or not entity_id:
            raise SyntheticCapabilityError("registered synthetic kind and entity required")
        self.world, self.kind, self.entity_id = world, kind, entity_id

    async def execute(self, decision: dict[str, Any], *, idempotency_key: str) -> str:
        self.world._guard()
        if not idempotency_key or not isinstance(decision, dict):
            raise SyntheticCapabilityError("native decision and adapter idempotency key required")
        signature = _fingerprint({"entity": self.entity_id, "decision": decision})
        slot = (self.kind, idempotency_key)
        previous = self.world._receipts.get(slot)
        if previous is not None:
            if previous[0] != signature:
                raise SyntheticCapabilityError("adapter key reused for different native decision")
            return previous[1]
        if slot in self.world._fail_once:
            self.world._fail_once.remove(slot)
            raise SyntheticCapabilityError("injected synthetic provider failure")

        if self.kind == "mail":
            contact = self.world.contacts.get(self.entity_id)
            if contact is None or contact.get("consent_verified") is not True or contact.get("suppressed") is True:
                raise SyntheticCapabilityError("synthetic contact ineligible or opted out")
        elif self.kind == "authority":
            customer = self.world.customers.get(self.entity_id)
            if customer is None or customer.get("authority_verified") is not True:
                raise SyntheticCapabilityError("customer authority missing")
        elif self.kind == "lead":
            lead = self.world.leads.get(self.entity_id)
            if lead is None or lead.get("eligible") is not True:
                raise SyntheticCapabilityError("lead eligibility missing")
        elif self.kind == "payment":
            payment = self.world.payments.get(self.entity_id)
            if payment is None or payment.get("status") not in {"paid", "failed"}:
                raise SyntheticCapabilityError("payment signal missing")
        elif self.kind == "calendar":
            if not any(reply.get("entity_id") == self.entity_id and reply.get("meeting_confirmed") is True
                       for reply in self.world.replies.values()):
                raise SyntheticCapabilityError("meeting has no authoritative confirmation")

        receipt = self.world._ref(self.kind, idempotency_key)
        self.world._receipts[slot] = (signature, receipt)
        self.world.outcomes[receipt] = {
            "kind": self.kind, "entity_id": self.entity_id,
            "action": decision.get("action"), "occurred_at": self.world.frozen_at.isoformat(),
            "external_side_effect": False,
        }
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
