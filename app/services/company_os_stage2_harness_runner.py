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
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.company_os_compliance import is_email_execute
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
from app.services.company_os_sandbox_service import (
    create_sandbox_run,
    create_workflow_instance,
    export_sandbox_event,
)
from app.services.company_os_stage2_inputs import apply_stage2_signal, build_stage2_snapshot
from app.services.company_os_synthetic_adapters import SyntheticAdapter, SyntheticWorld

MAX_DECISIONS_PER_CASE = 25
MAX_RETRIES_PER_CASE = 3
CLASSIFICATIONS = {
    "prospect_to_meeting": "acquisition", "customer_onboarding": "customer_onboarding",
    "missed_inquiry_recovery": "missed_inquiry_recovery", "estimate_followup": "estimate_followup",
    "stale_lead_reactivation": "stale_lead_reactivation", "integration_failure_recovery": "failure_recovery",
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
        if not any(not s.get("after") for s in case["inputs"]):
            raise FixtureAuthoringError("case needs initial signals")
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
            set(rejection) != {"call", "exception_type", "expects"}
            or rejection["call"] not in {"coordinate_sandbox_action", "resolve_sandbox_approval", "resume_sandbox_approval"}
            or rejection["exception_type"] != ("SandboxCoordinatorError" if rejection["call"] == "coordinate_sandbox_action" else "SandboxApprovalError")
            or not isinstance(rejection["expects"], str) or not rejection["expects"]
        ):
            raise FixtureAuthoringError("service rejection requires exact call, exception type and message")
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


def evidence_context(case: dict, consumed: list, world: SyntheticWorld) -> dict:
    """Conservative factual derivation; never copy a workflow requirement list.

    Unknown natural-language requirements remain unverified. Exact factual
    booleans may attest a requirement by name; false or missing facts cannot.
    """
    entity = case["entity_id"]
    facts = {}
    for signal in consumed:
        if signal["type"] != "failure_injection":
            facts.update(deepcopy(signal["facts"]))
    requirements = {key for key, value in facts.items() if value is True}
    if isinstance(facts.get("public_source"), str) and facts["public_source"]:
        requirements.add("public evidence preserved")
    if (type(facts.get("icp_score")) in (int, float) and math.isfinite(facts["icp_score"])
        and type(facts.get("confidence")) in (int, float) and math.isfinite(facts["confidence"])):
        requirements.add("score and confidence recorded")
    if facts.get("meeting_confirmed") is True and str(facts.get("confirmation_ref", "")).startswith("sandbox://"):
        requirements.update({"calendar or human confirmation", "authoritative calendar or human handoff"})
    refs = [f"sandbox://fixture/{case['id']}/snapshot"]
    refs.extend(f"sandbox://delivery/{s['delivery_id']}" for s in consumed)
    return {"facts": facts, "signals": deepcopy([s for s in consumed if s["type"] != "failure_injection"]),
            "contact_record": deepcopy(world.contacts.get(entity, {})),
            "consent_record": deepcopy(world.consents.get(entity, {})),
            "suppression_snapshot": deepcopy(world.suppression_snapshots.get(entity, {})),
            "available_template_ids": deepcopy(case.get("available_template_ids", [])),
            "approved_templates": {key: deepcopy(world.templates[key])
                                   for key in case.get("available_template_ids", []) if key in world.templates},
            "verified_requirements": sorted(requirements), "evidence_refs": refs,
            "consent_verified": world.contacts.get(entity, {}).get("consent_verified") is True and not world.is_suppressed(entity),
            "authority_verified": facts.get("authority_verified") is True or facts.get("customer_authority_verified") is True}


@dataclass
class _Case:
    source: dict
    instance: Any
    consumed: list = field(default_factory=list)
    commands_done: set = field(default_factory=set)
    attempts: int = 0
    retries: int = 0
    outcome: str = "ready"
    last_progress: tuple | None = None
    retry_key: str | None = None
    attempt_events: dict = field(default_factory=dict)
    attempt_keys: dict = field(default_factory=dict)
    resume_due: str | None = None


