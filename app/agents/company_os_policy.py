"""Fail-closed Company OS policy evaluation for proposed actions.

The evaluator is deliberately pure: it authorizes, escalates, or blocks a
structured intent but never executes an action or rewrites an agent response.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

Disposition = Literal["allow", "requires_founder", "block"]
RiskLevel = Literal["GREEN", "YELLOW", "RED"]
WRITE_CAPABLE_MODES = {"sandbox", "write_limited", "live"}


class CompanyOSPolicyError(ValueError):
    """Raised when policy or intent is incomplete and cannot be evaluated."""


@dataclass(frozen=True)
class PolicyDecision:
    disposition: Disposition
    effective_risk: RiskLevel
    founder_approval_required: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        return payload


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise CompanyOSPolicyError(f"{field} must be numeric")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CompanyOSPolicyError(f"{field} must be numeric") from exc


def _string_set(policy: dict[str, Any], field: str) -> set[str]:
    value = policy.get(field)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CompanyOSPolicyError(f"policy.{field} must be a list of strings")
    return set(value)


def evaluate_action_policy(intent: dict[str, Any], policy: dict[str, Any]) -> PolicyDecision:
    """Evaluate one complete requested action without executing or repairing it.

    ``requested_total`` is the full action size. A caller may not submit only a
    smaller first step to evade a configured bound.
    """
    if not isinstance(intent, dict) or not isinstance(policy, dict):
        raise CompanyOSPolicyError("intent and policy must be objects")

    action_type = intent.get("action_type")
    requested_risk = intent.get("risk_level")
    external_write = intent.get("external_write")
    if not isinstance(action_type, str) or not action_type:
        raise CompanyOSPolicyError("intent.action_type must be a non-empty string")
    if requested_risk not in {"GREEN", "YELLOW", "RED"}:
        raise CompanyOSPolicyError("intent.risk_level must be GREEN, YELLOW, or RED")
    if not isinstance(external_write, bool):
        raise CompanyOSPolicyError("intent.external_write must be a boolean")

    hard_denies = _string_set(policy, "hard_denies")
    non_approvable = _string_set(policy, "non_approvable_blocks")
    red_actions = _string_set(policy, "red_action_types")
    flags = intent.get("policy_flags", [])
    if not isinstance(flags, list) or any(not isinstance(item, str) for item in flags):
        raise CompanyOSPolicyError("intent.policy_flags must be a list of strings")
    matched_blocks = sorted({action_type, *flags} & (hard_denies | non_approvable))
    effective_risk: RiskLevel = "RED" if action_type in red_actions else requested_risk

    if matched_blocks:
        return PolicyDecision(
            "block",
            effective_risk,
            False,
            tuple(f"non-approvable policy block: {item}" for item in matched_blocks),
        )

    reasons: list[str] = []
    requires_founder = effective_risk == "RED"
    if requires_founder:
        reasons.append("RED action requires founder approval")

    limit_name = intent.get("limit_name")
    if limit_name is not None:
        limits = policy.get("limits")
        if not isinstance(limits, dict) or limit_name not in limits:
            requires_founder = True
            reasons.append(f"bound cannot be verified: {limit_name}")
        elif "requested_total" not in intent:
            requires_founder = True
            reasons.append("full requested action total is missing")
        else:
            requested_total = _decimal(intent["requested_total"], "intent.requested_total")
            limit = _decimal(limits[limit_name], f"policy.limits.{limit_name}")
            if requested_total > limit:
                requires_founder = True
                reasons.append(
                    f"full requested action exceeds {limit_name}: {requested_total} > {limit}"
                )

    if external_write:
        integration_mode = intent.get("integration_mode")
        if integration_mode not in WRITE_CAPABLE_MODES:
            return PolicyDecision(
                "block",
                effective_risk,
                False,
                (f"integration mode does not permit writes: {integration_mode!r}",),
            )
        if intent.get("requires_authority") and intent.get("authority_verified") is not True:
            return PolicyDecision("block", effective_risk, False, ("explicit authority is unverified",))
        if intent.get("requires_consent") and intent.get("consent_verified") is not True:
            return PolicyDecision("block", effective_risk, False, ("consent or eligibility is unverified",))

    if requires_founder and intent.get("founder_approval_status") != "approved":
        return PolicyDecision("requires_founder", effective_risk, True, tuple(reasons))
    if requires_founder:
        reasons.append("founder approval verified")
    return PolicyDecision("allow", effective_risk, requires_founder, tuple(reasons))


def require_action_allowed(intent: dict[str, Any], policy: dict[str, Any]) -> PolicyDecision:
    """Fail closed at an execution boundary unless the policy returns allow."""
    decision = evaluate_action_policy(intent, policy)
    if decision.disposition != "allow":
        raise CompanyOSPolicyError(
            f"action is not authorized: {decision.disposition}: {', '.join(decision.reasons)}"
        )
    return decision
