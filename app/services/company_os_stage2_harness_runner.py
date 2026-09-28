"""Bounded Stage 2 orchestration over the unmodified governed services.

Only persisted event rows are exported. The caller owns the transaction and
must commit each yielded pair before publishing it (the CLI does this). Control
facts, corrections and future inputs never enter the decision-provider context.
See docs/STAGE2_HARNESS_INTERFACE.md for the driver-facing contract.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.company_os_compliance import EMAIL_USES, is_email_execute
from app.agents.company_os_integration import evaluate_integration_capability
from app.agents.company_os_stage2_provider import ScriptedProviderTimeout
from app.config import settings
from app.models.company_os_sandbox import CompanyOSSandboxApproval, CompanyOSSandboxEvent
from app.services.company_os_sandbox_approval_service import (
    EXECUTABLE,
    STATUSES,
    SandboxApprovalError,
    resolve_sandbox_approval,
    resume_sandbox_approval,
)
from app.services.company_os_sandbox_coordinator import (
    DecisionProvider,
    SandboxCoordinatorError,
    coordinate_sandbox_action,
)
from app.services.company_os_sandbox_dispatch import AGGREGATE_WORKFLOWS, PERSON_WORKFLOWS
from app.services.company_os_sandbox_service import (
    create_sandbox_run,
    create_workflow_instance,
    export_sandbox_event,
)
from app.services.company_os_stage2_inputs import apply_stage2_signal, build_stage2_snapshot
from app.services.company_os_synthetic_adapters import SyntheticAdapter, SyntheticWorld

MAX_DECISIONS_PER_CASE = 25
MAX_RETRIES_PER_CASE = 3
# The one real, explicit retry-policy limit integration_failure_recovery's own
# "safe retry limit reached" requirement is grounded in -- deliberately the
# same bound as the driver's own retry-command budget (MAX_RETRIES_PER_CASE)
# rather than a second, disconnected magic number (P13-REV2-01).
SAFE_RETRY_LIMIT = MAX_RETRIES_PER_CASE
# customer_onboarding's "integration registry verified" binds each of the
# customer's own approved_message_channels to the registry integrations that
# actually serve that channel's use, never to any unrelated verified entry
# (P13-REV2-02). Only "email" has a real, established use-vocabulary mapping
# (EMAIL_USES, company_os_compliance.py) anywhere in this codebase.
CHANNEL_INTEGRATION_USES = {"email": EMAIL_USES}
CLASSIFICATIONS = {
    "prospect_to_meeting": "acquisition", "customer_onboarding": "customer_onboarding",
    "missed_inquiry_recovery": "missed_inquiry_recovery", "estimate_followup": "estimate_followup",
    "stale_lead_reactivation": "stale_lead_reactivation", "integration_failure_recovery": "failure_recovery",
}
# Outcomes the closing report never lists as a failure: "terminal" is success;
# compliance/policy blocks and a declared service rejection are each the
# deliberately-scored endpoint of their own coverage bucket, not a defect.
# Every other non-terminal final outcome (waiting/defect/collision/etc.) gets a
# failures entry unless a more specific one (budget, unconsumed signal/command)
# already covers it. See docs/STAGE2_HARNESS_INTERFACE.md.
NEVER_A_FAILURE = {"terminal", "compliance_blocked", "policy_blocked", "service_rejection"}


def _finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _approved_followup_time(facts: dict, world: SyntheticWorld) -> bool:
    value = facts.get("customer_approved_followup_at")
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value) == world.frozen_at
    except ValueError:
        return False


def _origin_dispatch(facts: dict, world: SyntheticWorld) -> dict | None:
    """Find the persistent dispatch-ledger record a recovery signal's own
    origin_workflow_id/origin_entity_id/original_action facts describe.

    Unlike world.action_failures (popped the instant a failure is armed --
    company_os_synthetic_adapters.py:236-243 -- so it is gone again by the
    time a recovery case's own transitions are evaluated), the ledger record
    itself is never removed. It is the only durable, genuinely-available
    evidence of "a failure happened here and here is its current state" by
    the time integration_failure_recovery's own requirements are checked.
    """
    matches = [record for record in world.dispatches.values()
               if record.get("origin_workflow_id") == facts.get("origin_workflow_id")
               and record.get("origin_entity_id") == facts.get("origin_entity_id")
               and record.get("frozen_native_decision", {}).get("action") == facts.get("original_action")]
    item = world.queue.get(facts.get("entity_id")) if isinstance(facts.get("entity_id"), str) else None
    if item:
        dispatch_id = item.get("dispatch_id") or world.dispatch_id(item["item_id"])
        return next((record for record in matches if record.get("dispatch_id") == dispatch_id), None)
    # Without a bound queue item, ambiguity must fail closed, not select a
    # convenient first record and borrow its failure or health evidence.
    return matches[0] if len(matches) == 1 else None


async def _origin_failure_count(db: AsyncSession, record: dict | None) -> int:
    """Count the recovery case's own originating dispatch's genuinely
    persisted failure_detected events -- the only real, accumulating,
    queryable evidence of how many times the provider has actually failed on
    this attempt (P13-REV2-01).

    SyntheticWorld's dispatch-ledger status is set once by prepare_dispatch
    and stores no retry count or policy at all; a genuine provider failure
    (adapter.execute raising SyntheticCapabilityError) never advances it past
    "prepared" -- company_os_sandbox_dispatch.py's execute_dispatch only ever
    moves status to "blocked" on a compliance/eligibility refusal, never on
    the adapter's own failure. So dispatch-ledger status alone can never
    distinguish "never attempted" from "failed five times," which is exactly
    what let one prepared record satisfy every retry-count-shaped requirement
    at once. Each real failure any legitimate consumer's own coordinator call
    persists is a separate, immutable failure_detected event row.

    Scoped by dispatch_id alone (P13-REV3-01) -- never by workflow_instance_id
    or action. execute_dispatch registers recovery instances as consumers of
    the SAME dispatch record, and their own coordinator/approval calls persist
    failures under the recovery instance's id and its own consumer_decision
    action, not the origin's. dispatch_id itself is already a run-scoped
    fingerprint (SyntheticWorld.dispatch_id: fingerprint(run_id, message_key)),
    so matching on it alone is exact -- restricting to origin_workflow_instance_id
    silently ignored every recovery-path failure on the same shared dispatch.
    """
    if record is None or not record.get("dispatch_id"):
        return 0
    events = await db.scalars(select(CompanyOSSandboxEvent).where(
        CompanyOSSandboxEvent.event_type == "failure_detected",
    ))
    return sum(
        1 for event in events
        if event.payload.get("metadata", {}).get("gate") in {"integration", "approval_resume"}
        and event.payload.get("metadata", {}).get("dispatch_attempted") is True
        and event.payload.get("metadata", {}).get("dispatch_id") == record.get("dispatch_id")
    )


async def _post_failure_provider_health(db: AsyncSession, record: dict | None) -> bool:
    """A later successful sandbox adapter attempt on the same provider is the
    only available operational health observation. Static registry verification
    alone cannot prove recovery after this dispatch failed.
    """
    if not record or not record.get("dispatch_id"):
        return False
    provider = ((record.get("frozen_native_decision") or {}).get("integration") or {}).get("name")
    if not provider:
        return False
    # Scoped by dispatch_id alone, matching _origin_failure_count above
    # (P13-REV3-01) -- a recovery-instance failure on the same dispatch must
    # count too, and a later recovery-instance failure after a successful
    # origin-instance health observation must still leave health unverified.
    all_events = await db.scalars(select(CompanyOSSandboxEvent).where(
        CompanyOSSandboxEvent.event_type == "failure_detected",
    ))
    failures = [event for event in all_events
                if event.payload.get("metadata", {}).get("gate") in {"integration", "approval_resume"}
                and event.payload.get("metadata", {}).get("dispatch_attempted") is True
                and event.payload.get("metadata", {}).get("dispatch_id") == record["dispatch_id"]]
    if not failures:
        return False
    latest = max(failures, key=lambda event: event.sequence)
    later = await db.scalars(select(CompanyOSSandboxEvent).where(
        CompanyOSSandboxEvent.sandbox_run_id == latest.sandbox_run_id,
        CompanyOSSandboxEvent.sequence > latest.sequence,
        CompanyOSSandboxEvent.result == "executed_in_sandbox",
    ))
    return any(
        (event.payload.get("integration") or {}).get("name") == provider
        and (event.payload.get("integration") or {}).get("target") == "sandbox"
        and event.payload.get("metadata", {}).get("dispatch_attempted") is True
        for event in later
    )


def _execute_ready(registry: dict, *, integration: str, requested_use: str | None = None,
                    requested_scope: str | None = None) -> bool:
    """Stage 2 execute-readiness, matching the real governed callers exactly --
    never `evaluate_integration_capability(...).allowed` alone.

    `.allowed` alone is `evaluate_integration_capability`'s own broader gate:
    its WRITE_CAPABLE_MODES = {"sandbox", "write_limited", "live"} permits
    execute in any of the three. But the actual governed Stage 2 callers --
    `coordinate_sandbox_action` and `resume_sandbox_approval` -- additionally
    require `mode == "sandbox"` specifically for phase in {"read", "execute"}
    (and explicitly deny `mode == "live"` outright), so an integration set to
    `live` or `write_limited` still passed `.allowed` but would be rejected by
    the real governed path (P13-REV4-01).

    `requested_scope` must also be supplied -- `evaluate_integration_capability`
    rejects any integration with a configured non-null `scope` whenever
    `requested_scope` is `None`, so omitting it silently fails every real,
    correctly-scoped integration too (P13-REV4-02). The caller must derive
    `requested_scope` from trusted customer/workflow facts, never from the
    registry's own configured scope (that would trivially always match).
    """
    decision = evaluate_integration_capability(
        registry, integration=integration, phase="execute",
        requested_use=requested_use, requested_scope=requested_scope,
    )
    return decision.allowed and decision.mode == "sandbox"


# Explicit, per-string predicates for genuinely derivable transition requirements.
# A predicate reads facts already ingested plus world/registry state -- never a
# fixture fact literally named after the requirement string it attests (that
# would be self-fulfilling; validate_pack bans it). Requirement strings with no
# entry here fail pack-build loudly instead of silently passing. Covers every
# requirement string declared anywhere in the four in-scope canonical workflow
# definitions (prospect_to_meeting, customer_onboarding, missed_inquiry_recovery,
# estimate_followup, stale_lead_reactivation, integration_failure_recovery --
# tests/fixtures/company_os/acqivo/workflows/*.json),
# not just the ones a particular dev fixture happens to reach (P13-REV-01).
# The 5th positional argument, origin_failures, is the recovery case's own
# originating dispatch's genuinely-persisted failure_detected count (see
# _origin_failure_count) -- 0 whenever there is no such origin (most
# predicates below never use it, but every predicate accepts it so the
# caller (evidence_context) can invoke all of them uniformly).
REQUIREMENT_RULES: dict[str, Callable[[dict, SyntheticWorld, dict, dict, int], bool]] = {
    "public evidence preserved": lambda facts, world, case, registry, origin_failures: (
        isinstance(facts.get("public_source"), str) and bool(facts["public_source"])),
    "score and confidence recorded": lambda facts, world, case, registry, origin_failures: (
        _finite_number(facts.get("icp_score")) and facts["icp_score"] >= 7
        and _finite_number(facts.get("confidence"))),
    "score below qualification threshold": lambda facts, world, case, registry, origin_failures: (
        _finite_number(facts.get("icp_score")) and facts["icp_score"] < 7),
    "score and reason recorded": lambda facts, world, case, registry, origin_failures: (
        _finite_number(facts.get("icp_score")) and isinstance(facts.get("disqualification_reason"), str)
        and bool(facts["disqualification_reason"])),
    "personalization supported by evidence": lambda facts, world, case, registry, origin_failures: (
        isinstance(facts.get("personalization_evidence"), str) and bool(facts["personalization_evidence"])),
    "first live batch or unapproved sequence": lambda facts, world, case, registry, origin_failures: (
        facts.get("first_live_batch") is True or facts.get("unapproved_sequence") is True),
    # Both bounds independently verified against their own usage/limit evidence
    # (P13-REV-02) -- previously only the daily-send pair was checked, so a
    # case with an exhausted follow-up allowance verified anyway.
    "within daily send and follow-up limits": lambda facts, world, case, registry, origin_failures: (
        _finite_number(facts.get("daily_sends_used")) and _finite_number(facts.get("daily_send_limit"))
        and facts["daily_sends_used"] < facts["daily_send_limit"]
        and _finite_number(facts.get("follow_ups_sent")) and _finite_number(facts.get("follow_up_limit"))
        and facts["follow_ups_sent"] < facts["follow_up_limit"]),
    # Bound to the registry's own real sandbox sender identity (P13-REV-02) --
    # previously any nonempty fixture-supplied string satisfied this with no
    # check that the sender/identity was actually approved by real evidence.
    "sending identity approved": lambda facts, world, case, registry, origin_failures: (
        isinstance(facts.get("approved_sequence_id"), str) and bool(facts["approved_sequence_id"])
        and registry.get("integrations", {}).get("sendgrid", {}).get("verified") is True
        and facts["approved_sequence_id"] == (
            registry.get("integrations", {}).get("sendgrid", {}).get("sandbox_sender", {}).get("sender_id"))),
    "integration mode permits write": lambda facts, world, case, registry, origin_failures: any(
        cfg.get("external_writes") is True for cfg in registry.get("integrations", {}).values()),
    "opt-out handling enabled": lambda facts, world, case, registry, origin_failures: any(
        isinstance((world.templates.get(tid) or {}).get("opt_out_route"), dict)
        and world.templates[tid]["opt_out_route"].get("covers_all_marketing") is True
        for tid in case.get("available_template_ids", [])),
    "authoritative calendar or human handoff": lambda facts, world, case, registry, origin_failures: (
        facts.get("meeting_confirmed") is True and str(facts.get("confirmation_ref", "")).startswith("sandbox://")),
    # customer_onboarding (required_customer_facts: service_area, business_hours,
    # workflow_permissions, approved_message_channels, escalation_contacts --
    # the same "authority" channel evidence company_os_stage2_inputs.py's own
    # authority_verified derivation reads).
    "first production activation requires founder approval": lambda facts, world, case, registry, origin_failures: (
        facts.get("authority_verified") is True or facts.get("customer_authority_verified") is True),
    "scope approved": lambda facts, world, case, registry, origin_failures: (
        isinstance(facts.get("service_area"), str) and bool(facts["service_area"])
        and isinstance(facts.get("business_hours"), str) and bool(facts["business_hours"])
        and isinstance(facts.get("approved_message_channels"), list) and bool(facts["approved_message_channels"])),
    "customer permissions recorded": lambda facts, world, case, registry, origin_failures: (
        isinstance(facts.get("workflow_permissions"), list) and bool(facts["workflow_permissions"])
        and isinstance(facts.get("escalation_contacts"), list) and bool(facts["escalation_contacts"])),
    # Bound to the customer's own required connections (P13-REV2-02),
    # verified through the real capability evaluator rather than a hand-rolled
    # verified+external_writes check that omitted mode (P13-REV3-02), and
    # through _execute_ready (P13-REV4-01/02) rather than `.allowed` alone --
    # `.allowed` permits live/write_limited execute too (WRITE_CAPABLE_MODES),
    # but the real governed callers require mode == "sandbox" specifically;
    # `.allowed` alone also silently fails any integration with a configured
    # scope because no requested_scope was ever supplied. requested_scope is
    # the case's own trusted customer_id fact -- the same customer identity
    # "approved customer workflow"/"customer-approved timing and channel"
    # below key their own customer lookups on -- never the registry's own
    # configured scope copied back at it (which would trivially always
    # match and prove nothing).
    # Every channel the customer's own approved_message_channels facts
    # declare must be served by a specific execute-capable registry
    # integration for that channel's use; an unrelated verified entry (e.g.
    # a verified read-only imap connection) can never satisfy an email
    # requirement, and an unrecognized channel with no established
    # use-vocabulary mapping (CHANNEL_INTEGRATION_USES) never verifies.
    "integration registry verified": lambda facts, world, case, registry, origin_failures: (
        isinstance(facts.get("approved_message_channels"), list) and bool(facts["approved_message_channels"])
        and all(
            channel in CHANNEL_INTEGRATION_USES and any(
                (use := next(iter(set(cfg.get("desired_use", [])) & CHANNEL_INTEGRATION_USES[channel]), None)) is not None
                and _execute_ready(registry, integration=name, requested_use=use,
                                   requested_scope=facts.get("customer_id"))
                for name, cfg in registry.get("integrations", {}).items())
            for channel in facts["approved_message_channels"])),
    # missed_inquiry_recovery / integration_failure_recovery: the customer's own
    # recorded authority must cover this specific workflow, and the registry
    # must have a genuinely verified, write-capable integration -- never a
    # same-named fact flag standing in for either.
    "approved customer workflow": lambda facts, world, case, registry, origin_failures: (
        (customer := world.customers.get(facts.get("customer_id"), {})).get("authority_verified") is True
        and isinstance(customer.get("workflow_permissions"), list)
        and case["workflow_id"] in customer["workflow_permissions"]),
    # Same fix as "integration registry verified" above (P13-REV3-02/REV4-01/
    # REV4-02): _execute_ready enforces the real governed callers' mode ==
    # "sandbox" requirement (not `.allowed` alone, which also permits live/
    # write_limited) and supplies requested_scope from the case's own
    # trusted customer_id fact (not the registry's own configured scope,
    # and never omitted, which would silently fail every correctly-scoped
    # integration).
    "verified writable integration": lambda facts, world, case, registry, origin_failures: any(
        _execute_ready(registry, integration=name, requested_scope=facts.get("customer_id"))
        for name in registry.get("integrations", {})),
    # Estimate follow-up is grounded in the linked customer's recorded authority,
    # an explicit channel/time fact, and the fixture's numeric send budget.
    "customer-approved timing and channel": lambda facts, world, case, registry, origin_failures: (
        (customer := world.customers.get(facts.get("customer_id"), {})).get("authority_verified") is True
        and facts.get("customer_authority_verified") is True
        and isinstance(customer.get("approved_message_channels"), list)
        and facts.get("customer_approved_channel") in customer["approved_message_channels"]
        and _approved_followup_time(facts, world)),
    "follow-up count within policy": lambda facts, world, case, registry, origin_failures: (
        type(facts.get("followups_sent")) is int and facts["followups_sent"] >= 0
        and type(facts.get("approved_max_followups")) is int
        and facts["approved_max_followups"] > facts["followups_sent"]),
    # Reactivation requires an actual opt-out route on an available approved
    # template and a bounded, positive batch size in the synthetic authority.
    "opt-out path": lambda facts, world, case, registry, origin_failures: any(
        isinstance((world.templates.get(tid) or {}).get("opt_out_route"), dict)
        and world.templates[tid]["opt_out_route"].get("covers_all_marketing") is True
        and world.templates[tid].get("approved_at")
        and world.templates[tid].get("revoked_at") is None
        for tid in case.get("available_template_ids", [])),
    "batch size approved": lambda facts, world, case, registry, origin_failures: (
        (customer := world.customers.get(facts.get("customer_id"), {})).get("authority_verified") is True
        and facts.get("customer_authority_verified") is True
        and type(facts.get("record_count")) is int and facts["record_count"] > 0
        and type(customer.get("approved_batch_size")) is int
        and facts["record_count"] <= customer["approved_batch_size"]),
    # integration_failure_recovery: grounded in the persistent dispatch-ledger
    # record the recovery case's own origin facts describe (_origin_dispatch)
    # for structural identity, and in origin_failures -- the origin's own
    # genuinely persisted failure_detected count -- for retry-count evidence
    # (P13-REV2-01). Never world.action_failures (already popped by the time
    # these run), never dispatch-ledger status alone (sits at "prepared"
    # whether zero or five real failures happened), and never a same-named
    # fixture fact.
    "retry policy permits another attempt": lambda facts, world, case, registry, origin_failures: (
        _origin_dispatch(facts, world) is not None and 1 <= origin_failures < SAFE_RETRY_LIMIT),
    "idempotency is preserved": lambda facts, world, case, registry, origin_failures: (
        (record := _origin_dispatch(facts, world)) is not None and bool(record.get("adapter_idempotency_key"))),
    "safe retry limit reached or repeated provider errors": lambda facts, world, case, registry, origin_failures: (
        _origin_dispatch(facts, world) is not None and origin_failures >= 2),
    "unsent records remain durable": lambda facts, world, case, registry, origin_failures: (
        (record := _origin_dispatch(facts, world)) is not None and record["status"] == "prepared"),
    "provider errors continue": lambda facts, world, case, registry, origin_failures: (
        _origin_dispatch(facts, world) is not None and origin_failures >= 2),
    # Bound to the origin's provider and to a later successful sandbox
    # adapter attempt on that same provider. Static registry verification
    # remains necessary but cannot certify post-failure recovery by itself.
    "provider health verified": lambda facts, world, case, registry, origin_failures: (
        (record := _origin_dispatch(facts, world)) is not None
        and case.get("_post_failure_provider_health") is True
        and bool(provider := ((record.get("frozen_native_decision") or {}).get("integration") or {}).get("name"))
        and registry.get("integrations", {}).get(provider, {}).get("verified") is True
        and registry["integrations"][provider].get("mode") == "sandbox"),
    "original approval remains valid": lambda facts, world, case, registry, origin_failures: (
        (record := _origin_dispatch(facts, world)) is not None and record["status"] != "blocked"),
    "no scope expansion": lambda facts, world, case, registry, origin_failures: (
        _origin_dispatch(facts, world) is not None and not any(
            record.get("origin_workflow_id") == facts.get("origin_workflow_id")
            and record.get("origin_entity_id") == facts.get("origin_entity_id")
            and record.get("frozen_native_decision", {}).get("action") != facts.get("original_action")
            for record in world.dispatches.values())),
    # Reused wherever a recovery/aggregate case needs evidence that its queue
    # binding genuinely exists, not merely a same-named fact flag.
    "queue integrity verified": lambda facts, world, case, registry, origin_failures: case["entity_id"] in world.queue,
}


class HarnessDefect(ValueError):
    """Invalid driver inputs, unexpected service rejection, or orchestration bug."""


class FixtureAuthoringError(HarnessDefect):
    """A fixture has an invalid or statically unresolvable reference."""


@dataclass
class HarnessReport:
    provider: str = "deterministic_mock"
    outcomes: dict = field(default_factory=dict)
    service_rejections: list = field(default_factory=list)
    harness_defect: dict | None = None
    environment_failure: dict | None = None
    pending: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)


def classify_outcome(payload: dict) -> str:
    """Fail closed on unknown signatures; do not infer cause from gate alone."""
    kind, result, gate = payload["event_type"], payload["result"], payload["metadata"]["gate"]
    if gate in {"allow", "approved_resume", "reconciliation"}:
        kinds = {"transition_completed", "terminal_outcome"}
        results = {"executed_in_sandbox"}
        if gate != "reconciliation":
            kinds.add("decision_produced")
            results.add("observed")
        if kind in kinds and result in results:
            return "terminal" if kind == "terminal_outcome" else "ready"
    if (kind, result, gate) == ("approval_requested", "escalated", "approval"):
        return "waiting_on_approval"
    if kind == "approval_resolved" and gate == "founder_resolution":
        status = payload["approval"]["status"]
        if status in STATUSES and result == ("observed" if status in EXECUTABLE else "blocked"):
            return "resume_pending" if status in EXECUTABLE else "waiting_on_evidence" if status == "needs_more_evidence" else "ready"
    if (kind, result, gate) == ("transition_attempted", "blocked", "compliance"):
        return "compliance_blocked"
    if kind == "failure_detected":
        if gate == "transition" and result in {"blocked", "failed"}:
            return ("waiting_on_evidence" if result == "blocked" and
                    payload.get("error") == "canonical transition requirements lack evidence" else "transition_defect")
        outcomes = {
            ("approval", "blocked"): "approval_collision", ("approval_resume", "blocked"): "resume_defect",
            ("contract", "failed"): "native_decision_defect", ("policy", "blocked"): "policy_blocked",
            ("dispatch_review", "blocked"): "waiting_on_reconciliation",
            ("unsupported_execute", "blocked"): "waiting_on_reconciliation",
            ("integration", "failed"): "integration_failure", ("provider", "failed"): "provider_failure",
            ("provider", "blocked"): "provider_failure",
        }
        if (gate, result) in outcomes:
            return outcomes[gate, result]
    raise HarnessDefect(f"unrecognized service signature: {(kind, result, gate)}")


def export_pair(event) -> tuple[dict, dict]:
    payload = export_sandbox_event(event)
    native = deepcopy(event.native_decision)
    return payload, {"event_id": payload["event_id"], "native_decision": native,
                     "native_failure": deepcopy(event.native_failure),
                     "handoff_to": native.get("handoff_to") if native else None}


def validate_artifact_shapes(fixture_pack: Any, founder_resolutions: Any, driver_controls: Any) -> None:
    """Cheap, structural pre-flight the CLI also runs before build_stage2_snapshot.

    Callers that pre-build their own ``world`` (the CLI) never reach
    ``run_stage2_fixture_pack``'s own validation until after
    ``build_stage2_snapshot`` has already run against the same malformed
    pack -- and that loader's own exception handling turns a malformed case
    into a plain ``Stage2InputError`` (a ValueError), not a HarnessDefect,
    which the CLI's outer except Exception would misfile as an environment
    failure (P13B-06). Calling this first, before build_stage2_snapshot,
    closes that gap without changing company_os_stage2_inputs.py's own
    unrelated call paths (e.g. load_stage2_inputs).
    """
    if not isinstance(fixture_pack, dict) or not isinstance(fixture_pack.get("cases"), list) or any(
        not isinstance(case, dict) for case in fixture_pack["cases"]
    ):
        raise FixtureAuthoringError("fixture pack must be an object with a list of case objects")
    if founder_resolutions is not None and (not isinstance(founder_resolutions, list) or any(
        not isinstance(row, dict) for row in founder_resolutions
    )):
        raise FixtureAuthoringError("founder resolutions must be a list of correction objects")
    if driver_controls is not None and (not isinstance(driver_controls, dict) or any(
        not isinstance(value, dict) for value in driver_controls.values()
    )):
        raise FixtureAuthoringError("driver controls must be an object of case-keyed control objects")


def validate_pack(pack: dict, workflows: dict, corrections: list, registry: dict) -> None:
    """Validate declared references, not unknowable future model outcomes."""
    sentinels = pack.get("scripted_failure_types", [])
    if not isinstance(sentinels, list) or any(name != "ScriptedProviderTimeout" for name in sentinels):
        raise FixtureAuthoringError("unknown scripted failure type declaration")
    cases = {c["id"]: c for c in pack["cases"]}
    if len(cases) != len(pack["cases"]) or not cases:
        raise FixtureAuthoringError("case identities must be unique and nonempty")
    deliveries = {}
    decisions = set()
    for case in cases.values():
        workflow = workflows[case["workflow_id"]]
        if case["workflow_id"] not in CLASSIFICATIONS or workflow["entity_type"] != case["entity_type"]:
            raise FixtureAuthoringError("unsupported workflow classification or entity type")
        if not case["inputs"]:
            raise FixtureAuthoringError("case needs signals")
        # Every reachable transition requirement needs a real derivation rule
        # (P13B-02); a fixture fact literally named after one would trivially
        # self-fulfill it, so that exact key is banned from signal facts.
        requirement_strings = {req for t in workflow["transitions"] for req in t.get("requirements", [])}
        unknown_requirements = requirement_strings - set(REQUIREMENT_RULES)
        if unknown_requirements:
            raise FixtureAuthoringError(
                f"no derivation rule for transition requirement: {sorted(unknown_requirements)[0]}")
        for signal in case["inputs"]:
            delivery = signal["delivery_id"]
            if delivery in deliveries:
                raise FixtureAuthoringError("duplicate delivery identity in scheduling graph")
            if signal["type"] in {"founder_resolution", "retry"}:
                raise FixtureAuthoringError("control commands must not be inbound signals")
            if "sequence_hint" in signal and (type(signal["sequence_hint"]) is not int or signal["sequence_hint"] < 0):
                raise FixtureAuthoringError("sequence_hint must be a nonnegative integer")
            if not isinstance(signal.get("after", []), list):
                raise FixtureAuthoringError("after must be a predecessor list")
            facts = signal.get("facts", {})
            if isinstance(facts, dict) and requirement_strings & set(facts):
                raise FixtureAuthoringError(
                    "fixture fact key must not literally match a transition requirement string")
            deliveries[delivery] = signal
        refs, ordinals = set(), set()
        for attempt in case.get("attempts", []):
            ordinal = attempt["ordinal"]
            if type(ordinal) is not int or not 1 <= ordinal <= MAX_DECISIONS_PER_CASE or ordinal in ordinals:
                raise FixtureAuthoringError("invalid or duplicate attempt ordinal")
            if not attempt["ref"] or attempt["ref"] in refs:
                raise FixtureAuthoringError("attempt references must be unique")
            refs.add(attempt["ref"])
            ordinals.add(ordinal)
            if attempt.get("decision_id"):
                if attempt["decision_id"] in decisions:
                    raise FixtureAuthoringError("decision IDs must be unique across the run")
                decisions.add(attempt["decision_id"])
            if attempt.get("scripted_failure") and (
                attempt["scripted_failure"] != "ScriptedProviderTimeout" or
                "ScriptedProviderTimeout" not in pack.get("scripted_failure_types", [])
            ):
                raise FixtureAuthoringError("undeclared or unknown scripted failure type")
        for attempt in case.get("attempts", []):
            if attempt.get("replay_of") is not None:
                earlier = {a["ref"] for a in case["attempts"] if a["ordinal"] < attempt["ordinal"]}
                if attempt["replay_of"] not in earlier:
                    raise FixtureAuthoringError("replay must reference an earlier attempt")
        local_decisions = {a["decision_id"] for a in case.get("attempts", []) if a.get("decision_id")}
        rejection = case.get("expected_service_rejection")
        if rejection is not None and (
            set(rejection) != {"call", "exception_type", "expects", "binding"}
            or rejection["call"] not in {"coordinate_sandbox_action", "resolve_sandbox_approval", "resume_sandbox_approval"}
            or rejection["exception_type"] != ("SandboxCoordinatorError" if rejection["call"] == "coordinate_sandbox_action" else "SandboxApprovalError")
            or not isinstance(rejection["expects"], str) or not rejection["expects"]
            or not isinstance(rejection["binding"], str) or not rejection["binding"]
        ):
            raise FixtureAuthoringError(
                "service rejection requires exact call, exception type, message and a bound attempt/decision")
        commands = case.get("control_commands", [])
        command_ids = [c["id"] for c in commands]
        if len(command_ids) != len(set(command_ids)):
            raise FixtureAuthoringError("duplicate command identity")
        resolution_targets = set()
        retry_targets = set()
        for command in commands:
            pre = command["precondition"]
            if command["command_type"] == "founder_resolution":
                target = command["decision_id"]
                if pre != {"type": "approval_pending", "decision_id": target} or target not in local_decisions:
                    raise FixtureAuthoringError("resolution references an undeclared decision")
                if target in resolution_targets:
                    raise FixtureAuthoringError("only one resolution command per decision")
                resolution_targets.add(target)
                matches = [r for r in corrections if (r["case_id"], r["decision_id"]) == (case["id"], target)]
                if len(matches) != 1:
                    raise FixtureAuthoringError("resolution requires exactly one corrections lookup")
            elif command["command_type"] == "retry":
                target_key = json.dumps(pre, sort_keys=True)
                if target_key in retry_targets:
                    raise FixtureAuthoringError("one retry command per failed attempt")
                retry_targets.add(target_key)
                if pre["type"] != "failed_attempt_persisted":
                    raise FixtureAuthoringError("invalid retry precondition")
                if "decision_id" in pre:
                    if (set(pre) != {"type", "decision_id", "failure_ordinal"} or
                        pre["decision_id"] not in local_decisions or type(pre["failure_ordinal"]) is not int or
                        not 1 <= pre["failure_ordinal"] <= MAX_RETRIES_PER_CASE):
                        raise FixtureAuthoringError("resume retry needs declared decision and failure ordinal")
                elif set(pre) != {"type", "attempt_ref"} or pre["attempt_ref"] not in refs:
                    raise FixtureAuthoringError("retry references an undeclared attempt")
            else:
                raise FixtureAuthoringError("unknown control command")
    graph = {}
    for delivery, signal in deliveries.items():
        graph[delivery] = []
        for dependency in signal.get("after", []):
            if not isinstance(dependency, str):
                raise FixtureAuthoringError("dependency must be a string")
            if dependency.startswith("signal:"):
                target = dependency[7:]
                if target not in deliveries:
                    raise FixtureAuthoringError("unknown signal dependency")
                graph[delivery].append(target)
            elif dependency.startswith("event:"):
                parts = dependency.split(":")
                if (len(parts) != 5 or parts[1] not in cases or parts[3] not in
                    {"failure_detected", "transition_completed", "terminal_outcome"} or
                    not parts[4].isdigit() or int(parts[4]) < 1 or
                    not any(t["action"] == parts[2] for t in workflows[cases[parts[1]]["workflow_id"]]["transitions"])):
                    raise FixtureAuthoringError("invalid persisted-event dependency")
                source_signals = cases[parts[1]]["inputs"]
                initial = [s["delivery_id"] for s in source_signals if not s.get("after")]
                graph[delivery].extend(initial or [s["delivery_id"] for s in source_signals])
            elif dependency.startswith("milestone:"):
                parts = dependency.split(":")
                if len(parts) != 3 or parts[1] not in cases:
                    raise FixtureAuthoringError("unknown milestone case")
                target = cases[parts[1]]
                transitions = workflows[target["workflow_id"]]["transitions"]
                if not any(t["action"] == parts[2] and is_email_execute(
                    {"name": t.get("integration"), "phase": "execute"}, registry,
                ) for t in transitions):
                    raise FixtureAuthoringError("milestone target is not a governed email transition")
                # A queued_message's own origin facts are what receive() actually
                # matches against (P13B-04); a milestone barrier that doesn't
                # agree with them would let the scheduler drain a signal receive()
                # can never bind, producing a silent unmatched queue entry.
                if signal["type"] == "queued_message":
                    origin_facts = signal.get("facts", {})
                    if (origin_facts.get("origin_workflow_id") != target["workflow_id"]
                            or origin_facts.get("origin_entity_id") != target["entity_id"]
                            or origin_facts.get("original_action") != parts[2]):
                        raise FixtureAuthoringError(
                            "queued message origin facts do not match its milestone dependency")
                    if (target["workflow_id"] in PERSON_WORKFLOWS
                            and origin_facts.get("recipient_id") != target["contact_record"].get("recipient_id")):
                        raise FixtureAuthoringError("queued message recipient differs from person-workflow origin")
            else:
                raise FixtureAuthoringError("ambiguous dependency: expected signal: or milestone:")
    visiting, done = set(), set()

    def visit(node):
        if node in visiting:
            raise FixtureAuthoringError("cyclic signal dependencies")
        if node in done:
            return
        visiting.add(node)
        for other in graph[node]:
            visit(other)
        visiting.remove(node)
        done.add(node)
    for node in graph:
        visit(node)
    correction_keys = set()
    for row in corrections:
        key = row["case_id"], row["decision_id"]
        if key in correction_keys or key[0] not in cases or row["status"] not in STATUSES:
            raise FixtureAuthoringError("invalid or duplicate founder correction")
        correction_keys.add(key)
        if key[1] not in {a.get("decision_id") for a in cases[key[0]].get("attempts", [])}:
            raise FixtureAuthoringError("correction references an undeclared decision")
        minutes = row.get("founder_minutes")
        if (not isinstance(row.get("founder_id"), str) or not row["founder_id"] or
            type(minutes) not in (int, float) or not math.isfinite(minutes) or minutes < 0):
            raise FixtureAuthoringError("founder identity and finite nonnegative minutes are required")
        manual = row.get("manual_evidence_ref")
        if manual is not None and (row["status"] not in EXECUTABLE or
                                   not isinstance(manual, str) or not manual.startswith("sandbox://")):
            raise FixtureAuthoringError("manual evidence requires an executable status and sandbox reference")
        if (row["status"] == "modified") != (row.get("corrected_decision") is not None):
            raise FixtureAuthoringError("only modified resolution carries corrected_decision")
        if row.get("corrected_decision") is not None and set(row["corrected_decision"]) != {
            "action", "risk_level", "state_after", "agent_type", "handoff_to", "policy_intent", "integration"
        }:
            raise FixtureAuthoringError("corrected decision must have the seven native fields")
        if row["status"] == "modified" and pack.get("infrastructure_only") is not True:
            intent = row["corrected_decision"].get("policy_intent")
            expected_marker = f"sandbox://founder-correction/{row['case_id']}/{row['decision_id']}"
            if not isinstance(intent, dict) or intent.get("founder_correction_ref") != expected_marker:
                raise FixtureAuthoringError("modified correction needs founder-only marker")


def bound_recipient(case: dict, world: SyntheticWorld) -> tuple[str | None, dict | None]:
    """Mirror resolve_recipient's own recipient-key choice (company_os_sandbox_dispatch.py:94-133).

    Person (and otherwise-unscoped, e.g. customer_onboarding) cases are keyed on
    their own contact record's recipient_id. Aggregate/queue-based cases have no
    real recipient until their queue item is bound; the queue item's own
    recipient_id is authoritative then, never the case's own contact_record.
    """
    if case["workflow_id"] in AGGREGATE_WORKFLOWS:
        item = world.queue.get(case["entity_id"])
        return (item["recipient_id"], item) if item else (None, None)
    return case["contact_record"]["recipient_id"], None


async def evidence_context(case: dict, consumed: list, world: SyntheticWorld, registry: dict, db: AsyncSession) -> dict:
    """Conservative factual derivation; never copy a workflow requirement list.

    Every verified requirement comes from an explicit, per-string predicate
    (REQUIREMENT_RULES) grounded in genuinely available evidence -- never a
    fixture fact literally named after the requirement it would attest.
    """
    recipient, _ = bound_recipient(case, world)
    facts = {}
    for signal in consumed:
        if signal["type"] != "failure_injection":
            facts.update(deepcopy(signal["facts"]))
    # Computed once per call, not once per predicate, and passed to every
    # predicate uniformly (P13-REV2-01) -- the only real, accumulating,
    # queryable evidence of how many times the recovery case's own
    # originating dispatch has genuinely failed.
    origin_record = _origin_dispatch(facts, world)
    origin_failures = await _origin_failure_count(db, origin_record)
    derived_case = {**case, "_post_failure_provider_health":
                    await _post_failure_provider_health(db, origin_record)}
    requirements = {name for name, rule in REQUIREMENT_RULES.items()
                     if rule(facts, world, derived_case, registry, origin_failures)}
    refs = [f"sandbox://fixture/{case['id']}/snapshot"]
    refs.extend(f"sandbox://delivery/{s['delivery_id']}" for s in consumed)
    contact = world.contacts.get(recipient, {}) if recipient else {}
    return {"facts": facts, "signals": deepcopy([s for s in consumed if s["type"] != "failure_injection"]),
            "contact_record": deepcopy(contact),
            "consent_record": deepcopy(world.consents.get(recipient, {})) if recipient else {},
            "suppression_snapshot": deepcopy(world.suppression_snapshots.get(recipient, {})) if recipient else {},
            "available_template_ids": deepcopy(case.get("available_template_ids", [])),
            "approved_templates": {key: deepcopy(world.templates[key])
                                   for key in case.get("available_template_ids", []) if key in world.templates},
            "verified_requirements": sorted(requirements), "evidence_refs": refs,
            "consent_verified": bool(recipient) and contact.get("consent_verified") is True and not world.is_suppressed(recipient),
            "authority_verified": (
                (world.customers.get(facts.get("customer_id"), {}).get("authority_verified") is True
                 and facts.get("customer_authority_verified") is True)
                if isinstance(facts.get("customer_id"), str)
                else facts.get("authority_verified") is True or facts.get("customer_authority_verified") is True)}


async def decision_fingerprint(case: dict, consumed: list, world: SyntheticWorld, registry: dict, db: AsyncSession) -> str:
    """Hash only compliance/policy-relevant evidence, never the ever-growing raw ref list.

    Used both to gate drain()'s blocked-outcome reset and record()'s
    no-progress guard, so neither mistakes an unrelated new signal (or the
    delivery-ref list simply getting longer) for genuinely new evidence.
    """
    context = await evidence_context(case, consumed, world, registry, db)
    relevant = {key: context[key] for key in (
        "facts", "verified_requirements", "consent_verified", "authority_verified",
        "contact_record", "consent_record", "suppression_snapshot")}
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class _Case:
    source: dict
    instance: Any
    consumed: list = field(default_factory=list)
    commands_done: set = field(default_factory=set)
    dropped_commands: list = field(default_factory=list)
    attempts: int = 0
    retries: int = 0
    outcome: str = "ready"
    last_progress: tuple | None = None
    blocked_fingerprint: str | None = None
    retry_key: str | None = None
    attempt_events: dict = field(default_factory=dict)
    attempt_keys: dict = field(default_factory=dict)
    resume_due: str | None = None
    rejection_credited: bool = False


class _Runner:
    def __init__(self, db, pack, world, decide, workflows, options, corrections, report):
        self.db, self.pack, self.world, self.decide = db, pack, world, decide
        self.workflows, self.options, self.corrections, self.report = workflows, options, corrections, report
        self.cases = {}
        self.ingested, self.exported = set(), set()
        self.recorded = []
        self.activity = 0

    def options_for(self, state):
        return {**self.options, "workflow": self.workflows[state.source["workflow_id"]]}

    async def wake_blocked_cases(self):
        """Recheck shared evidence after any signal or persisted event.

        Recovery requirements can change through another case's dispatch or
        provider-health event, with no new signal to the blocked case itself.
        """
        for state in self.cases.values():
            if state.outcome not in {"waiting_on_evidence", "compliance_blocked", "policy_blocked"}:
                continue
            current = await decision_fingerprint(state.source, state.consumed, self.world,
                self.options["integration_registry"], self.db)
            if state.blocked_fingerprint is None:
                state.blocked_fingerprint = current
            elif current != state.blocked_fingerprint:
                state.blocked_fingerprint = None
                state.outcome = "ready"
                self.report.outcomes[state.source["id"]] = "ready"

    def dependency_ready(self, dependency):
        if dependency.startswith("signal:"):
            return dependency[7:] in self.ingested
        if dependency.startswith("event:"):
            _, case_id, action, event_type, count = dependency.split(":")
            case = self.cases[case_id].source
            return sum(event["workflow_id"] == case["workflow_id"]
                       and event["entity_id"] == case["entity_id"]
                       and event["action"] == action and event["event_type"] == event_type
                       for event in self.recorded) >= int(count)
        _, case_id, action = dependency.split(":")
        case = self.cases[case_id].source
        # Evaluate the identical predicate SyntheticWorld.receive uses to match
        # a queued_message against a dispatch record (company_os_synthetic_
        # adapters.py:189-197) -- keyed on the record's own origin fields, not
        # on which instance happens to be a registered consumer of it. A
        # chained/queue-originated record's origin never becomes the
        # downstream case's own instance, so matching on "consumer" here would
        # diverge silently from what receive() will actually bind (P13B-04).
        return any(record["status"] != "blocked"
                   and record.get("origin_workflow_id") == case["workflow_id"]
                   and record.get("origin_entity_id") == case["entity_id"]
                   and record["frozen_native_decision"]["action"] == action
                   for record in self.world.dispatches.values())

    async def drain(self):
        """Global fixed point after each arrival/event, before another service call."""
        while True:
            changed = False
            for state in self.cases.values():
                signals = sorted(enumerate(state.source["inputs"]), key=lambda pair: (pair[1].get("sequence_hint", pair[0]), pair[0]))
                for _, signal in signals:
                    if signal["delivery_id"] in self.ingested or not all(self.dependency_ready(d) for d in signal.get("after", [])):
                        continue
                    try:
                        apply_stage2_signal(self.world, state.source, signal, self.world.frozen_at)
                    except Exception as exc:
                        raise HarnessDefect(f"signal ingestion: {type(exc).__name__}: {exc}") from exc
                    self.ingested.add(signal["delivery_id"])
                    state.consumed.append(deepcopy(signal))
                    self.activity += 1
                    changed = True
                    # Recheck every blocked case, because shared dispatch/health
                    # evidence can also change through another case (M3).
                    await self.wake_blocked_cases()
                    # Re-scan from the beginning after each signal, including cross-case references.
                    break
                if changed:
                    break
            if not changed:
                return

    async def approval(self, state, decision_id):
        row = await self.db.scalar(select(CompanyOSSandboxApproval).where(
            CompanyOSSandboxApproval.workflow_instance_id == state.instance.id,
            CompanyOSSandboxApproval.sandbox_run_id == state.instance.sandbox_run_id,
            CompanyOSSandboxApproval.decision_id == decision_id,
        ))
        if row is not None:
            request = await self.db.get(CompanyOSSandboxEvent, row.request_event_id)
            if (request is None or request.event_type != "approval_requested" or
                request.workflow_instance_id != state.instance.id or request.sandbox_run_id != row.sandbox_run_id or
                request.payload["approval"]["decision_id"] != decision_id):
                raise HarnessDefect("approval request binding is invalid")
        return row

    async def call(self, state, function, *, binding=None, **kwargs):
        await self.drain()
        try:
            return await function(self.db, **kwargs)
        except (SandboxCoordinatorError, SandboxApprovalError) as exc:
            tag = state.source.get("expected_service_rejection")
            if (tag and not state.rejection_credited
                    and tag.get("call") == function.__name__ and tag.get("expects") == str(exc)
                    and tag.get("exception_type", type(exc).__name__) == type(exc).__name__
                    and tag.get("binding") == binding
                    and await self._rejection_precondition_holds(state, function, kwargs, binding)):
                state.rejection_credited = True
                self.report.service_rejections.append({"case_id": state.source["id"], "call": function.__name__,
                    "exception_type": type(exc).__name__, "message": str(exc), "binding": binding,
                    "expected": True})
                state.outcome = "service_rejection"
                self.activity += 1
                return None
            raise HarnessDefect(f"{function.__name__}: {type(exc).__name__}: {exc}") from exc

    async def _rejection_precondition_holds(self, state, function, kwargs, binding):
        """Verify the driver actually created the precondition the fixture tagged.

        The coordinator's own duplicate/stale/terminal raise persists an
        identical message for three distinct causes; matching on message text
        alone (P13B-05) could credit an unrelated scheduling bug as if it were
        the one deliberately-scripted scenario. This binds the tag to a
        specific attempt/decision and checks the real runtime cause, so a
        non-matching or unbound rejection is always a harness defect instead.
        """
        if function.__name__ == "coordinate_sandbox_action":
            attempt = next((a for a in state.source.get("attempts", []) if a.get("ref") == binding), None)
            if attempt is None:
                return False
            duplicate = (attempt.get("replay_of") is not None
                        and kwargs["idempotency_key"] in state.attempt_keys.values())
            stale = kwargs.get("expected_version") != state.instance.version
            return duplicate or stale or state.instance.terminal
        approval_id = kwargs.get("approval_id")
        if approval_id is None:
            return False
        approval = await self.db.get(CompanyOSSandboxApproval, approval_id)
        return approval is not None and approval.decision_id == binding

    async def record(self, state, event):
        if event is None or event.event_id in self.exported:
            return None
        pair = export_pair(event)
        self.exported.add(event.event_id)
        self.recorded.append(pair[0])
        self.activity += 1
        state.outcome = classify_outcome(pair[0])
        if state.outcome == "ready":
            fingerprint = await decision_fingerprint(state.source, state.consumed, self.world,
                                               self.options["integration_registry"], self.db)
            progress = (event.state_before, pair[0]["action"], event.state_after, fingerprint)
            if progress == state.last_progress:
                state.outcome = "waiting_on_evidence"
            state.last_progress = progress
        if state.outcome == "provider_failure":
            failure_type = (event.native_failure or {}).get("type")
            if failure_type == "CompanyOSDecisionMethodDefect":
                self.report.harness_defect = {"case_id": state.source["id"], "type": failure_type}
            elif failure_type not in {*self.pack.get("scripted_failure_types", []), "CompanyOSMalformedOutputError"}:
                self.report.environment_failure = {"case_id": state.source["id"], "type": failure_type}
        if state.outcome in {"waiting_on_evidence", "compliance_blocked", "policy_blocked"}:
            state.blocked_fingerprint = await decision_fingerprint(
                state.source, state.consumed, self.world, self.options["integration_registry"], self.db)
        else:
            state.blocked_fingerprint = None
        self.report.outcomes[state.source["id"]] = state.outcome
        await self.wake_blocked_cases()
        return pair

    async def resolve(self, state, decision_id):
        """Existence-only binding; exact replays remain the service's responsibility."""
        row = await self.approval(state, decision_id)
        if row is None:
            raise HarnessDefect("resolution has no persisted approval request")
        correction = next(r for r in self.corrections if
                          (r["case_id"], r["decision_id"]) == (state.source["id"], decision_id))
        params = {key: correction.get(key) for key in
                  ("status", "founder_id", "founder_minutes", "corrected_decision", "manual_evidence_ref")}
        key = f"{state.source['id']}:{decision_id}:{correction['status']}"
        return await self.call(state, resolve_sandbox_approval, binding=decision_id, approval_id=row.id,
            resolution_key=key, event_schema=self.options["event_schema"],
            workflow=self.workflows[state.source["workflow_id"]], **params)

    async def controls(self) -> AsyncIterator[tuple[dict, dict]]:
        """Dispatch one command/service call at a time, then globally re-scan."""
        while True:
            await self.drain()
            changed = False
            for state in self.cases.values():
                if state.resume_due:
                    decision_id = state.resume_due
                    state.resume_due = None
                    row = await self.approval(state, decision_id)
                    event = await self.call(state, resume_sandbox_approval, binding=decision_id,
                        approval_id=row.id, **self.options_for(state))
                    pair = await self.record(state, event)
                    if pair:
                        yield pair
                    changed = True
                    break
                for command in state.source.get("control_commands", []):
                    if command["id"] in state.commands_done:
                        continue
                    pre = command["precondition"]
                    row = None
                    if pre["type"] == "approval_pending":
                        row = await self.approval(state, pre["decision_id"])
                        if row is None or row.status != "pending":
                            continue
                    elif "decision_id" in pre:
                        row = await self.approval(state, pre["decision_id"])
                        if row is None or row.failure_attempt_count != pre["failure_ordinal"] or row.last_failure_event_id is None:
                            continue
                        failed = await self.db.get(CompanyOSSandboxEvent, row.last_failure_event_id)
                        if (failed.event_type != "failure_detected" or failed.workflow_instance_id != state.instance.id or
                            failed.sandbox_run_id != row.sandbox_run_id):
                            continue
                    else:
                        failed_id = state.attempt_events.get(pre["attempt_ref"])
                        if failed_id is None:
                            continue
                        failed = await self.db.get(CompanyOSSandboxEvent, failed_id)
                        if failed.event_type != "failure_detected" or failed.workflow_instance_id != state.instance.id:
                            continue
                    state.commands_done.add(command["id"])
                    self.activity += 1
                    changed = True
                    if command["command_type"] == "founder_resolution":
                        event = await self.resolve(state, row.decision_id)
                        if event is not None and row.status in EXECUTABLE:
                            state.resume_due = row.decision_id
                    else:
                        if state.retries >= MAX_RETRIES_PER_CASE:
                            # This command was already marked consumed above,
                            # but it never actually dispatched a retry -- record
                            # it as dropped so the failure report doesn't
                            # silently omit it (P13B-09).
                            state.dropped_commands.append(command["id"])
                            state.outcome = "retry_budget_exhausted"
                            self.report.outcomes[state.source["id"]] = state.outcome
                            break
                        state.retries += 1
                        if row is not None:
                            event = await self.call(state, resume_sandbox_approval, binding=row.decision_id,
                                                    approval_id=row.id, retry=True, **self.options_for(state))
                        else:
                            if state.attempts >= MAX_DECISIONS_PER_CASE:
                                state.dropped_commands.append(command["id"])
                                state.outcome = "decision_budget_exhausted"
                            else:
                                state.retry_key = f"{failed.idempotency_key}:retry:{command['id']}"
                                state.outcome = "ready"
                            self.report.outcomes[state.source["id"]] = state.outcome
                            break
                    pair = await self.record(state, event)
                    if pair:
                        yield pair
                    break
                if changed:
                    break
            if self.report.harness_defect or self.report.environment_failure or not changed:
                return

    async def advance(self, state):
        await self.drain()
        if state.outcome != "ready" or not state.consumed:
            return None
        if state.attempts >= MAX_DECISIONS_PER_CASE:
            state.outcome = "decision_budget_exhausted"
            self.report.outcomes[state.source["id"]] = state.outcome
            return None
        state.attempts += 1
        attempt = next((a for a in state.source.get("attempts", []) if a["ordinal"] == state.attempts), {})
        # Every driver-owned operation completes before the coordinator is called.
        try:
            context = await evidence_context(state.source, state.consumed, self.world,
                self.options["integration_registry"], self.db)
            context.update(case_id=state.source["id"], current_state=state.instance.current_state,
                           workflow=deepcopy(self.workflows[state.source["workflow_id"]]),
                           canonical_agents=self.options["canonical_agents"], canonical_actions=self.options["canonical_actions"],
                           canonical_handoffs=self.options["canonical_handoffs"],
                           policy=deepcopy(self.options["policy"]), integration_registry=deepcopy(self.options["integration_registry"]))
            # Once a bound aggregate-workflow queue item exists, carry its id at
            # the snapshot's top level so find_dispatch/resolve_recipient's
            # queue-scope cross-check actually runs (P13B-10) -- otherwise it
            # never fires for any harness run and a real queue-scope mismatch
            # would pass unnoticed.
            _, item = bound_recipient(state.source, self.world)
            if item is not None:
                context["item_id"] = item["item_id"]
            key = state.retry_key or f"{state.source['id']}:attempt:{state.attempts}"
            state.retry_key = None
            if attempt.get("replay_of"):
                key = state.attempt_keys[attempt["replay_of"]]
            snapshot = deepcopy(context)
            snapshot["primary_classification"] = CLASSIFICATIONS[state.source["workflow_id"]]
            if attempt.get("decision_id"):
                snapshot["approval_decision_id"] = attempt["decision_id"]
            if attempt.get("scripted_failure"):
                sentinel = ScriptedProviderTimeout("fixture-scripted provider timeout")

                async def callback(_):
                    raise sentinel
            elif hasattr(self.decide, "prepare"):
                callback = self.decide.prepare(context)
            else:
                frozen = deepcopy(context)

                async def callback(_):
                    return await self.decide(frozen)
        except DBAPIError:
            # Evidence derivation queries the event store. Preserve database
            # failure provenance for the run-level DBAPIError classifier.
            raise
        except Exception as exc:
            raise HarnessDefect(f"decision preparation: {type(exc).__name__}: {exc}") from exc
        event = await self.call(state, coordinate_sandbox_action, binding=attempt.get("ref"),
            workflow_instance_id=state.instance.id,
            expected_version=attempt.get("expected_version", state.instance.version), idempotency_key=key,
            synthetic_input=snapshot, decide=callback, **self.options_for(state))
        if attempt.get("ref"):
            state.attempt_keys[attempt["ref"]] = key
            if event is not None:
                state.attempt_events[attempt["ref"]] = event.id
        return await self.record(state, event)


async def run_stage2_fixture_pack(
    db: AsyncSession, *, fixture_pack: dict, run_id: str, decide: DecisionProvider,
    workflows: dict[str, dict], policy: dict, integration_registry: dict,
    event_schema: dict, canonical_agents, canonical_handoffs, canonical_actions,
    synthetic_adapters: dict[str, SyntheticAdapter], world: SyntheticWorld | None = None,
    compliance_policy: dict | None = None, founder_resolutions: list | None = None,
    report: HarnessReport | None = None, driver_controls: dict | None = None,
) -> AsyncIterator[tuple[dict, dict | None]]:
    """Yield each newly persisted canonical/native pair once, in service-call order.

    This function does not commit the caller's session. Consume and commit each
    pair before exporting it. Provider-less/mocked runs are infrastructure only.
    """
    report = report if report is not None else HarnessReport()
    try:
        # Validate the shape of every driver-supplied artifact through the
        # HarnessDefect path before the run starts (P13B-06) -- a malformed
        # pack/controls/corrections file must never be discovered only by
        # whatever incidental exception it happens to trip deep inside the
        # loop, and must never be misfiled as infrastructure flakiness.
        validate_artifact_shapes(fixture_pack, founder_resolutions, driver_controls)
        corrections = deepcopy(founder_resolutions or [])
        pack = deepcopy(fixture_pack)
        if not settings.sandbox_mode or pack.get("synthetic_only") is not True:
            raise FixtureAuthoringError("synthetic-only pack and sandbox_mode are required")
        # Evaluator/control artifacts are separate from the input-only fixture pack.
        controls = deepcopy(driver_controls or {})
        ids = {case["id"] for case in pack["cases"]}
        if set(controls) - ids:
            raise FixtureAuthoringError("controls reference an unknown case")
        for case in pack["cases"]:
            if set(case) & {"attempts", "control_commands", "expected_service_rejection"}:
                raise FixtureAuthoringError("attempts, commands and expectations belong in driver_controls")
            control = controls.get(case["id"], {})
            if set(control) - {"attempts", "control_commands", "expected_service_rejection"}:
                raise FixtureAuthoringError("unknown driver control field")
            case.update(control)
        validate_pack(pack, workflows, corrections, integration_registry)
        if world is None:
            world = build_stage2_snapshot(fixture_pack, integration_registry, pack["approved_templates"],
                run_id=run_id, compliance_policy=compliance_policy or pack.get("compliance_policy", {}))
            synthetic_adapters = {name: SyntheticAdapter(world, adapter.kind) for name, adapter in synthetic_adapters.items()}
        if world.run_id != run_id or world.frozen_at != datetime.fromisoformat(pack["frozen_clock"]):
            raise FixtureAuthoringError("world identity or clock differs from pack")
        if world._inbound or world.action_failures or world.dispatches:
            raise FixtureAuthoringError("runner needs a snapshot-only world, not eagerly ingested inputs")
        if any(not isinstance(a, SyntheticAdapter) or a.world is not world for a in synthetic_adapters.values()):
            raise FixtureAuthoringError("all adapters must share the run's synthetic world")
        report.provider = getattr(decide, "provider", "deterministic_mock")
        run = await create_sandbox_run(db, run_id=run_id, company_slug=pack.get("company_slug", "acqivo"),
            protocol_version=pack.get("schema_version", "0.1.0"),
            input_manifest={"provider": report.provider, "infrastructure_only": report.provider == "deterministic_mock"})
        options = dict(policy=policy, integration_registry=integration_registry, event_schema=event_schema,
                       canonical_agents=canonical_agents, canonical_handoffs=canonical_handoffs,
                       canonical_actions=canonical_actions, synthetic_adapters=synthetic_adapters)
        runner = _Runner(db, pack, world, decide, workflows, options, corrections, report)
        for case in pack["cases"]:
            workflow = workflows[case["workflow_id"]]
            instance = await create_workflow_instance(db, sandbox_run_id=run.id, workflow_id=case["workflow_id"],
                entity_type=case["entity_type"], entity_id=case["entity_id"], initial_state=workflow["states"][0])
            runner.cases[case["id"]] = _Case(case, instance)
        while True:
            before = runner.activity
            await runner.drain()
            for state in runner.cases.values():
                async for pair in runner.controls():
                    yield pair
                if report.harness_defect or report.environment_failure:
                    return
                pair = await runner.advance(state)
                if pair:
                    yield pair
                if report.harness_defect or report.environment_failure:
                    return
                async for pair in runner.controls():
                    yield pair
                if report.harness_defect or report.environment_failure:
                    return
            if runner.activity == before:
                break
        for case_id, state in runner.cases.items():
            report.outcomes[case_id] = state.outcome
            pending_signals = [s["delivery_id"] for s in state.source["inputs"] if s["delivery_id"] not in runner.ingested]
            pending_commands = [c["id"] for c in state.source.get("control_commands", []) if c["id"] not in state.commands_done]
            reported = False
            if pending_commands or state.dropped_commands:
                report.failures.append({"case_id": case_id, "outcome": "unconsumed_control_command",
                                        "commands": pending_commands, "dropped_commands": state.dropped_commands,
                                        "last_outcome": state.outcome})
                reported = True
                if state.outcome not in {"decision_budget_exhausted", "retry_budget_exhausted"}:
                    report.outcomes[case_id] = "unconsumed_control_command"
            if state.outcome in {"decision_budget_exhausted", "retry_budget_exhausted"}:
                report.failures.append({"case_id": case_id, "outcome": state.outcome})
                reported = True
            if pending_signals:
                report.failures.append({"case_id": case_id, "outcome": "unconsumed_signal",
                                        "signals": pending_signals})
                reported = True
            # Every other non-terminal final outcome gets a failures entry too
            # (P13B-11), unless it's one of the run's intentionally-scored
            # non-terminal endpoints (compliance/policy block, a credited
            # service rejection) -- those are documented successes for their
            # own coverage bucket, not defects. See NEVER_A_FAILURE and
            # docs/STAGE2_HARNESS_INTERFACE.md.
            if not reported and report.outcomes[case_id] not in NEVER_A_FAILURE:
                report.failures.append({"case_id": case_id, "outcome": report.outcomes[case_id]})
            if pending_signals or pending_commands:
                report.pending[case_id] = {"signals": pending_signals, "commands": pending_commands,
                                           "reason": "runtime_precondition_unsatisfied"}
    except HarnessDefect as exc:
        report.harness_defect = {"type": type(exc).__name__, "message": str(exc)}
        raise
    except DBAPIError as exc:
        if isinstance(exc, IntegrityError | DataError | ProgrammingError):
            # These three inherit from DBAPIError too, but they mean the
            # application or fixture issued a bad constraint/value/
            # statement -- a genuine code/fixture defect, never an
            # infrastructure outage (P13-REV2-03: the previous blanket
            # "except DBAPIError: raise" carve-out folded these into
            # environment_failure alongside real connection/availability
            # failures). Convert exactly like the generic except Exception
            # below, since none of the three is itself a HarnessDefect.
            report.harness_defect = {"type": type(exc).__name__, "message": str(exc)}
            raise HarnessDefect(str(exc)) from exc
        # Everything else DBAPIError wraps (InterfaceError, OperationalError,
        # InternalError, NotSupportedError, or an unclassified base
        # DBAPIError) represents a genuine connection/availability/storage
        # failure -- the infrastructure actually failing -- and must keep
        # that classification, never folded into harness_defect just because
        # it happened to surface inside this generator.
        raise
    except Exception as exc:
        # Widened from a fixed (KeyError, TypeError, ValueError) tuple
        # (P13B-06): anything raised inside this driver-owned generator is
        # presumed a code/fixture defect, never a guess at infrastructure
        # flakiness -- a genuine provider/environment failure is instead
        # detected only through the typed native_failure classification in
        # record(), never through what exception class happens to escape here.
        report.harness_defect = {"type": type(exc).__name__, "message": str(exc)}
        raise HarnessDefect(str(exc)) from exc
