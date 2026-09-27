"""Canonical Stage B-III development paths; no live or scoreable run."""

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import select

from app.agents.company_os_integration import evaluate_integration_capability
from app.config import settings
from app.models.company_os_sandbox import CompanyOSSandboxApproval, CompanyOSSandboxEvent
from app.services.company_os_sandbox_approval_service import (
    SandboxApprovalError,
    resolve_sandbox_approval,
    resume_sandbox_approval,
)
from app.services.company_os_sandbox_coordinator import coordinate_sandbox_action
from app.services.company_os_sandbox_service import create_sandbox_run, create_workflow_instance
from app.services.company_os_stage2_inputs import load_stage2_inputs
from app.services.company_os_synthetic_adapters import SyntheticAdapter

ROOT = Path(__file__).resolve().parents[1] / "fixtures/company_os/acqivo"


def load(path):
    return json.loads((ROOT / path).read_text())


MANIFEST = load("MANIFEST.json")
FIXTURES = load("simulations/stage2_synthetic_input_fixtures.json")
POLICY = load("policy/approval_policy.json")
REGISTRY = load("integrations/stage2_sandbox_registry.json")
SCHEMA = load("schemas/sandbox_event.schema.json")
WORKFLOWS = {p.stem: json.loads(p.read_text()) for p in (ROOT / "workflows").glob("*.json")}
AGENTS = sorted({w["owner_agent"] for w in WORKFLOWS.values()})
ACTIONS = sorted({t["action"] for w in WORKFLOWS.values() for t in w["transitions"]})
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
PROSPECT_CLEAN = [
    "score_against_icp", "qualify_if_score_7_plus", "draft_personalized_outreach",
    "apply_existing_approved_sequence", "send_outreach", "classify_reply",
    "qualify_positive_reply", "book_or_handoff_meeting",
]
RECOVERY = [
    "stop_retries_after_safe_limit_log_failure_and_preserve_queue", "review_provider_recovery",
    "approve_resume_on_same_verified_provider", "resume_preserved_queue_within_original_approval",
]


