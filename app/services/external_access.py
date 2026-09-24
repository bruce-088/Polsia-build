"""Shared fail-closed boundary for production external mutations."""

from app.config import settings


class ExternalAccessBlocked(RuntimeError):
    """Raised before a production mutation client is constructed in sandbox mode."""


def require_production_write_allowed(capability: str) -> None:
    """Block every production external write while sandbox mode is enabled.

    Stage 2 uses separate registered synthetic adapters. Production services
    must never reinterpret sandbox mode as permission to target a test address,
    account, repository, or credential set.
    """
    if settings.sandbox_mode:
        raise ExternalAccessBlocked(
            f"production external write blocked in sandbox mode: {capability}"
        )