class _Runner:
    def __init__(self, db, pack, world, decide, workflows, options, corrections, report):
        self.db, self.pack, self.world, self.decide = db, pack, world, decide
        self.workflows, self.options, self.corrections, self.report = workflows, options, corrections, report
        self.cases = {}
        self.ingested, self.exported = set(), set()
        self.activity = 0

    def options_for(self, state):
        return {**self.options, "workflow": self.workflows[state.source["workflow_id"]]}

    def dependency_ready(self, dependency):
        if dependency.startswith("signal:"):
            return dependency[7:] in self.ingested
        _, case_id, action = dependency.split(":")
        state = self.cases[case_id]
        return any(record["status"] != "blocked" and
                   any(consumer_id == state.instance.id and consumer["consumer_decision"]["action"] == action
                       for consumer_id, consumer in record["consumers"].items())
                   for record in self.world.dispatches.values())

    def drain(self):
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
                    if state.outcome in {"waiting_on_evidence", "compliance_blocked", "policy_blocked"}:
                        state.outcome = "ready"
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

    async def call(self, state, function, **kwargs):
        self.drain()
        try:
            return await function(self.db, **kwargs)
        except (SandboxCoordinatorError, SandboxApprovalError) as exc:
            expected = state.source.get("expected_service_rejection", {})
            if (expected.get("call") == function.__name__ and expected.get("expects") == str(exc) and
                expected.get("exception_type", type(exc).__name__) == type(exc).__name__):
                self.report.service_rejections.append({"case_id": state.source["id"], "call": function.__name__,
                    "exception_type": type(exc).__name__, "message": str(exc), "expected": True})
                state.outcome = "service_rejection"
                self.activity += 1
                return None
            raise HarnessDefect(f"{function.__name__}: {type(exc).__name__}: {exc}") from exc

    def record(self, state, event):
        if event is None or event.event_id in self.exported:
            return None
        pair = export_pair(event)
        self.exported.add(event.event_id)
        self.activity += 1
        state.outcome = classify_outcome(pair[0])
        if state.outcome == "ready":
            fingerprint = hashlib.sha256(json.dumps(pair[0]["evidence_refs"], sort_keys=True).encode()).hexdigest()
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
        self.report.outcomes[state.source["id"]] = state.outcome
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
        return await self.call(state, resolve_sandbox_approval, approval_id=row.id,
            resolution_key=key, event_schema=self.options["event_schema"],
            workflow=self.workflows[state.source["workflow_id"]], **params)

    async def controls(self) -> AsyncIterator[tuple[dict, dict]]:
        """Dispatch one command/service call at a time, then globally re-scan."""
        while True:
            self.drain()
            changed = False
            for state in self.cases.values():
                if state.resume_due:
                    decision_id = state.resume_due
                    state.resume_due = None
                    row = await self.approval(state, decision_id)
                    event = await self.call(state, resume_sandbox_approval, approval_id=row.id, **self.options_for(state))
                    pair = self.record(state, event)
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
                            state.outcome = "retry_budget_exhausted"
                            self.report.outcomes[state.source["id"]] = state.outcome
                            break
                        state.retries += 1
                        if row is not None:
                            event = await self.call(state, resume_sandbox_approval, approval_id=row.id,
                                                    retry=True, **self.options_for(state))
                        else:
                            if state.attempts >= MAX_DECISIONS_PER_CASE:
                                state.outcome = "decision_budget_exhausted"
                            else:
                                state.retry_key = f"{failed.idempotency_key}:retry:{command['id']}"
                                state.outcome = "ready"
                            self.report.outcomes[state.source["id"]] = state.outcome
                            break
                    pair = self.record(state, event)
                    if pair:
                        yield pair
                    break
                if changed:
                    break
            if self.report.harness_defect or self.report.environment_failure or not changed:
                return

    async def advance(self, state):
        self.drain()
        if state.outcome != "ready":
            return None
        if state.attempts >= MAX_DECISIONS_PER_CASE:
            state.outcome = "decision_budget_exhausted"
            self.report.outcomes[state.source["id"]] = state.outcome
            return None
        state.attempts += 1
        attempt = next((a for a in state.source.get("attempts", []) if a["ordinal"] == state.attempts), {})
        # Every driver-owned operation completes before the coordinator is called.
        try:
            context = evidence_context(state.source, state.consumed, self.world)
            context.update(case_id=state.source["id"], current_state=state.instance.current_state,
                           workflow=deepcopy(self.workflows[state.source["workflow_id"]]),
                           canonical_agents=self.options["canonical_agents"], canonical_actions=self.options["canonical_actions"],
                           canonical_handoffs=self.options["canonical_handoffs"],
                           policy=deepcopy(self.options["policy"]), integration_registry=deepcopy(self.options["integration_registry"]))
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
        except Exception as exc:
            raise HarnessDefect(f"decision preparation: {type(exc).__name__}: {exc}") from exc
        event = await self.call(state, coordinate_sandbox_action, workflow_instance_id=state.instance.id,
            expected_version=attempt.get("expected_version", state.instance.version), idempotency_key=key,
            synthetic_input=snapshot, decide=callback, **self.options_for(state))
        if attempt.get("ref"):
            state.attempt_keys[attempt["ref"]] = key
            if event is not None:
                state.attempt_events[attempt["ref"]] = event.id
        return self.record(state, event)


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
    corrections = deepcopy(founder_resolutions or [])
    pack = deepcopy(fixture_pack)
    try:
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
            runner.drain()
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
            if pending_commands:
                report.failures.append({"case_id": case_id, "outcome": "unconsumed_control_command",
                                        "commands": pending_commands, "last_outcome": state.outcome})
                if state.outcome not in {"decision_budget_exhausted", "retry_budget_exhausted"}:
                    report.outcomes[case_id] = "unconsumed_control_command"
            if state.outcome in {"decision_budget_exhausted", "retry_budget_exhausted"}:
                report.failures.append({"case_id": case_id, "outcome": state.outcome})
            if pending_signals:
                report.failures.append({"case_id": case_id, "outcome": "unconsumed_signal",
                                        "signals": pending_signals})
            if pending_signals or pending_commands:
                report.pending[case_id] = {"signals": pending_signals, "commands": pending_commands,
                                           "reason": "runtime_precondition_unsatisfied"}
    except HarnessDefect as exc:
        report.harness_defect = {"type": type(exc).__name__, "message": str(exc)}
        raise
    except (KeyError, TypeError, ValueError) as exc:
        report.harness_defect = {"type": type(exc).__name__, "message": str(exc)}
        raise HarnessDefect(str(exc)) from exc
