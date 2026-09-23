"""Strict native decision contract for Company OS Stage 1 runs."""

from __future__ import annotations

import json
from typing import Any

CONTRACT_NAME = "company_os_stage1"
REQUIRED_FIELDS = {
    "scenario_id", "action", "risk_level", "state", "founder_approval",
    "handoff_to", "actions_taken", "actions_proposed", "assumptions",
}
OPTIONAL_FIELDS = {"notes"}
RISK_LEVELS = {"GREEN", "YELLOW", "RED"}


def stage1_json_schema(
    scenario_id: str,
    *,
    handoff_owners: list[str] | None = None,
    workflow_states: list[str] | None = None,
) -> dict[str, Any]:
    """Return the provider schema for the existing strict Stage 1 envelope."""
    string_list = {"type": "array", "items": {"type": "string"}}
    properties: dict[str, Any] = {
        "scenario_id": {"type": "string", "const": scenario_id},
        "action": {"type": "string", "minLength": 1},
        "risk_level": {"type": "string", "enum": sorted(RISK_LEVELS)},
        "state": {"type": "string", "minLength": 1},
        "founder_approval": {"type": "boolean"},
        "handoff_to": {"type": "string", "minLength": 1},
        "actions_taken": string_list,
        "actions_proposed": string_list,
        "assumptions": string_list,
        "notes": {"type": "string"},
    }
    if handoff_owners:
        properties["handoff_to"]["enum"] = handoff_owners
    if workflow_states:
        properties["state"]["enum"] = workflow_states
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(REQUIRED_FIELDS),
        "additionalProperties": False,
    }


class CompanyOSContractError(ValueError):
    """Raised when an agent does not natively satisfy the decision contract."""

    def __init__(self, message: str, *, raw_output: str | None = None) -> None:
        super().__init__(message)
        self.raw_output = raw_output


def _require_string(value: Any, field: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise CompanyOSContractError(f"{field} must be a non-empty string")


def _require_string_list(value: Any, field: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CompanyOSContractError(f"{field} must be a list of strings")


def validate_stage1_decision(
    payload: Any,
    scenario_id: str,
    *,
    handoff_owners: list[str] | None = None,
    workflow_states: list[str] | None = None,
) -> dict[str, Any]:
    """Validate without filling, translating, normalizing, or dropping fields."""
    if not isinstance(payload, dict):
        raise CompanyOSContractError("Stage 1 output must be a JSON object")
    missing = REQUIRED_FIELDS - payload.keys()
    if missing:
        raise CompanyOSContractError(f"missing required fields: {sorted(missing)}")
    unexpected = payload.keys() - REQUIRED_FIELDS - OPTIONAL_FIELDS
    if unexpected:
        raise CompanyOSContractError(f"unexpected fields: {sorted(unexpected)}")
    if payload["scenario_id"] != scenario_id:
        raise CompanyOSContractError(
            f"scenario_id mismatch: expected {scenario_id!r}, got {payload['scenario_id']!r}"
        )
    _require_string(payload["scenario_id"], "scenario_id")
    _require_string(payload["action"], "action")
    _require_string(payload["state"], "state")
    _require_string(payload["handoff_to"], "handoff_to")
    if handoff_owners and payload["handoff_to"] not in handoff_owners:
        raise CompanyOSContractError("handoff_to is not a canonical Company OS owner")
    if workflow_states and payload["state"] not in workflow_states:
        raise CompanyOSContractError("state is not a canonical Company OS workflow state")
    if payload["risk_level"] not in RISK_LEVELS:
        raise CompanyOSContractError("risk_level must be GREEN, YELLOW, or RED")
    if not isinstance(payload["founder_approval"], bool):
        raise CompanyOSContractError("founder_approval must be a boolean")
    for field in ("actions_taken", "actions_proposed", "assumptions"):
        _require_string_list(payload[field], field)
    if "notes" in payload:
        _require_string(payload["notes"], "notes", allow_empty=True)
    return payload


def parse_stage1_decision(raw: str, scenario_id: str) -> dict[str, Any]:
    """Require one clean JSON object; prose/fences are contract failures."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise CompanyOSContractError(
            "Stage 1 output must be clean JSON", raw_output=raw
        ) from exc
    try:
        return validate_stage1_decision(payload, scenario_id)
    except CompanyOSContractError as exc:
        exc.raw_output = raw
        raise
