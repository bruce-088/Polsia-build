"""Fail-closed capability checks for Company OS integration states."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

Phase = Literal["draft", "propose", "read", "execute"]
MODES = {"disabled", "documented", "sandbox", "read_only", "write_limited", "live"}
READ_CAPABLE_MODES = {"sandbox", "read_only", "write_limited", "live"}
WRITE_CAPABLE_MODES = {"sandbox", "write_limited", "live"}


class CompanyOSIntegrationError(ValueError):
    """Raised when an integration registry or requested capability is invalid."""


@dataclass(frozen=True)
class IntegrationDecision:
    integration: str
    phase: Phase
    mode: str
    allowed: bool
    external_write: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_integration_registry(registry: dict[str, Any]) -> None:
    """Validate registry shape without treating credentials as capability."""
    if not isinstance(registry, dict):
        raise CompanyOSIntegrationError("integration registry must be an object")
    declared_modes = registry.get("modes")
    if not isinstance(declared_modes, list) or set(declared_modes) != MODES:
        raise CompanyOSIntegrationError("registry.modes must declare the canonical modes")
    integrations = registry.get("integrations")
    if not isinstance(integrations, dict):
        raise CompanyOSIntegrationError("registry.integrations must be an object")
    for name, config in integrations.items():
        if not isinstance(name, str) or not name or not isinstance(config, dict):
            raise CompanyOSIntegrationError("each integration must have a named object config")
        if config.get("mode") not in MODES:
            raise CompanyOSIntegrationError(f"{name}.mode is not canonical")
        if not isinstance(config.get("verified"), bool):
            raise CompanyOSIntegrationError(f"{name}.verified must be a boolean")
        if not isinstance(config.get("external_writes"), bool):
            raise CompanyOSIntegrationError(f"{name}.external_writes must be a boolean")
        desired_use = config.get("desired_use")
        if not isinstance(desired_use, list) or any(
            not isinstance(item, str) or not item for item in desired_use
        ):
            raise CompanyOSIntegrationError(
                f"{name}.desired_use must be a list of non-empty strings"
            )
        scope = config.get("scope")
        if scope is not None and (not isinstance(scope, str) or not scope):
            raise CompanyOSIntegrationError(f"{name}.scope must be a non-empty string")


def evaluate_integration_capability(
    registry: dict[str, Any],
    *,
    integration: str,
    phase: Phase,
    requested_use: str | None = None,
    requested_scope: str | None = None,
) -> IntegrationDecision:
    """Evaluate draft/propose/read/execute as distinct capabilities."""
    validate_integration_registry(registry)
    if phase not in {"draft", "propose", "read", "execute"}:
        raise CompanyOSIntegrationError(f"unknown integration phase: {phase!r}")
    config = registry["integrations"].get(integration)
    if config is None:
        return IntegrationDecision(
            integration, phase, "unregistered", False, False, "integration is not registered"
        )
    mode = config["mode"]

    if requested_use is not None and requested_use not in config["desired_use"]:
        return IntegrationDecision(
            integration, phase, mode, False, False, "requested use is not registered"
        )
    configured_scope = config.get("scope")
    if configured_scope is not None and requested_scope != configured_scope:
        return IntegrationDecision(
            integration, phase, mode, False, False, "requested scope is not authorized"
        )

    if phase in {"draft", "propose"}:
        return IntegrationDecision(
            integration,
            phase,
            mode,
            True,
            False,
            "offline preparation does not claim integration access",
        )
    if not config["verified"]:
        return IntegrationDecision(
            integration, phase, mode, False, False, "integration is not verified"
        )
    if phase == "read":
        allowed = mode in READ_CAPABLE_MODES
        return IntegrationDecision(
            integration,
            phase,
            mode,
            allowed,
            False,
            "read capability verified" if allowed else "integration mode does not permit reads",
        )

    allowed = mode in WRITE_CAPABLE_MODES and config["external_writes"] is True
    return IntegrationDecision(
        integration,
        phase,
        mode,
        allowed,
        allowed,
        "write capability verified"
        if allowed
        else "integration mode or registry capability does not permit writes",
    )


def require_integration_capability(
    registry: dict[str, Any],
    *,
    integration: str,
    phase: Phase,
    requested_use: str | None = None,
    requested_scope: str | None = None,
) -> IntegrationDecision:
    """Fail closed unless the requested integration capability is allowed."""
    decision = evaluate_integration_capability(
        registry,
        integration=integration,
        phase=phase,
        requested_use=requested_use,
        requested_scope=requested_scope,
    )
    if not decision.allowed:
        raise CompanyOSIntegrationError(
            f"integration capability denied: {decision.integration} "
            f"{decision.phase}: {decision.reason}"
        )
    return decision