@pytest.fixture(autouse=True)
def sandbox(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_mode", True)


def test_manifest_hashes_and_source_identity():
    assert re.fullmatch(r"[0-9a-f]{40}", MANIFEST["source_commit"])
    assert MANIFEST["source_commit"] == "7eafeeb63e6291f85e7660447740658a3771ace5"
    assert "source_state" not in MANIFEST and "source_head" not in MANIFEST
    expected = {"COMPLIANCE_POLICY.md", "policy/approval_policy.json",
                "integrations/stage2_sandbox_registry.json", "schemas/sandbox_event.schema.json",
                "simulations/stage2_synthetic_input_fixtures.json"}
    expected |= {f"workflows/{name}.json" for name in (
        "prospect_to_meeting", "customer_onboarding", "missed_inquiry_recovery",
        "estimate_followup", "stale_lead_reactivation", "integration_failure_recovery")}
    assert set(MANIFEST["files"]) == expected
    assert {str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file()} == expected | {"MANIFEST.json"}
    for path, record in MANIFEST["files"].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == record["sha256"], path
        assert record["source_path"] == str(Path(MANIFEST["source_repo"]) / path)
    assert MANIFEST["compliance_policy_version"] == "0.1.0"
    assert "0.1.0" in (ROOT / "COMPLIANCE_POLICY.md").read_text()
    assert MANIFEST["compliance_policy_sha256"] == MANIFEST["files"]["COMPLIANCE_POLICY.md"]["sha256"]


@pytest.mark.parametrize("use", ["acquisition_email", "transactional_email"])
def test_sendgrid_overlay_permits_synthetic_execute(use):
    result = evaluate_integration_capability(REGISTRY, integration="sendgrid", phase="execute", requested_use=use)
    assert result.allowed and result.mode == "sandbox"
    assert REGISTRY["integrations"]["sendgrid"]["external_writes"] is True


class PathDriver:
    def __init__(self, db):
        self.db = db
        self.sequence = 0
        self.registry = deepcopy(REGISTRY)
        self.world = load_stage2_inputs(
            FIXTURES, self.registry, FIXTURES["approved_templates"], run_id="canonical-paths",
            compliance_policy={"version": MANIFEST["compliance_policy_version"],
                               "sha256": MANIFEST["compliance_policy_sha256"]},
        )
        self.adapter = SyntheticAdapter(self.world, "mail")
        self.adapter.execute = AsyncMock(wraps=self.adapter.execute)

    async def start(self):
        self.run = await create_sandbox_run(self.db, run_id=self.world.run_id, company_slug="acqivo",
            protocol_version="0.1.0", input_manifest={"canonical_manifest": MANIFEST})
        return self

    async def instance(self, workflow, entity, state=None):
        definition = WORKFLOWS[workflow]
        return await create_workflow_instance(self.db, sandbox_run_id=self.run.id,
            workflow_id=workflow, entity_type=definition["entity_type"], entity_id=entity,
            initial_state=state or definition["states"][0])

    def options(self, instance):
        return dict(workflow=WORKFLOWS[instance.workflow_id], policy=POLICY,
                    integration_registry=self.registry, event_schema=SCHEMA,
                    canonical_agents=AGENTS, canonical_handoffs=AGENTS, canonical_actions=ACTIONS,
                    synthetic_adapters={"sendgrid": self.adapter})

    def decision(self, instance, action):
        transition = next(t for t in WORKFLOWS[instance.workflow_id]["transitions"]
                          if t["from"] == instance.current_state and t["action"] == action)
        integration = None
        if transition.get("integration"):
            integration = {"name": transition["integration"], "phase": "execute",
                           "use": "acquisition_email", "message": {
                               "template_id": FIXTURES["approved_templates"][0]["template_id"], "fills": {}}}
        return {"action": action, "state_after": transition["to"], "risk_level": transition["risk_level"],
                "agent_type": WORKFLOWS[instance.workflow_id]["owner_agent"], "handoff_to": "orchestrator",
                "integration": integration, "policy_intent": {
                    "action_type": action, "risk_level": transition["risk_level"],
                    "external_write": integration is not None,
                    **({"integration_mode": "sandbox"} if integration else {})}}

    async def step(self, instance, action, *, output=None, snapshot=None, decide=None):
        self.sequence += 1
        transition = next(t for t in WORKFLOWS[instance.workflow_id]["transitions"]
                          if t["from"] == instance.current_state and t["action"] == action)
        classification = {"prospect_to_meeting": "acquisition", "integration_failure_recovery": "failure_recovery"}.get(
            instance.workflow_id, instance.workflow_id)
        inputs = {"primary_classification": classification, "evidence_refs": [f"sandbox://fixture/{instance.entity_id}"],
                  "verified_requirements": transition.get("requirements", []),
                  "approval_decision_id": f"APR-{self.sequence}", **(snapshot or {})}
        event = await coordinate_sandbox_action(self.db, workflow_instance_id=instance.id,
            expected_version=instance.version, idempotency_key=f"delivery-{self.sequence}",
            synthetic_input=inputs, decide=decide or AsyncMock(return_value=output or self.decision(instance, action)),
            **self.options(instance))
        self.validate(event)
        return event

    def validate(self, event):
        VALIDATOR.validate(event.payload)
        assert event.payload["external_side_effect"] is False

    async def approval(self, instance, request, *, status="approved"):
        approval = await self.db.scalar(select(CompanyOSSandboxApproval).where(
            CompanyOSSandboxApproval.request_event_id == request.id))
        event = await resolve_sandbox_approval(self.db, approval_id=approval.id, status=status,
            founder_id="synthetic-founder", founder_minutes=2, resolution_key=f"resolve-{approval.id}",
            event_schema=SCHEMA, workflow=WORKFLOWS[instance.workflow_id])
        self.validate(event)
        return approval

    async def resume(self, instance, approval):
        event = await resume_sandbox_approval(self.db, approval_id=approval.id, **self.options(instance))
        self.validate(event)
        return event

    async def path(self, instance, actions):
        events = []
        for action in actions:
            event = await self.step(instance, action)
            if event.event_type == "approval_requested":
                before = self.adapter.execute.call_count
                approval = await self.approval(instance, event)
                event = await self.resume(instance, approval)
                assert event.payload["autonomy"]["class"] == "A1"
                if not event.native_decision["integration"]:
                    assert self.adapter.execute.call_count == before
            assert event.result == "executed_in_sandbox", event.payload
            events.append(event)
        return events

    def queue(self, origin):
        item = self.world.queue["io-syn-01"]
        assert (item["origin_workflow_id"], item["origin_entity_id"]) == (origin.workflow_id, origin.entity_id)
        return item


async def driver(db):
    return await PathDriver(db).start()


def terminal(instance, events):
    assert instance.terminal
    assert instance.current_state in WORKFLOWS[instance.workflow_id]["terminal_states"]
    assert events[-1].event_type == "terminal_outcome"
    assert sum(e.event_type == "terminal_outcome" for e in events) == 1


async def test_prospect_to_meeting_clean_a0_path(async_db_session):
    d = await driver(async_db_session)
    instance = await d.instance("prospect_to_meeting", "p-syn-01")
    events = await d.path(instance, PROSPECT_CLEAN)
    terminal(instance, events)
    assert instance.current_state == "booked"
    assert {e.payload["autonomy"]["class"] for e in events} == {"A0"}
    assert len(d.world.outcomes) == d.adapter.execute.call_count == 1


async def test_customer_onboarding_then_missed_inquiry_fulfillment(async_db_session):
    d = await driver(async_db_session)
    customer = await d.instance("customer_onboarding", "c-syn-01")
    events = await d.path(customer, [t["action"] for t in WORKFLOWS[customer.workflow_id]["transitions"][:8]])
    terminal(customer, events)
    assert customer.current_state == "active"
    assert sum(e.payload["autonomy"]["class"] == "A1" for e in events) == 2
    lead = await d.instance("missed_inquiry_recovery", "l-syn-01")
    events = await d.path(lead, ["verify_source_channel_and_customer_policy", "draft_recovery_response",
        "verify_approved_template_and_channel", "send_recovery_message", "record_reply",
        "qualify_using_customer_rules", "route_to_authoritative_next_step"])
    terminal(lead, events)
    assert lead.current_state == "routed"
    assert len(d.world.outcomes) == 1


@pytest.mark.parametrize("choice", ["approved", "rejected", "suppressed"])
async def test_red_stop_resume_or_nonapprovable_block(async_db_session, choice):
    d = await driver(async_db_session)
    instance = await d.instance("prospect_to_meeting", "p-syn-02")
    await d.path(instance, PROSPECT_CLEAN[:3])
    request = await d.step(instance, "request_first_campaign_approval")
    assert request.event_type == "approval_requested"
    assert instance.current_state == "outreach_drafted" and d.adapter.execute.call_count == 0
    approval = await d.approval(instance, request, status="rejected" if choice == "rejected" else "approved")
    if choice == "rejected":
        with pytest.raises(SandboxApprovalError, match="does not permit"):
            await d.resume(instance, approval)
        assert not d.world.outcomes and d.adapter.execute.call_count == 0
        return
    event = await d.resume(instance, approval)
    assert instance.current_state == "approval_pending"
    assert await d.resume(instance, approval) is event
    await d.path(instance, ["founder_approves"])
    if choice == "suppressed":
        output = d.decision(instance, "send_outreach")
        output["policy_intent"].update(limit_name="max_discount_percent", requested_total=20)
        request = await d.step(instance, "send_outreach", output=output)
        approval = await d.approval(instance, request)
        d.world.receive("opt_out", "stop-after-approval", {"entity_id": instance.entity_id})
        event = await d.resume(instance, approval)
        assert_block(event)
        assert event.payload["metadata"]["override_attempt"] is True
        assert event.payload["compliance"]["override_attempt"] == {"decision_id": approval.decision_id, "status": "approved"}
        assert instance.current_state == "ready_to_send" and d.adapter.execute.call_count == 0
        return
    events = await d.path(instance, PROSPECT_CLEAN[4:])
    terminal(instance, events)
    assert len(d.world.outcomes) == d.adapter.execute.call_count == 1


async def test_failure_recovery_one_effect_and_own_terminal_completions(async_db_session):
    d = await driver(async_db_session)
    origin = await d.instance("prospect_to_meeting", "p-syn-03", "ready_to_send")
    facts = d.queue(origin)
    assert facts["original_adapter_key"] is None
    assert (origin.workflow_id, origin.entity_id, "send_outreach") in d.world.action_failures
    failure = await d.step(origin, "send_outreach")
    assert failure.event_type == "failure_detected"
    assert failure.payload["metadata"]["dispatch_attempted"] is True
    assert failure.payload["metadata"]["adapter_idempotency_key"] == facts["original_adapter_key"]
    assert (origin.workflow_id, origin.entity_id, "send_outreach") not in d.world.action_failures
    await async_db_session.commit()
    different = d.decision(origin, "send_outreach")
    different["action"] = "record_opt_out"
    different["integration"]["message"] = {"template_id": "different-content", "fills": {"body": "changed"}}
    decide = AsyncMock(return_value=different)
    reconciled = await d.step(origin, "send_outreach", decide=decide)
    decide.assert_not_called()
    assert reconciled.native_decision == failure.native_decision
    assert reconciled.payload["metadata"]["reconciled"] is True
    assert reconciled.payload["evidence_refs"][-1] == next(iter(d.world.outcomes))
    events = await d.path(origin, PROSPECT_CLEAN[5:])
    terminal(origin, events)
    recovery = await d.instance("integration_failure_recovery", "io-syn-01")
    events = await d.path(recovery, RECOVERY[:-1])
    events.append(await d.step(recovery, RECOVERY[-1]))
    terminal(recovery, events)
    assert recovery.current_state == "completed"
    assert events[-1].native_decision["action"] == RECOVERY[-1]
    assert events[-1].payload["metadata"]["recipient_id"] == "p-syn-03"
    assert events[-1].payload["entity_id"] == "io-syn-01"
    assert len(d.world.outcomes) == d.adapter.execute.call_count == 1
    assert d.world.queue["io-syn-01"]["status"] == "sent"
    dispatch_id = reconciled.payload["metadata"]["dispatch_id"]
    completions = [e for e in await async_db_session.scalars(select(CompanyOSSandboxEvent))
                   if e.result == "executed_in_sandbox" and e.payload.get("metadata", {}).get("dispatch_id") == dispatch_id]
    assert {e.workflow_instance_id for e in completions} == {origin.id, recovery.id}
    assert len(completions) == 2


def assert_block(event):
    VALIDATOR.validate(event.payload)
    assert (event.risk_level, event.result, event.event_type) == ("RED", "blocked", "transition_attempted")
    assert event.payload["approval"] is None
    assert event.payload["compliance"]["approvable"] is False
    assert event.payload["compliance"]["failed_rule_ids"]
    assert event.state_before == event.state_after


@pytest.mark.parametrize("scenario", ["mismatched_recipient", "shared_address", "unknown_stop", "non_email", "queued_stop", "foreign_queue"])
async def test_canonical_negative_paths_fail_closed(async_db_session, scenario):
    d = await driver(async_db_session)
    instance = await d.instance("prospect_to_meeting", "p-syn-01", "ready_to_send")
    snapshot = {}
    action = "send_outreach"
    if scenario == "mismatched_recipient":
        d.world.ingest_opt_out({"entity_id": "p-syn-01"})
        snapshot["recipient_id"] = "p-syn-02"
    elif scenario == "shared_address":
        contact = deepcopy(d.world.contacts["p-syn-02"])
        contact["address"] = d.world.contacts["p-syn-01"]["address"].upper()
        d.world.set_contact("p-syn-02", contact)
        d.world.receive("reply", "shared-stop", {"entity_id": "p-syn-02", "body": "STOP"})
    elif scenario == "unknown_stop":
        contact = d.world.contacts.pop("p-syn-01")
        d.world.receive("mail", "unknown-stop", {"entity_id": "p-syn-01", "body": "STOP"})
        assert "entity:p-syn-01" in d.world.pending_opt_outs
        d.world.set_contact("p-syn-01", contact)
    elif scenario == "non_email":
        d.registry["integrations"]["sendgrid"]["desired_use"].append("sms")
    else:
        origin = await d.instance("prospect_to_meeting", "p-syn-03", "ready_to_send")
        d.queue(origin)
        instance = await d.instance("integration_failure_recovery",
                                    "other-operation" if scenario == "foreign_queue" else "io-syn-01", "ready_to_resume")
        action = RECOVERY[-1]
        if scenario == "queued_stop":
            d.world.receive("opt_out", "queued-stop", {"address": d.world.contacts["p-syn-03"]["address"]})
    decision = d.decision(instance, action)
    if scenario == "non_email":
        decision["integration"]["use"] = "sms"
    event = await d.step(instance, action, output=decision, snapshot=snapshot)
    assert_block(event)
    assert d.adapter.execute.call_count == 0 and not d.world.outcomes
    assert instance.version == 0
