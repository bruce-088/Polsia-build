"""Deterministic validation for canonical Company OS workflow transitions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


class CompanyOSWorkflowError(ValueError):
    """Raised when a workflow definition or proposed transition is invalid."""


@dataclass(frozen=True)
class WorkflowTransition:
    workflow_id: str
    from_state: str
    to_state: str
    action: str
    risk_level: str
    requirements: tuple[str, ...]
    integration: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["requirements"] = list(self.requirements)
        return payload


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise CompanyOSWorkflowError(f"{field} must be a list of non-empty strings")
    return value


def validate_workflow_definition(workflow: dict[str, Any]) -> None:
    """Reject malformed or ambiguous workflow definitions before use."""
    if not isinstance(workflow, dict):
        raise CompanyOSWorkflowError("workflow must be an object")
    workflow_id = workflow.get("id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise CompanyOSWorkflowError("workflow.id must be a non-empty string")

    states_list = _string_list(workflow.get("states"), "workflow.states")
    if len(states_list) != len(set(states_list)):
        raise CompanyOSWorkflowError("workflow.states contains duplicates")
    states = set(states_list)
    terminal_states = set(
        _string_list(workflow.get("terminal_states", []), "workflow.terminal_states")
    )
    unknown_terminals = terminal_states - states
    if unknown_terminals:
        raise CompanyOSWorkflowError(
            f"terminal states are not registered: {sorted(unknown_terminals)}"
        )

    transitions = workflow.get("transitions")
    if not isinstance(transitions, list):
        raise CompanyOSWorkflowError("workflow.transitions must be a list")
    seen: set[tuple[str, str, str]] = set()
    for index, transition in enumerate(transitions):
        if not isinstance(transition, dict):
            raise CompanyOSWorkflowError(f"transition {index} must be an object")
        source = transition.get("from")
        target = transition.get("to")
        action = transition.get("action")
        risk = transition.get("risk_level")
        if source not in states or target not in states:
            raise CompanyOSWorkflowError(
                f"transition {index} references an unregistered state"
            )
        if source in terminal_states:
            raise CompanyOSWorkflowError(
                f"terminal state {source!r} cannot have outgoing transitions"
            )
        if not isinstance(action, str) or not action:
            raise CompanyOSWorkflowError(
                f"transition {index} action must be a non-empty string"
            )
        if risk not in {"GREEN", "YELLOW", "RED"}:
            raise CompanyOSWorkflowError(
                f"transition {index} risk_level must be GREEN, YELLOW, or RED"
            )
        key = (source, action, target)
        if key in seen:
            raise CompanyOSWorkflowError(f"duplicate transition: {key}")
        seen.add(key)
        _string_list(transition.get("requirements", []), f"transition {index} requirements")
        integration = transition.get("integration")
        if integration is not None and (not isinstance(integration, str) or not integration):
            raise CompanyOSWorkflowError(
                f"transition {index} integration must be a non-empty string"
            )


def require_valid_transition(
    workflow: dict[str, Any],
    *,
    current_state: str,
    action: str,
    proposed_state: str,
    proposed_risk: str | None = None,
) -> WorkflowTransition:
    """Return the canonical transition or reject the proposal without repair."""
    validate_workflow_definition(workflow)
    states = set(workflow["states"])
    if current_state not in states:
        raise CompanyOSWorkflowError(f"current state is not registered: {current_state!r}")
    if proposed_state not in states:
        raise CompanyOSWorkflowError(f"proposed state is not registered: {proposed_state!r}")
    if current_state in set(workflow.get("terminal_states", [])):
        raise CompanyOSWorkflowError(f"terminal state cannot transition: {current_state!r}")

    matches = [
        transition
        for transition in workflow["transitions"]
        if transition["from"] == current_state
        and transition["action"] == action
        and transition["to"] == proposed_state
    ]
    if not matches:
        raise CompanyOSWorkflowError(
            "transition is not registered: "
            f"{current_state!r} --{action!r}--> {proposed_state!r}"
        )
    transition = matches[0]
    canonical_risk = transition["risk_level"]
    if proposed_risk is not None and proposed_risk != canonical_risk:
        raise CompanyOSWorkflowError(
            f"transition risk mismatch: expected {canonical_risk}, got {proposed_risk}"
        )
    return WorkflowTransition(
        workflow_id=workflow["id"],
        from_state=current_state,
        to_state=proposed_state,
        action=action,
        risk_level=canonical_risk,
        requirements=tuple(transition.get("requirements", [])),
        integration=transition.get("integration"),
    )
