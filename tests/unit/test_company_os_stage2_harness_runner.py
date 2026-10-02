"""Infrastructure-only driver proof. All decisions/transports are deterministic mocks.

These are small synthetic test scenarios, never smoke/scoreable fixture runs.
The coordinator, approval service, persistence and dispatch ledger are real.
"""
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

import app.services.company_os_stage2_harness_runner as harness
from app.agents.base_agent import BasePolsiaAgent
from app.agents.company_os_stage2_provider import (
    CompanyOSDecisionMethodDefect,
    CompanyOSMalformedOutputError,
    CompanyOSTransportError,
    OwnerRoutedProvider,
    stage2_decision,
)
from app.config import settings
from app.models.company_os_sandbox import CompanyOSSandboxApproval, CompanyOSSandboxEvent
from app.services.company_os_sandbox_approval_service import (
    resolve_sandbox_approval,
    resume_sandbox_approval,
)
from app.services.company_os_sandbox_coordinator import SandboxCoordinatorError
from app.services.company_os_sandbox_service import create_sandbox_run, create_workflow_instance
from app.services.company_os_stage2_inputs import build_stage2_snapshot
from app.services.company_os_synthetic_adapters import SyntheticAdapter, SyntheticWorld
from scripts.stage2_harness_export import DeterministicMockProvider
from tests.unit.test_company_os_stage2_required_paths import FIXTURES, REGISTRY, SCHEMA, WORKFLOWS

POLICY = {"red_action_types": ["pricing_change"], "hard_denies": ["bypass_opt_out"],
          "non_approvable_blocks": [], "limits": {"max_discount_percent": 10}}
WORKFLOW = {
    "id": "prospect_to_meeting", "entity_type": "prospect", "owner_agent": "email_outreach",
    "states": ["research", "scored", "sent", "done"], "terminal_states": ["done"],
    "transitions": [
        {"from": "research", "to": "scored", "action": "score_against_icp", "risk_level": "GREEN"},
        {"from": "scored", "to": "research", "action": "revisit", "risk_level": "GREEN"},
        {"from": "scored", "to": "sent", "action": "send_outreach", "risk_level": "YELLOW", "integration": "sendgrid"},
        {"from": "sent", "to": "done", "action": "close", "risk_level": "GREEN"},
        {"from": "research", "to": "done", "action": "finish", "risk_level": "GREEN"},
    ],
}
ACTIONS = ["score_against_icp", "revisit", "send_outreach", "close", "finish", "pricing_change", "bypass_opt_out",
           "qualify_if_score_7_plus", "draft_personalized_outreach", "apply_existing_approved_sequence",
           "classify_reply", "qualify_positive_reply", "book_or_handoff_meeting"]


def native(action="score_against_icp", state="scored", risk="GREEN", integration=None, **intent):
    return {"action": action, "state_after": state, "risk_level": risk, "agent_type": "email_outreach",
            "handoff_to": "orchestrator", "integration": integration,
            "policy_intent": {"action_type": action, "risk_level": risk, "external_write": integration is not None,
                              **({"integration_mode": "sandbox"} if integration else {}), **intent}}


def send(**intent):
    return native("send_outreach", "sent", "YELLOW", {
        "name": "sendgrid", "phase": "execute", "use": "acquisition_email",
        "message": {"template_id": FIXTURES["approved_templates"][0]["template_id"], "fills": {}},
    }, **intent)


def pack():
    result = {k: deepcopy(v) for k, v in FIXTURES.items() if k != "cases"}
    case = deepcopy(FIXTURES["cases"][0])
    case["id"] = "one"
    case["inputs"] = case["inputs"][:1]
    result.update(cases=[case], infrastructure_only=True)
    return result


def resolution(status="approved", decision_id="D", correction=None):
    return {"case_id": "one", "decision_id": decision_id, "status": status,
            "founder_id": "synthetic-founder", "founder_minutes": 2,
            "corrected_decision": correction, "manual_evidence_ref": None}


def approval_controls(ordinal=1, decision_id="D"):
    return {"one": {"attempts": [{"ref": "first", "ordinal": ordinal, "decision_id": decision_id}],
                    "control_commands": [{"id": "resolve", "command_type": "founder_resolution",
                        "decision_id": decision_id, "precondition": {"type": "approval_pending", "decision_id": decision_id}}]}}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_mode", True)
    # Even a mistakenly selected real structured path cannot spawn a process.
    monkeypatch.setattr("app.agents.base_agent.subprocess.run", lambda *a, **k: pytest.fail("real subprocess forbidden"))


class Run:
    def __init__(self, db, decisions, *, fixtures=None, controls=None, corrections=None, workflow=None, provider=None):
        self.db = db
        self.pack = fixtures or pack()
        self.workflows = {"prospect_to_meeting": deepcopy(workflow or WORKFLOW)}
        self.world = build_stage2_snapshot(self.pack, REGISTRY, self.pack["approved_templates"],
            run_id="harness-test", compliance_policy={"version": "0.1.0", "sha256": "a" * 64})
        self.adapter = SyntheticAdapter(self.world, "mail")
        self.provider = provider or DeterministicMockProvider({"one": decisions})
        self.report = harness.HarnessReport()
        self.controls, self.corrections = controls or {}, corrections or []
        self.options = dict(workflows=self.workflows, policy=POLICY, integration_registry=REGISTRY,
            event_schema=SCHEMA, canonical_agents=["email_outreach", "orchestrator"],
            canonical_handoffs=["orchestrator"], canonical_actions=ACTIONS,
            synthetic_adapters={"sendgrid": self.adapter})
        self.pairs = []

    async def run(self):
        async for pair in harness.run_stage2_fixture_pack(self.db, fixture_pack=self.pack,
            run_id=self.world.run_id, world=self.world, decide=self.provider, report=self.report,
            driver_controls=self.controls, founder_resolutions=self.corrections, **self.options):
            await self.db.commit()
            self.pairs.append(pair)
        return [p for p, _ in self.pairs]

    async def rows(self):
        return list(await self.db.scalars(select(CompanyOSSandboxEvent).order_by(CompanyOSSandboxEvent.sequence)))


async def test_native_export_matches_persisted_rows_and_literal_ids(async_db_session):
    r = Run(async_db_session, [native(), send(), native("close", "done")])
    events = await r.run()
    rows = await r.rows()
    assert len(events) == 3
    assert [e["sequence"] for e in events] == [1, 2, 3]
    assert events == [row.payload for row in rows]
    assert r.report.provider == "deterministic_mock"
    assert r.report.outcomes == {"one": "terminal"}
    for row, (_, evidence) in zip(rows, r.pairs, strict=True):
        assert evidence == {"event_id": row.event_id, "native_decision": row.native_decision,
            "native_failure": row.native_failure, "handoff_to": row.native_decision["handoff_to"]}
    assert len(r.world.outcomes) == 1


async def test_evidence_context_keys_on_contact_records_recipient_id_not_entity_id(async_db_session):
    """P13B-01: build_stage2_snapshot stores contact/consent/suppression under
    the contact record's own recipient_id (company_os_stage2_inputs.py), not
    the case's entity_id -- 6 of 9 dev-pack cases differ between the two
    (e.g. sandbox-customer-fulfillment: entity_id "c-syn-01" vs. recipient_id
    "l-syn-01"). Keying on entity_id would silently yield empty contact/
    consent/suppression evidence and consent_verified=False for those cases.
    """
    fixtures = {k: deepcopy(v) for k, v in FIXTURES.items() if k != "cases"}
    case = deepcopy(FIXTURES["cases"][1])
    assert case["id"] == "sandbox-customer-fulfillment"
    assert case["entity_id"] != case["contact_record"]["recipient_id"]
    fixtures["cases"] = [case]
    world = build_stage2_snapshot(fixtures, REGISTRY, fixtures["approved_templates"],
        run_id="evidence-context-test", compliance_policy={"version": "0.1.0", "sha256": "a" * 64})
    context = await harness.evidence_context(case, [], world, REGISTRY, async_db_session)
    assert context["contact_record"] == world.contacts[case["contact_record"]["recipient_id"]]
    assert context["consent_record"]["recipient_id"] == case["contact_record"]["recipient_id"]
    assert context["consent_verified"] is True
    # Keying on entity_id instead would find nothing at all.
    assert case["entity_id"] not in world.contacts


async def test_reply_after_send_not_consumed_early(async_db_session):
    fixtures = pack()
    fixtures["cases"][0]["inputs"].append({"delivery_id": "reply", "type": "reply",
        "after": ["milestone:one:send_outreach"], "facts": {"text": "yes", "meeting_confirmed": True}})
    seen = []
    provider = DeterministicMockProvider({"one": [native(), send(), native("close", "done")]})
    original = provider.prepare

    def prepare(context):
        seen.append([s["delivery_id"] for s in context["signals"]])
        return original(context)
    provider.prepare = prepare
    r = Run(async_db_session, [], fixtures=fixtures, provider=provider)
    await r.run()
    assert "reply" not in seen[0] and "reply" not in seen[1]
    assert "reply" in seen[2]


async def test_milestone_evidence_visible_on_next_decision_after_persist(async_db_session):
    await test_reply_after_send_not_consumed_early(async_db_session)


async def test_prospect_to_meeting_full_chain_uses_genuine_derived_requirements(async_db_session):
    """P13B-02: verified_requirements must come from real per-workflow
    predicates (REQUIREMENT_RULES), not a fake "any fact literally True" rule.
    Drives the real prospect_to_meeting.json transition chain end to end,
    proving every real requirement string on that chain gets satisfied by
    genuine evidence the fixture supplies -- not a workflow with no
    requirements at all, and not a same-named fact flag.
    """
    workflow = WORKFLOWS["prospect_to_meeting"]
    fixtures = {k: deepcopy(v) for k, v in FIXTURES.items() if k != "cases"}
    case = deepcopy(FIXTURES["cases"][0])
    case["id"] = "one"
    # sending identity approved (P13-REV-02) now requires the fact to match
    # the registry's own real sandbox sender identity, not any nonempty
    # string; the daily/follow-up limit requirement now independently checks
    # both bounds against their own usage/limit evidence.
    case["inputs"][0]["facts"].update(confidence=0.9, personalization_evidence="prior-project-notes",
        daily_sends_used=1, daily_send_limit=50, follow_ups_sent=0, follow_up_limit=3,
        approved_sequence_id=REGISTRY["integrations"]["sendgrid"]["sandbox_sender"]["sender_id"])
    fixtures.update(cases=[case], infrastructure_only=True)
    decisions = [
        native("score_against_icp", "scored"),
        native("qualify_if_score_7_plus", "qualified"),
        native("draft_personalized_outreach", "outreach_drafted"),
        native("apply_existing_approved_sequence", "ready_to_send", "YELLOW"),
        send(),
        native("classify_reply", "replied"),
        native("qualify_positive_reply", "meeting_ready"),
        native("book_or_handoff_meeting", "booked", "YELLOW"),
    ]
    r = Run(async_db_session, decisions, fixtures=fixtures, workflow=workflow)
    events = await r.run()
    assert [e["action"] for e in events] == [d["action"] for d in decisions]
    assert events[-1]["event_type"] == "terminal_outcome"
    assert r.report.outcomes["one"] == "terminal"
    assert not r.report.harness_defect and not r.report.environment_failure


def test_requirement_rules_cover_every_string_in_all_in_scope_workflows():
    """P13-REV-01: the round-1 fix's "fail loudly for unmapped requirements"
    rejected ANY pack containing customer_onboarding/missed_inquiry_recovery/
    integration_failure_recovery entirely, because their real canonical
    workflow definitions (tests/fixtures/company_os/acqivo/workflows/*.json)
    have several transition-requirement strings with no REQUIREMENT_RULES
    entry -- regardless of whether a given pack's own fixture cases ever
    reach those specific transitions. Every requirement string declared
    anywhere in all six in-scope real workflow definitions must have a rule.
    """
    for workflow_id in ("prospect_to_meeting", "customer_onboarding",
                        "missed_inquiry_recovery", "estimate_followup",
                        "stale_lead_reactivation", "integration_failure_recovery"):
        workflow = WORKFLOWS[workflow_id]
        requirement_strings = {req for t in workflow["transitions"] for req in t.get("requirements", [])}
        assert requirement_strings, f"{workflow_id} fixture has no requirement strings to prove against"
        missing = requirement_strings - set(harness.REQUIREMENT_RULES)
        assert not missing, f"{workflow_id} has unmapped requirement strings: {missing}"


def test_followup_and_reactivation_requirements_use_source_evidence():
    world = SyntheticWorld("requirements", datetime(2026, 9, 25, 14, tzinfo=UTC))
    world.customers["customer"] = {"authority_verified": True,
                                   "workflow_permissions": ["missed_inquiry_recovery"],
                                   "approved_message_channels": ["email"], "approved_batch_size": 10}
    template = deepcopy(FIXTURES["approved_templates"][0])
    world.templates[template["template_id"]] = template
    rules = harness.REQUIREMENT_RULES
    inquiry = {"workflow_id": "missed_inquiry_recovery"}
    assert rules["approved customer workflow"]({"customer_id": "customer"}, world, inquiry, REGISTRY, 0)
    assert not rules["approved customer workflow"]({"customer_id": "other"}, world, inquiry, REGISTRY, 0)
    estimate = {"customer_id": "customer", "customer_authority_verified": True,
                "customer_approved_channel": "email",
                "customer_approved_followup_at": world.frozen_at.isoformat(),
                "followups_sent": 1, "approved_max_followups": 3}
    assert rules["customer-approved timing and channel"](estimate, world, {}, REGISTRY, 0)
    assert not rules["customer-approved timing and channel"](
        {**estimate, "customer_approved_channel": "sms"}, world, {}, REGISTRY, 0)
    assert rules["follow-up count within policy"](estimate, world, {}, REGISTRY, 0)
    assert not rules["follow-up count within policy"](
        {**estimate, "followups_sent": 3}, world, {}, REGISTRY, 0)
    stale_case = {"available_template_ids": [template["template_id"]]}
    assert rules["opt-out path"]({}, world, stale_case, REGISTRY, 0)
    assert not rules["opt-out path"]({}, world, {"available_template_ids": []}, REGISTRY, 0)
    batch = {"customer_id": "customer", "customer_authority_verified": True, "record_count": 5}
    assert rules["batch size approved"](batch, world, {}, REGISTRY, 0)
    assert not rules["batch size approved"]({**batch, "customer_id": "other"}, world, {}, REGISTRY, 0)
    assert not rules["batch size approved"](
        {**batch, "record_count": 11}, world, {}, REGISTRY, 0)


def test_validate_pack_accepts_real_customer_onboarding_missed_inquiry_and_recovery_workflows():
    """P13-REV-01 regression: validate_pack must accept a pack whose case
    uses one of the three previously-rejected real workflow definitions,
    proving the rejection was fixed for the whole workflow, not merely for
    the one requirement string a particular test happens to exercise.
    """
    for workflow_id, entity_type in (
        ("customer_onboarding", "customer_account"),
        ("missed_inquiry_recovery", "lead"),
        ("integration_failure_recovery", "integration_operation"),
    ):
        fixtures = pack()
        case = fixtures["cases"][0]
        case.update(workflow_id=workflow_id, entity_type=entity_type)
        harness.validate_pack(fixtures, {workflow_id: WORKFLOWS[workflow_id]}, [], REGISTRY)


async def test_customer_onboarding_full_chain_uses_genuine_derived_requirements(async_db_session):
    """P13-REV-01/02: customer_onboarding's own real requirement strings
    ("first production activation requires founder approval", "scope
    approved", "customer permissions recorded", "integration registry
    verified") each get a real derivation rule grounded in the case's own
    real authority-signal evidence (service_area/business_hours/
    workflow_permissions/approved_message_channels/escalation_contacts/
    authority_verified -- the same fields company_os_stage2_inputs.py's own
    authority handling reads) and the real integration registry, driven end
    to end through both RED founder-approval gates via the unmodified real
    customer_onboarding.json definition.
    """
    workflow = WORKFLOWS["customer_onboarding"]
    actions = [t["action"] for t in workflow["transitions"]]
    fixtures = {k: deepcopy(v) for k, v in FIXTURES.items() if k != "cases"}
    case = deepcopy(FIXTURES["cases"][1])
    assert case["id"] == "sandbox-customer-fulfillment"
    case["id"] = "one"
    fixtures.update(cases=[case], infrastructure_only=True)
    controls = {"one": {
        "attempts": [
            {"ref": "verify", "ordinal": 6, "decision_id": "D1"},
            {"ref": "activate", "ordinal": 8, "decision_id": "D2"},
        ],
        "control_commands": [
            {"id": "resolve1", "command_type": "founder_resolution", "decision_id": "D1",
             "precondition": {"type": "approval_pending", "decision_id": "D1"}},
            {"id": "resolve2", "command_type": "founder_resolution", "decision_id": "D2",
             "precondition": {"type": "approval_pending", "decision_id": "D2"}},
        ],
    }}
    corrections = [resolution("approved", "D1"), resolution("approved", "D2")]
    decisions = [
        native("send_or_prepare_intake", "intake_requested", "GREEN"),
        native("record_customer_inputs", "intake_received", "GREEN"),
        native("confirm_offer_scope", "scope_confirmed", "GREEN"),
        native("review_channels_workflows_and_authority", "permissions_review", "YELLOW"),
        native("collect_required_connection_status", "integrations_pending", "YELLOW"),
        native("verify_integrations", "baseline_pending", "RED"),
        native("capture_baseline_metrics", "ready_for_activation", "GREEN"),
        native("activate_approved_workflows", "active", "RED"),
    ]
    r = Run(async_db_session, decisions, fixtures=fixtures, controls=controls, corrections=corrections)
    r.workflows.clear()
    r.workflows["customer_onboarding"] = workflow
    r.options["canonical_actions"] = actions
    events = await r.run()
    assert r.report.outcomes["one"] == "terminal"
    assert events[-1]["event_type"] == "terminal_outcome" and events[-1]["state_after"] == "active"
    assert not r.report.harness_defect and not r.report.environment_failure


@pytest.mark.parametrize("unusable_mode", ["live", "write_limited"])
async def test_customer_onboarding_activation_never_advances_with_non_sandbox_required_integration(
        async_db_session, unusable_mode):
    """P13-REV4-01 regression, end to end: "integration registry verified"
    must not certify Stage 2 readiness for "activate_approved_workflows"
    (customer_onboarding.json's final RED transition) when the customer's
    required sendgrid connection is set to "live" or "write_limited" --
    evaluate_integration_capability(...).allowed alone would incorrectly
    pass both (WRITE_CAPABLE_MODES), but the real governed callers require
    mode == "sandbox" specifically. Drives the same real onboarding chain as
    test_customer_onboarding_full_chain_uses_genuine_derived_requirements
    above (which proves the sandbox-mode case correctly reaches "active"),
    but with the registry's sendgrid mode swapped -- this case must never
    reach "active"/"terminal" the way that one does.
    """
    workflow = WORKFLOWS["customer_onboarding"]
    actions = [t["action"] for t in workflow["transitions"]]
    fixtures = {k: deepcopy(v) for k, v in FIXTURES.items() if k != "cases"}
    case = deepcopy(FIXTURES["cases"][1])
    assert case["id"] == "sandbox-customer-fulfillment"
    case["id"] = "one"
    fixtures.update(cases=[case], infrastructure_only=True)
    controls = {"one": {
        "attempts": [
            {"ref": "verify", "ordinal": 6, "decision_id": "D1"},
            {"ref": "activate", "ordinal": 8, "decision_id": "D2"},
        ],
        "control_commands": [
            {"id": "resolve1", "command_type": "founder_resolution", "decision_id": "D1",
             "precondition": {"type": "approval_pending", "decision_id": "D1"}},
            {"id": "resolve2", "command_type": "founder_resolution", "decision_id": "D2",
             "precondition": {"type": "approval_pending", "decision_id": "D2"}},
        ],
    }}
    corrections = [resolution("approved", "D1"), resolution("approved", "D2")]
    decisions = [
        native("send_or_prepare_intake", "intake_requested", "GREEN"),
        native("record_customer_inputs", "intake_received", "GREEN"),
        native("confirm_offer_scope", "scope_confirmed", "GREEN"),
        native("review_channels_workflows_and_authority", "permissions_review", "YELLOW"),
        native("collect_required_connection_status", "integrations_pending", "YELLOW"),
        native("verify_integrations", "baseline_pending", "RED"),
        native("capture_baseline_metrics", "ready_for_activation", "GREEN"),
        native("activate_approved_workflows", "active", "RED"),
    ]
    r = Run(async_db_session, decisions, fixtures=fixtures, controls=controls, corrections=corrections)
    r.workflows.clear()
    r.workflows["customer_onboarding"] = workflow
    r.options["canonical_actions"] = actions
    non_sandbox_registry = deepcopy(REGISTRY)
    non_sandbox_registry["integrations"]["sendgrid"]["mode"] = unusable_mode
    r.options["integration_registry"] = non_sandbox_registry
    events = await r.run()
    assert r.report.outcomes["one"] != "terminal"
    assert all(event["state_after"] != "active" for event in events)
    assert not any(event["event_type"] == "terminal_outcome" for event in events)


async def test_missed_inquiry_recovery_full_chain_uses_genuine_derived_requirements(async_db_session):
    """P13-REV-01: missed_inquiry_recovery's own real requirement strings
    ("approved customer workflow", "verified writable integration") get a
    real derivation rule -- the customer's own recorded workflow_permissions
    must literally cover this workflow, and the registry must have a
    genuinely verified, write-capable integration -- driven end to end
    through the unmodified real missed_inquiry_recovery.json definition,
    including its own governed email-execute transition.
    """
    workflow = WORKFLOWS["missed_inquiry_recovery"]
    actions = [t["action"] for t in workflow["transitions"]]
    fixtures = {k: deepcopy(v) for k, v in FIXTURES.items() if k != "cases"}
    case = deepcopy(FIXTURES["cases"][0])  # prospect_to_meeting's own valid contact/consent snapshot
    case.update(id="one", workflow_id="missed_inquiry_recovery", entity_type="lead")
    case["inputs"] = [{
        "delivery_id": "authority", "type": "authority",
        "facts": {"customer_id": case["entity_id"], "authority_verified": True,
                  "workflow_permissions": ["missed_inquiry_recovery"]},
    }]
    fixtures.update(cases=[case], infrastructure_only=True)

    def recovery_send(**intent):
        return native("send_recovery_message", "sent", "YELLOW", {
            "name": "sendgrid", "phase": "execute", "use": "acquisition_email",
            "message": {"template_id": FIXTURES["approved_templates"][0]["template_id"], "fills": {}},
        }, **intent)
    decisions = [
        native("verify_source_channel_and_customer_policy", "eligibility_check", "GREEN"),
        native("draft_recovery_response", "response_drafted", "GREEN"),
        native("verify_approved_template_and_channel", "ready_to_send", "YELLOW"),
        recovery_send(),
        native("record_reply", "engaged", "GREEN"),
        native("qualify_using_customer_rules", "qualified", "GREEN"),
        native("route_to_authoritative_next_step", "routed", "YELLOW"),
    ]
    r = Run(async_db_session, decisions, fixtures=fixtures)
    r.workflows.clear()
    r.workflows["missed_inquiry_recovery"] = workflow
    r.options["canonical_actions"] = actions
    events = await r.run()
    assert r.report.outcomes["one"] == "terminal", (events[-1]['action'], events[-1]['result'], events[-1].get('error'), r.world.customers)
    assert events[-1]["event_type"] == "terminal_outcome" and events[-1]["state_after"] == "routed"
    assert not r.report.harness_defect and not r.report.environment_failure


async def test_integration_failure_recovery_full_chain_uses_genuine_derived_requirements(async_db_session):
    """P13-REV-01/02/P13-REV2-01/02: integration_failure_recovery's own real
    requirement strings are grounded in the persistent dispatch-ledger record
    its own queued_message origin facts describe (_origin_dispatch) -- never
    world.action_failures, which is popped the instant a failure is armed
    (company_os_synthetic_adapters.py:236-243) and so is already gone by the
    time this recovery case's own transitions are evaluated -- and the
    retry-count-shaped requirements ("retry policy permits another attempt",
    "provider errors continue", "provider health verified") are grounded in
    the origin instance's own genuinely persisted failure_detected events
    (origin_failures), never in dispatch-ledger status alone (P13-REV2-01/02:
    a single prepared record with zero real failures previously satisfied
    every one of these at once).

    Drives the unmodified real integration_failure_recovery.json definition
    through the driver's own advance()/drain() (the same internals
    run_stage2_fixture_pack uses each turn -- start_internal-style direct
    driving is the existing pattern this suite already uses for scenarios
    that need precise turn-by-turn control, e.g.
    test_decision_loop_covers_every_real_service_signature) against a
    dispatch-ledger record seeded exactly as a real, prior send_outreach
    failure would leave it: created (prepare_dispatch), then left at
    status="prepared" because the adapter never returns a valid receipt --
    never a fixture fact standing in for that evidence -- and against a real
    origin workflow instance that accumulates one genuinely persisted
    failure_detected event per real retry, exactly as the coordinator's own
    denial path (company_os_sandbox_coordinator.py's failure_detected
    _append call) would leave it.
    """
    workflow = WORKFLOWS["integration_failure_recovery"]
    actions = [t["action"] for t in workflow["transitions"]]
    fixtures = pack()
    case = fixtures["cases"][0]
    case.update(workflow_id="integration_failure_recovery", entity_type="integration_operation")
    recipient = case["contact_record"]["recipient_id"]
    case["inputs"] = [{"delivery_id": "queue", "type": "queued_message", "facts": {
        "entity_id": case["entity_id"], "workflow_id": "integration_failure_recovery",
        "item_id": case["entity_id"], "recipient_id": recipient,
        "origin_workflow_id": "prospect_to_meeting", "origin_entity_id": "origin-case",
        "original_action": "send_outreach"}}]
    world = build_stage2_snapshot(fixtures, REGISTRY, fixtures["approved_templates"],
        run_id="ifr-full-chain", compliance_policy={"version": "0.1.0", "sha256": "a" * 64})
    run = await create_sandbox_run(async_db_session, run_id=world.run_id, company_slug="acqivo",
        protocol_version="0.1.0", input_manifest={"provider": "deterministic_mock"})
    origin = await create_workflow_instance(async_db_session, sandbox_run_id=run.id,
        workflow_id="prospect_to_meeting", entity_type="prospect", entity_id="origin-case",
        initial_state="sent")
    world.prepare_dispatch(
        message_key="origin-msg", recipient_id=recipient, adapter_kind="mail",
        adapter_idempotency_key="origin-key-1", origin_workflow_instance_id=origin.id,
        origin_state_before="scored", origin_workflow_version=0,
        frozen_native_decision={"action": "send_outreach",
                                 "integration": {"name": "sendgrid", "phase": "execute"}},
        rendered_payload={}, consumers={}, origin_workflow_id="prospect_to_meeting", origin_entity_id="origin-case",
    )

    origin_dispatch_id = next(iter(world.dispatches.values()))["dispatch_id"]
    origin_failure_ordinal = 0

    async def persist_origin_failure():
        """Mirror the shape of a real coordinator-persisted failure_detected
        event (company_os_sandbox_coordinator.py's _append/denial path) on
        the origin instance -- never a fixture fact standing in for it. The
        recovery case's own advance() calls persist real events into this
        same sandbox_run_id too, so the next sequence number is computed
        fresh each time rather than hardcoded (the same allocation
        company_os_sandbox_service.append_sandbox_event itself uses)."""
        nonlocal origin_failure_ordinal
        origin_failure_ordinal += 1
        next_sequence = (await async_db_session.scalar(
            select(func.max(CompanyOSSandboxEvent.sequence)).where(
                CompanyOSSandboxEvent.sandbox_run_id == run.id))) or 0
        async_db_session.add(CompanyOSSandboxEvent(
            sandbox_run_id=run.id, workflow_instance_id=origin.id,
            event_id=f"origin-fail-{origin_failure_ordinal}", sequence=next_sequence + 1,
            idempotency_key=f"origin-key-1:attempt:{origin_failure_ordinal}",
            primary_classification="acquisition", event_type="failure_detected", result="failed",
            risk_level="GREEN", state_before="scored", state_after="scored", external_side_effect=False,
            payload={"action": "send_outreach", "metadata": {"gate": "integration", "dispatch_id": origin_dispatch_id,
                                                          "dispatch_attempted": True}},
            native_decision=None, native_failure={"type": "SyntheticCapabilityError"},
        ))
        await async_db_session.flush()

    async def persist_provider_recovery(label, *, provider="sendgrid", target="sandbox", attempted=True):
        """Persist an actual-success-shaped event, with variants that must
        never be mistaken for post-failure health evidence."""
        next_sequence = (await async_db_session.scalar(
            select(func.max(CompanyOSSandboxEvent.sequence)).where(
                CompanyOSSandboxEvent.sandbox_run_id == run.id))) or 0
        async_db_session.add(CompanyOSSandboxEvent(
            sandbox_run_id=run.id, workflow_instance_id=origin.id,
            event_id=f"provider-recovered-{label}", sequence=next_sequence + 1,
            idempotency_key=f"provider-recovered-{label}",
            primary_classification="acquisition", event_type="decision_produced",
            result="executed_in_sandbox", risk_level="GREEN",
            state_before="scored", state_after="scored", external_side_effect=False,
            payload={"action": "send_outreach",
                     "integration": {"name": provider, "target": target},
                     "metadata": {"gate": "allow", "dispatch_attempted": attempted}},
            native_decision={"action": "send_outreach"}, native_failure=None,
        ))
        await async_db_session.flush()

    instance = await create_workflow_instance(async_db_session, sandbox_run_id=run.id,
        workflow_id="integration_failure_recovery", entity_type="integration_operation",
        entity_id=case["entity_id"], initial_state=workflow["states"][0])
    decisions = [
        native("retry_within_safe_limit", "retry_wait", "GREEN"),
        native("stop_retries_after_safe_limit_log_failure_and_preserve_queue", "blocked", "GREEN"),
        native("review_provider_recovery", "recovery_review", "GREEN"),
        native("approve_resume_on_same_verified_provider", "ready_to_resume", "YELLOW"),
    ]
    provider = DeterministicMockProvider({"one": decisions})
    options = dict(policy=POLICY, integration_registry=REGISTRY, event_schema=SCHEMA,
        canonical_agents=["email_outreach", "orchestrator"], canonical_handoffs=["orchestrator"],
        canonical_actions=actions, synthetic_adapters={"sendgrid": SyntheticAdapter(world, "mail")})
    report = harness.HarnessReport()
    runner = harness._Runner(async_db_session, fixtures, world, provider,
        {"integration_failure_recovery": workflow}, options, [], report)
    runner.cases["one"] = harness._Case(case, instance)
    state = runner.cases["one"]
    events = []
    # One genuine failure persisted before retry_within_safe_limit: eligible
    # to retry, not yet exhausted or "repeated".
    await persist_origin_failure()
    for index, decision in enumerate(decisions):
        if index == 1:
            # A success before the *latest* failure cannot certify later
            # recovery; the second failure invalidates the earlier success.
            await persist_provider_recovery("before-latest-failure")
            await persist_origin_failure()
        if index == 3:
            origin_record = next(iter(world.dispatches.values()))
            assert await harness._post_failure_provider_health(async_db_session, origin_record) is False
            await persist_provider_recovery("wrong-provider", provider="imap")
            await persist_provider_recovery("not-attempted", attempted=False)
            await persist_provider_recovery("wrong-target", target="live")
            assert await harness._post_failure_provider_health(async_db_session, origin_record) is False
            await persist_provider_recovery("actual")
            assert await harness._post_failure_provider_health(async_db_session, origin_record) is True
        pair = await runner.advance(state)
        assert pair is not None, report.harness_defect or report.environment_failure
        events.append(pair[0])
        assert pair[0]["action"] == decision["action"]
    assert [e["state_after"] for e in events] == ["retry_wait", "blocked", "recovery_review", "ready_to_resume"]
    assert state.outcome == "ready"
    assert not report.harness_defect and not report.environment_failure


def test_origin_dispatch_requires_unambiguous_queue_binding():
    world = SyntheticWorld("origin-binding", datetime(2026, 9, 25, tzinfo=UTC))
    facts = {"entity_id": "recovery-case", "origin_workflow_id": "prospect_to_meeting",
             "origin_entity_id": "origin-case", "original_action": "send_outreach"}
    base = {"origin_workflow_id": "prospect_to_meeting", "origin_entity_id": "origin-case",
            "frozen_native_decision": {"action": "send_outreach"}}
    world.dispatches["first"] = {**base, "dispatch_id": "first"}
    world.dispatches["second"] = {**base, "dispatch_id": "second"}
    assert harness._origin_dispatch(facts, world) is None
    world.queue["recovery-case"] = {"item_id": "item", "dispatch_id": "second"}
    assert harness._origin_dispatch(facts, world) is world.dispatches["second"]


def test_verified_writable_integration_and_no_scope_expansion_predicates():
    """P13-REV-01: direct predicate proof for the two integration_failure_
    recovery/missed_inquiry_recovery requirement strings the full-chain
    tests above don't reach (both live on the final, governed email-execute
    transition). "verified writable integration" is grounded in the real
    registry, never a fixture flag; "no scope expansion" is grounded in the
    origin dispatch record and rejects a case where a second, different-
    action record for the same origin identity would indicate the recovery
    smuggled in a different action than the one that originally failed.
    """
    world = SyntheticWorld("scope-test", datetime(2026, 9, 25, tzinfo=UTC))
    facts = {"origin_workflow_id": "prospect_to_meeting", "origin_entity_id": "case-a",
             "original_action": "send_outreach"}
    assert harness.REQUIREMENT_RULES["verified writable integration"](facts, world, {}, REGISTRY, 0) is True
    assert harness.REQUIREMENT_RULES["verified writable integration"](facts, world, {}, {"integrations": {}}, 0) is False
    world.dispatches["d1"] = {"origin_workflow_id": "prospect_to_meeting", "origin_entity_id": "case-a",
                              "frozen_native_decision": {"action": "send_outreach"}, "status": "prepared"}
    assert harness.REQUIREMENT_RULES["no scope expansion"](facts, world, {}, REGISTRY, 0) is True
    # A second record for the same origin identity but a different action
    # would mean the recovery is no longer resuming the exact action that
    # failed -- that must never verify.
    world.dispatches["d2"] = {"origin_workflow_id": "prospect_to_meeting", "origin_entity_id": "case-a",
                              "frozen_native_decision": {"action": "some_other_action"}, "status": "prepared"}
    assert harness.REQUIREMENT_RULES["no scope expansion"](facts, world, {}, REGISTRY, 0) is False


def test_verified_writable_integration_rejects_live_and_write_limited_modes():
    """P13-REV4-01: "verified writable integration" previously accepted
    evaluate_integration_capability(...).allowed alone, but that evaluator's
    own WRITE_CAPABLE_MODES ({"sandbox", "write_limited", "live"}) permits
    execute in live/write_limited too -- the real governed callers
    (coordinate_sandbox_action/resume_sandbox_approval) additionally require
    mode == "sandbox" specifically for phase="execute" and explicitly deny
    mode == "live" outright. Setting the only verified, write-capable
    registry entry to "live" or "write_limited" must never verify this
    requirement, even though it still satisfies verified+external_writes
    and even .allowed on its own.
    """
    world = SyntheticWorld("scope-test-2", datetime(2026, 9, 25, tzinfo=UTC))
    facts = {}
    for unusable_mode in ("live", "write_limited"):
        wrong_mode = deepcopy(REGISTRY)
        wrong_mode["integrations"]["sendgrid"]["mode"] = unusable_mode
        assert harness.REQUIREMENT_RULES["verified writable integration"](
            facts, world, {}, wrong_mode, 0) is False


def test_verified_writable_integration_requires_matching_requested_scope():
    """P13-REV4-02: neither predicate ever supplied requested_scope, and
    evaluate_integration_capability rejects any integration with a
    configured non-null scope whenever requested_scope is None -- a
    verified sandbox integration correctly restricted to an authorized
    customer scope would be wrongly reported unavailable. requested_scope
    must be derived from the case's own trusted customer_id fact (never
    copied back from the registry's own configured scope, which would
    trivially always match and prove nothing).
    """
    world = SyntheticWorld("scope-test-3", datetime(2026, 9, 25, tzinfo=UTC))
    scoped = deepcopy(REGISTRY)
    scoped["integrations"]["sendgrid"]["scope"] = "c-syn-01"
    # The case's own customer_id fact matches the integration's configured
    # scope -- the real coordinator would allow this exact request, so
    # readiness must hold.
    assert harness.REQUIREMENT_RULES["verified writable integration"](
        {"customer_id": "c-syn-01"}, world, {}, scoped, 0) is True
    # A different customer's request against the same scoped integration
    # must never verify -- the real coordinator would reject it.
    assert harness.REQUIREMENT_RULES["verified writable integration"](
        {"customer_id": "some-other-customer"}, world, {}, scoped, 0) is False
    # No customer_id fact at all (requested_scope stays None) against a
    # scoped integration must also fail closed, matching the real evaluator.
    assert harness.REQUIREMENT_RULES["verified writable integration"](
        {}, world, {}, scoped, 0) is False


def test_qualify_requirement_rejects_below_threshold_score():
    rule = harness.REQUIREMENT_RULES["score and confidence recorded"]
    assert rule({"icp_score": 6, "confidence": 0.91}, None, {}, {}, 0) is False
    assert rule({"icp_score": 7, "confidence": 0.91}, None, {}, {}, 0) is True


def test_sending_identity_approved_requires_real_registry_sender_match():
    """P13-REV-02: "sending identity approved" was satisfied by any nonempty
    approved_sequence_id fact with no check that the sender/identity was
    actually approved by real evidence. It must now be bound to the
    registry's own real, verified sandbox sender identity.
    """
    world = SyntheticWorld("identity-test", datetime(2026, 9, 25, tzinfo=UTC))
    rule = harness.REQUIREMENT_RULES["sending identity approved"]
    real_id = REGISTRY["integrations"]["sendgrid"]["sandbox_sender"]["sender_id"]
    assert rule({"approved_sequence_id": real_id}, world, {}, REGISTRY, 0) is True
    # An arbitrary, unverified fixture-invented string must never verify.
    assert rule({"approved_sequence_id": "invented-sequence-id"}, world, {}, REGISTRY, 0) is False
    assert rule({}, world, {}, REGISTRY, 0) is False


def test_daily_send_and_follow_up_limits_independently_verified():
    """P13-REV-02: "within daily send and follow-up limits" only checked the
    daily-send pair, ignoring follow-up limits entirely -- a case with an
    approved daily allowance but an exhausted follow-up allowance verified
    anyway. Both bounds must now be independently checked against their own
    usage/limit evidence; missing evidence must leave it unverified.
    """
    world = SyntheticWorld("limits-test", datetime(2026, 9, 25, tzinfo=UTC))
    rule = harness.REQUIREMENT_RULES["within daily send and follow-up limits"]
    within_both = {"daily_sends_used": 1, "daily_send_limit": 50, "follow_ups_sent": 0, "follow_up_limit": 3}
    assert rule(within_both, world, {}, REGISTRY, 0) is True
    exhausted_follow_up = {**within_both, "follow_ups_sent": 3}
    assert rule(exhausted_follow_up, world, {}, REGISTRY, 0) is False
    missing_follow_up_evidence = {"daily_sends_used": 1, "daily_send_limit": 50}
    assert rule(missing_follow_up_evidence, world, {}, REGISTRY, 0) is False


def test_integration_registry_verified_binds_to_customers_required_channels():
    """P13-REV2-02: "integration registry verified" previously accepted ANY
    verified, non-disabled registry entry -- a verified sandbox SendGrid
    entry could certify readiness for a customer whose own required
    connection was actually disabled. It must now bind to whichever
    channels the customer's own approved_message_channels facts declare,
    checking that channel's own specific capability.
    """
    rule = harness.REQUIREMENT_RULES["integration registry verified"]
    world = SyntheticWorld("registry-test", datetime(2026, 9, 25, tzinfo=UTC))
    assert rule({"approved_message_channels": ["email"]}, world, {}, REGISTRY, 0) is True
    # The customer's required channel (email) has no verified, write-capable
    # integration serving it -- an unrelated verified entry (imap, which
    # serves reply_ingestion/support_inbox, not an email use) must never
    # substitute for the customer's own actual requirement.
    email_disabled = deepcopy(REGISTRY)
    email_disabled["integrations"]["sendgrid"]["verified"] = False
    assert rule({"approved_message_channels": ["email"]}, world, {}, email_disabled, 0) is False
    # A channel with no established use-vocabulary mapping never verifies --
    # this must fail closed, never silently pass.
    assert rule({"approved_message_channels": ["carrier_pigeon"]}, world, {}, REGISTRY, 0) is False
    assert rule({"approved_message_channels": []}, world, {}, REGISTRY, 0) is False
    assert rule({}, world, {}, REGISTRY, 0) is False
    # P13-REV3-02: verified+external_writes alone previously certified
    # readiness even when the integration's own mode cannot execute --
    # evaluate_integration_capability's mode gate (WRITE_CAPABLE_MODES =
    # {sandbox, write_limited, live}) is what actually determines execute
    # readiness; disabled/documented/read_only cannot, regardless of
    # verified/external_writes.
    for unusable_mode in ("disabled", "documented", "read_only"):
        wrong_mode = deepcopy(REGISTRY)
        wrong_mode["integrations"]["sendgrid"]["mode"] = unusable_mode
        assert rule({"approved_message_channels": ["email"]}, world, {}, wrong_mode, 0) is False
    # P13-REV4-01: `.allowed` alone (WRITE_CAPABLE_MODES) permits execute in
    # live/write_limited too, unlike disabled/documented/read_only above --
    # but the real governed callers (coordinate_sandbox_action/
    # resume_sandbox_approval) require mode == "sandbox" specifically for
    # execute and explicitly deny "live" outright. live/write_limited must
    # never verify this requirement even though .allowed alone would be True.
    for unusable_mode in ("live", "write_limited"):
        wrong_mode = deepcopy(REGISTRY)
        wrong_mode["integrations"]["sendgrid"]["mode"] = unusable_mode
        assert rule({"approved_message_channels": ["email"]}, world, {}, wrong_mode, 0) is False


def test_integration_registry_verified_requires_matching_requested_scope():
    """P13-REV4-02: neither predicate ever supplied requested_scope, and
    evaluate_integration_capability rejects any integration with a
    configured non-null scope whenever requested_scope is None. Once the
    customer's required sendgrid connection has a configured scope,
    "integration registry verified" must only hold when the case's own
    trusted customer_id fact matches it -- never when it's absent or
    belongs to a different customer.
    """
    rule = harness.REQUIREMENT_RULES["integration registry verified"]
    world = SyntheticWorld("registry-scope-test", datetime(2026, 9, 25, tzinfo=UTC))
    scoped = deepcopy(REGISTRY)
    scoped["integrations"]["sendgrid"]["scope"] = "c-syn-01"
    assert rule({"approved_message_channels": ["email"], "customer_id": "c-syn-01"},
                world, {}, scoped, 0) is True
    assert rule({"approved_message_channels": ["email"], "customer_id": "some-other-customer"},
                world, {}, scoped, 0) is False
    assert rule({"approved_message_channels": ["email"]}, world, {}, scoped, 0) is False


@pytest.mark.asyncio
async def test_origin_failure_count_is_bound_to_dispatch_not_just_action():
    origin = {"origin_workflow_instance_id": 7, "dispatch_id": "target-dispatch",
              "frozen_native_decision": {"action": "send_outreach"}}
    events = [
        SimpleNamespace(payload={"action": "send_outreach", "metadata":
            {"gate": "integration", "dispatch_id": "other-dispatch", "dispatch_attempted": True}}),
        SimpleNamespace(payload={"action": "send_outreach", "metadata":
            {"gate": "provider", "dispatch_id": "target-dispatch", "dispatch_attempted": True}}),
        SimpleNamespace(payload={"action": "send_outreach", "metadata":
            {"gate": "integration", "dispatch_id": "target-dispatch", "dispatch_attempted": True}}),
        SimpleNamespace(payload={"action": "send_outreach", "metadata":
            {"gate": "approval_resume", "dispatch_id": "target-dispatch", "dispatch_attempted": True}}),
    ]
    db = SimpleNamespace(scalars=AsyncMock(return_value=events))
    assert await harness._origin_failure_count(db, origin) == 2


@pytest.mark.asyncio
async def test_origin_failure_count_counts_recovery_instance_failures_too(async_db_session):
    """P13-REV3-01: execute_dispatch registers a recovery instance as a
    consumer of the SAME dispatch record as the origin -- its own
    coordinator/approval calls persist failure_detected events under the
    RECOVERY instance's workflow_instance_id, not the origin's. Scoping the
    count query by origin_workflow_instance_id silently dropped every
    recovery-path failure on a shared dispatch. Must count by dispatch_id
    alone, across every consumer instance.
    """
    run = await create_sandbox_run(async_db_session, run_id="cross-instance-count", company_slug="acqivo",
        protocol_version="0.1.0", input_manifest={"provider": "deterministic_mock"})
    origin = await create_workflow_instance(async_db_session, sandbox_run_id=run.id,
        workflow_id="prospect_to_meeting", entity_type="prospect", entity_id="origin-case",
        initial_state="sent")
    recovery = await create_workflow_instance(async_db_session, sandbox_run_id=run.id,
        workflow_id="integration_failure_recovery", entity_type="integration_operation",
        entity_id="recovery-case", initial_state="dispatch_failed")
    record = {"origin_workflow_instance_id": origin.id, "dispatch_id": "shared-dispatch",
              "frozen_native_decision": {"action": "send_outreach"}}

    async def persist(instance_id, sequence, event_id):
        async_db_session.add(CompanyOSSandboxEvent(
            sandbox_run_id=run.id, workflow_instance_id=instance_id,
            event_id=event_id, sequence=sequence, idempotency_key=f"{event_id}:key",
            primary_classification="acquisition", event_type="failure_detected", result="failed",
            risk_level="GREEN", state_before="scored", state_after="scored", external_side_effect=False,
            payload={"action": "resume_recovery", "metadata": {"gate": "integration",
                     "dispatch_id": "shared-dispatch", "dispatch_attempted": True}},
            native_decision=None, native_failure={"type": "SyntheticCapabilityError"},
        ))
        await async_db_session.flush()

    # One failure on the origin instance, then one on the recovery instance
    # (a different instance id AND a different consumer action) -- both must
    # count toward the same dispatch's failure history.
    await persist(origin.id, 1, "origin-fail-1")
    assert await harness._origin_failure_count(async_db_session, record) == 1
    await persist(recovery.id, 2, "recovery-fail-1")
    assert await harness._origin_failure_count(async_db_session, record) == 2


def test_origin_failure_count_grounds_retry_predicates(async_db_session):
    """P13-REV2-01: "retry policy permits another attempt" / "safe retry
    limit reached or repeated provider errors" / "provider errors continue"
    were all satisfied by a single prepared dispatch record with no actual
    retry happening, because SyntheticWorld's dispatch-ledger status carries
    no retry count. They must now be grounded in origin_failures -- the
    origin dispatch's own genuinely persisted failure_detected count.
    Proves all four tiers: no failure yet (nothing verified), one failure
    (retry-eligible, not exhausted), repeated failures below the limit
    (still eligible), and failures at/above the limit (exhausted, no
    longer eligible).
    """
    rule = harness.REQUIREMENT_RULES
    world = SyntheticWorld("origin-count-test", datetime(2026, 9, 25, tzinfo=UTC))
    facts = {"origin_workflow_id": "prospect_to_meeting", "origin_entity_id": "origin-case",
             "original_action": "send_outreach"}
    # No originating dispatch at all: nothing can ever verify, regardless of count.
    assert rule["retry policy permits another attempt"](facts, world, {}, REGISTRY, 0) is False
    assert rule["safe retry limit reached or repeated provider errors"](facts, world, {}, REGISTRY, 5) is False
    assert rule["provider errors continue"](facts, world, {}, REGISTRY, 5) is False
    world.dispatches["origin-msg"] = {
        "origin_workflow_id": "prospect_to_meeting", "origin_entity_id": "origin-case",
        "frozen_native_decision": {"action": "send_outreach"}, "status": "prepared",
        "adapter_idempotency_key": "origin-key-1",
    }
    for count in range(0, harness.SAFE_RETRY_LIMIT + 1):
        eligible = rule["retry policy permits another attempt"](facts, world, {}, REGISTRY, count)
        exhausted_or_repeated = rule["safe retry limit reached or repeated provider errors"](
            facts, world, {}, REGISTRY, count)
        continuing = rule["provider errors continue"](facts, world, {}, REGISTRY, count)
        if count == 0:
            # Newly prepared dispatch, no real failure yet: nothing verified.
            assert (eligible, exhausted_or_repeated, continuing) == (False, False, False)
        elif count < 2:
            # One real failure: eligible to retry, not yet "repeated"/exhausted.
            assert (eligible, exhausted_or_repeated, continuing) == (True, False, False)
        elif count < harness.SAFE_RETRY_LIMIT:
            # Below the limit but repeated (>=2): still eligible, and errors
            # genuinely "continue".
            assert (eligible, exhausted_or_repeated, continuing) == (True, True, True)
        else:
            # At/above the safe retry limit: exhausted, no longer eligible.
            assert (eligible, exhausted_or_repeated, continuing) == (False, True, True)
    # idempotency is preserved and unsent records remain durable are
    # unaffected by origin_failures -- both stay grounded in the record
    # itself, never the failure count.
    assert rule["idempotency is preserved"](facts, world, {}, REGISTRY, 0) is True
    assert rule["unsent records remain durable"](facts, world, {}, REGISTRY, 0) is True


def test_provider_health_verified_binds_to_specific_originating_provider():
    """P13-REV2-02: "provider health verified" previously accepted ANY
    verified sandbox registry entry, and certified "recovery" from static
    registration alone with no failure having ever happened. It must now
    bind to the *originating* failed dispatch's own specific provider and
    require a successful same-provider attempt after a persisted failure.
    """
    rule = harness.REQUIREMENT_RULES["provider health verified"]
    world = SyntheticWorld("health-test", datetime(2026, 9, 25, tzinfo=UTC))
    facts = {"origin_workflow_id": "prospect_to_meeting", "origin_entity_id": "origin-case",
             "original_action": "send_outreach"}
    world.dispatches["origin-msg"] = {
        "origin_workflow_id": "prospect_to_meeting", "origin_entity_id": "origin-case",
        "frozen_native_decision": {"action": "send_outreach",
                                    "integration": {"name": "sendgrid", "phase": "execute"}},
        "status": "prepared", "adapter_idempotency_key": "origin-key-1",
    }
    # No failure has actually happened yet: static registration alone must
    # never certify recovery.
    assert rule(facts, world, {}, REGISTRY, 0) is False
    # A failure plus static registration still does not establish recovery.
    assert rule(facts, world, {}, REGISTRY, 1) is False
    observed = {"_post_failure_provider_health": True}
    assert rule(facts, world, observed, REGISTRY, 1) is True
    # An unrelated verified entry must never certify a DIFFERENT provider's
    # recovery: sendgrid itself (the one that actually failed) is disabled here.
    sendgrid_disabled = deepcopy(REGISTRY)
    sendgrid_disabled["integrations"]["sendgrid"]["verified"] = False
    assert rule(facts, world, observed, sendgrid_disabled, 1) is False


@pytest.mark.parametrize("status", ["approved", "modified", "rejected", "expired", "cancelled", "needs_more_evidence"])
async def test_complete_approval_lifecycle_via_control_command_queue(async_db_session, status):
    controls = approval_controls()
    corrected = native("finish", "done") if status == "modified" else None
    r = Run(async_db_session, [native("pricing_change", "research", "RED"), native("finish", "done")],
        controls=controls, corrections=[resolution(status, correction=corrected)])
    events = await r.run()
    row = await async_db_session.scalar(select(CompanyOSSandboxApproval))
    assert row.status == status
    assert events[0]["event_type"] == "approval_requested"
    assert events[1]["event_type"] == "approval_resolved"
    assert events[0]["approval"]["decision_id"] == events[1]["approval"]["decision_id"] == "D"
    assert sum(e["event_type"] == "approval_resolved" for e in events) == 1
    if status in {"approved", "modified"}:
        assert events[2]["metadata"]["gate"] == "approved_resume"
        assert row.resume_event_id is not None
    elif status == "needs_more_evidence":
        assert len(events) == 2
        assert r.report.outcomes["one"] == "waiting_on_evidence"
    else:
        assert events[2]["action"] == "finish"
        assert r.report.outcomes["one"] == "terminal"


async def test_complete_retry_lifecycle_via_control_command_queue(async_db_session):
    fixtures = pack()
    fixtures["scripted_failure_types"] = ["ScriptedProviderTimeout"]
    controls = {"one": {"attempts": [{"ordinal": 1, "ref": "timeout", "scripted_failure": "ScriptedProviderTimeout"}],
        "control_commands": [{"id": "retry", "command_type": "retry",
            "precondition": {"type": "failed_attempt_persisted", "attempt_ref": "timeout"}}]}}
    r = Run(async_db_session, [native("finish", "done")], fixtures=fixtures, controls=controls)
    events = await r.run()
    rows = await r.rows()
    assert len(events) == 2
    assert rows[0].native_failure["type"] == "ScriptedProviderTimeout"
    assert rows[1].idempotency_key == rows[0].idempotency_key + ":retry:retry"
    assert not r.report.environment_failure and not r.report.pending


async def test_resume_retry_uses_logical_decision_and_failure_ordinal(async_db_session):
    controls = approval_controls(2)
    controls["one"]["control_commands"].append({"id": "retry", "command_type": "retry",
        "precondition": {"type": "failed_attempt_persisted", "decision_id": "D", "failure_ordinal": 1}})
    fixtures = pack()
    fixtures["cases"][0]["inputs"].append({"delivery_id": "fail-send", "type": "failure_injection", "facts": {
        "workflow_id": "prospect_to_meeting", "entity_id": fixtures["cases"][0]["entity_id"],
        "action": "send_outreach", "after_effect": False}})
    r = Run(async_db_session, [native(), send(limit_name="max_discount_percent", requested_total=20), native("close", "done")],
        fixtures=fixtures, controls=controls, corrections=[resolution()])
    events = await r.run()
    row = await async_db_session.scalar(select(CompanyOSSandboxApproval))
    assert row.failure_attempt_count == 1
    assert row.last_failure_event_id is not None and row.resume_event_id is not None
    assert row.resume_key == f"stage2-approval-resume:{row.id}"
    assert [e["metadata"]["gate"] for e in events] == ["allow", "approval", "founder_resolution", "approval_resume", "approved_resume", "allow"]
    assert len(r.world.outcomes) == 1


@pytest.mark.parametrize("declared", [True, False])
async def test_expected_duplicate_delivery_rejection_recorded_as_evidence(async_db_session, declared):
    controls = {"one": {"attempts": [{"ordinal": 1, "ref": "first"}, {"ordinal": 2, "ref": "duplicate", "replay_of": "first"}]}}
    if declared:
        controls["one"]["expected_service_rejection"] = {"call": "coordinate_sandbox_action",
            "exception_type": "SandboxCoordinatorError", "expects": "duplicate, stale, or terminal workflow delivery",
            "binding": "duplicate"}
    r = Run(async_db_session, [native(), send()], controls=controls)
    if declared:
        await r.run()
        assert r.report.service_rejections[0]["expected"] is True
        assert not r.report.harness_defect
    else:
        with pytest.raises(harness.HarnessDefect, match="duplicate"):
            await r.run()
        assert r.report.harness_defect and not r.report.environment_failure
    assert len(await r.rows()) == len(r.pairs) == 1


async def test_expected_service_rejection_credited_at_most_once(async_db_session):
    """P13B-05: a tag is bound to one specific attempt/decision and credited
    at most once; a further matching rejection is a harness defect, never a
    second silent credit -- otherwise a tag meant for one deliberate
    duplicate-replay test could also credit an unrelated scheduler bug.
    """
    controls = {"one": {"attempts": [{"ordinal": 1, "ref": "first"}, {"ordinal": 2, "ref": "dup", "replay_of": "first"}],
        "expected_service_rejection": {"call": "coordinate_sandbox_action",
            "exception_type": "SandboxCoordinatorError", "expects": "duplicate, stale, or terminal workflow delivery",
            "binding": "dup"}}}
    r, runner, state = await start_internal(async_db_session, controls=controls)
    r.provider.decisions["one"] = [native()]
    await runner.advance(state)
    state.attempt_keys["dup"] = state.attempt_keys["first"]  # the fixture's real, declared duplicate precondition

    async def fake_coordinate(db, **kwargs):
        raise SandboxCoordinatorError("duplicate, stale, or terminal workflow delivery")
    fake_coordinate.__name__ = "coordinate_sandbox_action"

    event = await runner.call(state, fake_coordinate, binding="dup",
        idempotency_key=state.attempt_keys["first"], expected_version=state.instance.version)
    assert event is None and state.rejection_credited is True and state.outcome == "service_rejection"
    assert len(r.report.service_rejections) == 1
    with pytest.raises(harness.HarnessDefect):
        await runner.call(state, fake_coordinate, binding="dup",
            idempotency_key=state.attempt_keys["first"], expected_version=state.instance.version)
    assert len(r.report.service_rejections) == 1


async def test_unconsumed_control_command_is_explicit_runtime_failure(async_db_session):
    r = Run(async_db_session, [native("finish", "done")], controls=approval_controls(), corrections=[resolution()])
    await r.run()
    assert r.report.outcomes["one"] == "unconsumed_control_command"
    assert r.report.failures == [{"case_id": "one", "outcome": "unconsumed_control_command",
                                 "commands": ["resolve"], "dropped_commands": [], "last_outcome": "terminal"}]


async def test_unrelated_signal_after_compliance_block_causes_no_re_attempt(async_db_session):
    """P13B-07: drain() must reset a compliance/policy-blocked case to ready
    only when the arriving signal actually changes compliance/policy-relevant
    evidence, never on any signal -- otherwise, combined with the no-progress
    guard never running on blocked outcomes, an identical compliance-blocked
    send could repeat indefinitely (bounded only by the decision budget).
    """
    r, runner, state = await start_internal(async_db_session)
    r.world.consents[state.instance.entity_id] = {}  # compliance-blocks the upcoming send
    r.provider.decisions["one"] = [native(), send()]
    await runner.advance(state)
    pair = await runner.advance(state)
    assert harness.classify_outcome(pair[0]) == "compliance_blocked"
    assert state.outcome == "compliance_blocked"
    attempts_before = state.attempts
    # An unrelated signal arrives: it touches world state (a failure
    # injection for a different action) but changes nothing evidence_context
    # actually reads for this case's compliance/policy evidence.
    state.source["inputs"].append({"delivery_id": "unrelated", "type": "failure_injection", "facts": {
        "workflow_id": "prospect_to_meeting", "entity_id": state.source["entity_id"],
        "action": "close", "after_effect": False}})
    await runner.drain()
    assert state.outcome == "compliance_blocked"
    assert await runner.advance(state) is None
    assert state.attempts == attempts_before


async def test_alternating_decisions_hit_budget_not_infinite_loop(async_db_session):
    decisions = [native() if i % 2 == 0 else native("revisit", "research") for i in range(harness.MAX_DECISIONS_PER_CASE)]
    r = Run(async_db_session, decisions)
    events = await r.run()
    assert len(events) == harness.MAX_DECISIONS_PER_CASE
    assert r.report.outcomes["one"] == "decision_budget_exhausted"
    assert r.report.failures == [{"case_id": "one", "outcome": "decision_budget_exhausted"}]


async def test_transition_defect_outcome_gets_a_failure_entry(async_db_session):
    """P13B-11: a non-terminal halt that isn't an intentionally-scored
    coverage-bucket endpoint (compliance/policy block, a credited service
    rejection) must surface in the failures report too, not only in
    outcomes -- otherwise a genuine defect (here: an illegal transition) is
    invisible to anything that only reads failures.
    """
    r = Run(async_db_session, [native(state="sent")])  # illegal jump straight from "research"
    await r.run()
    assert r.report.outcomes["one"] == "transition_defect"
    assert r.report.failures == [{"case_id": "one", "outcome": "transition_defect"}]


async def test_driver_orchestration_bug_aborts_before_coordinator_call(async_db_session, monkeypatch):
    class Broken:
        provider = "deterministic_mock"
        def prepare(self, context):
            raise RuntimeError("agent selection broke")
    coordinator = AsyncMock()
    monkeypatch.setattr(harness, "coordinate_sandbox_action", coordinator)
    r = Run(async_db_session, [], provider=Broken())
    with pytest.raises(harness.HarnessDefect, match="agent selection broke"):
        await r.run()
    coordinator.assert_not_called()
    assert await r.rows() == []
    assert r.report.harness_defect


@pytest.mark.parametrize("error,marker", [
    (CompanyOSTransportError("offline"), "environment_failure"),
    (CompanyOSDecisionMethodDefect("bug"), "harness_defect"),
    (CompanyOSMalformedOutputError("bad JSON"), None),
    (RuntimeError("unknown"), "environment_failure"),
])
async def test_provider_failure_provenance(async_db_session, error, marker):
    async def decide(context):
        raise error
    r = Run(async_db_session, [], provider=decide)
    events = await r.run()
    assert len(events) == 1 and events[0]["metadata"]["gate"] == "provider"
    assert r.pairs[0][1]["native_failure"]["type"] == type(error).__name__
    assert bool(r.report.harness_defect) == (marker == "harness_defect")
    assert bool(r.report.environment_failure) == (marker == "environment_failure")


def _bare_dependency(p):
    # A second, "after"-free initial signal keeps the case's own "needs
    # initial signals" precondition satisfied, so the mutated first signal's
    # bare "bare" dependency actually reaches the ambiguous-dependency check
    # instead of failing that earlier, unrelated check first (P13B-08).
    p["cases"][0]["inputs"][0]["after"] = ["bare"]
    p["cases"][0]["inputs"].append({"delivery_id": "second", "type": "reply", "facts": {}})


@pytest.mark.asyncio
async def test_other_case_event_wakes_blocked_case_when_shared_evidence_changes(monkeypatch):
    report = harness.HarnessReport()
    runner = harness._Runner(None, {}, None, None, {}, {"integration_registry": {}}, [], report)
    blocked = harness._Case({"id": "blocked"}, SimpleNamespace())
    blocked.outcome = "waiting_on_evidence"
    blocked.blocked_fingerprint = "before"
    other = harness._Case({"id": "other"}, SimpleNamespace())
    runner.cases = {"blocked": blocked, "other": other}

    async def fingerprint(case, *_):
        return "after" if case["id"] == "blocked" else "unchanged"

    monkeypatch.setattr(harness, "decision_fingerprint", fingerprint)
    monkeypatch.setattr(harness, "export_pair", lambda _: ({"action": "other_action"}, {}))
    monkeypatch.setattr(harness, "classify_outcome", lambda _: "terminal")
    await runner.record(other, SimpleNamespace(event_id="other-event", native_failure=None))
    assert blocked.outcome == "ready"
    assert report.outcomes["blocked"] == "ready"


@pytest.mark.parametrize("mutate,match", [
    (_bare_dependency, "ambiguous dependency: expected signal: or milestone:"),
    (lambda p: p["cases"][0]["inputs"].append({"delivery_id": "next", "type": "reply", "facts": {},
        "after": ["milestone:one:score_against_icp"]}), "not a governed email"),
    (lambda p: p["cases"][0]["inputs"].extend([
        {"delivery_id": "a", "type": "reply", "facts": {}, "after": ["signal:b"]},
        {"delivery_id": "b", "type": "reply", "facts": {}, "after": ["signal:a"]}]), "cyclic"),
])
def test_ambiguous_dependency_rejected_at_pack_build(mutate, match):
    fixtures = pack()
    mutate(fixtures)
    with pytest.raises(harness.FixtureAuthoringError, match=match):
        harness.validate_pack(fixtures, {"prospect_to_meeting": WORKFLOW}, [], REGISTRY)


def test_control_command_precondition_never_satisfied_rejected_at_pack_build():
    fixtures = pack()
    fixtures["cases"][0].update(approval_controls()["one"])
    fixtures["cases"][0]["attempts"] = []
    with pytest.raises(harness.FixtureAuthoringError, match="undeclared decision"):
        harness.validate_pack(fixtures, {"prospect_to_meeting": WORKFLOW}, [resolution()], REGISTRY)


async def test_controls_never_enter_provider_context_or_apply_signal(async_db_session, monkeypatch):
    seen = []
    original = harness.apply_stage2_signal
    def apply(world, case, signal, now):
        seen.append(signal["type"])
        return original(world, case, signal, now)
    monkeypatch.setattr(harness, "apply_stage2_signal", apply)
    r = Run(async_db_session, [native("pricing_change", "research", "RED"), native("finish", "done")],
        controls=approval_controls(), corrections=[resolution()])
    prepare = r.provider.prepare
    def capture(context):
        encoded = str(context)
        assert "control_commands" not in encoded and "corrected_decision" not in encoded and "founder_minutes" not in encoded
        return prepare(context)
    r.provider.prepare = capture
    await r.run()
    assert seen == ["prospect"]


async def test_idempotent_replay_without_pending_precondition(async_db_session):
    r = Run(async_db_session, [native("pricing_change", "research", "RED"), native("finish", "done")],
        controls=approval_controls(), corrections=[resolution()])
    await r.run()
    approval = await async_db_session.scalar(select(CompanyOSSandboxApproval))
    row = await resolve_sandbox_approval(async_db_session, approval_id=approval.id, status="approved",
        founder_id="synthetic-founder", founder_minutes=2, resolution_key="one:D:approved", workflow=WORKFLOW, event_schema=SCHEMA)
    resume = await resume_sandbox_approval(async_db_session, approval_id=approval.id,
        **{k: v for k, v in r.options.items() if k != "workflows"}, workflow=WORKFLOW)
    assert row.id == approval.resolution_event_id and resume.id == approval.resume_event_id
    runner = harness._Runner(async_db_session, r.pack, r.world, r.provider, r.workflows, {}, [], r.report)
    runner.exported = {p[0]["event_id"] for p in r.pairs}
    assert await runner.record(None, row) is None and await runner.record(None, resume) is None
    assert len(await r.rows()) == len(r.pairs)


async def test_schema_declares_policy_intent_fields_email_execute_reaches_allow_or_approval(async_db_session):
    """P13B-03: the decision-provider schema must declare integration_mode/
    requires_consent/requires_authority/limit_name/requested_total/
    policy_flags -- evaluate_action_policy blocks any external_write intent
    whose integration_mode isn't declared, so without these fields a schema-
    conformant email-execute decision was systematically policy-blocked.
    Validates a schema-shaped decision against the actual schema, then drives
    it through coordinate_sandbox_action to a real allow/approval outcome,
    never blocked for a schema reason.
    """
    from jsonschema import Draft202012Validator

    from app.agents.company_os_stage2_provider import stage2_json_schema

    context = {"workflow": WORKFLOW, "canonical_actions": ACTIONS, "canonical_agents": ["email_outreach"],
               "canonical_handoffs": ["orchestrator"], "integration_registry": REGISTRY}
    schema = stage2_json_schema(context)
    assert schema["properties"]["policy_intent"]["properties"]["integration_mode"]["enum"] == sorted(REGISTRY["modes"])
    decision = send(requires_consent=True, requires_authority=False)
    Draft202012Validator(schema).validate(decision)
    r, runner, state = await start_internal(async_db_session)
    r.provider.decisions["one"] = [native(), decision]
    await runner.advance(state)
    pair = await runner.advance(state)
    assert pair[0]["metadata"]["gate"] in {"allow", "approval"}
    assert r.report.outcomes["one"] in {"ready", "terminal", "waiting_on_approval"}


async def test_revenue_ops_owner_route_uses_inherited_stage2_method():
    from app.agents.revenue_ops.agent import RevenueOperationsAgent
    agent = RevenueOperationsAgent()
    workflow = {**WORKFLOW, "owner_agent": "revenue_ops"}
    context = {"case_id": "one", "current_state": "research", "workflow": workflow,
               "canonical_actions": ACTIONS, "canonical_agents": ["revenue_ops"], "canonical_handoffs": ["orchestrator"]}
    output = {**native(), "agent_type": "revenue_ops"}
    provider = OwnerRoutedProvider({"workflows": {workflow["id"]: {"owner_agent": "revenue_ops"}}},
        {"revenue_ops": agent}, structured_transport=lambda prompt, schema: (output, "mock envelope"))
    assert await provider.prepare(context)({}) == output


@pytest.mark.parametrize("mode,error", [("missing", CompanyOSTransportError), ("malformed", CompanyOSMalformedOutputError),
                                        ("bug", CompanyOSDecisionMethodDefect)])
async def test_stage2_method_types_errors(mode, error, monkeypatch):
    monkeypatch.setattr("app.agents.base_agent.claude_structured_output_available", lambda: False)
    context = {"workflow": WORKFLOW, "canonical_actions": ACTIONS,
               "canonical_agents": ["email_outreach"], "canonical_handoffs": ["orchestrator"]}
    def transport(prompt, schema):
        if mode == "bug":
            raise RuntimeError("transport implementation bug")
        return {}, "invalid native output"
    with pytest.raises(error):
        await BasePolsiaAgent().run_company_os_stage2_decision({}, context,
            structured_transport=None if mode == "missing" else transport)


@pytest.mark.parametrize("dispatch", ["executed", "prepared", "blocked"])
async def test_cross_case_queue_ordering(async_db_session, dispatch):
    fixtures = pack()
    origin = fixtures["cases"][0]
    recovery = deepcopy(origin)
    recovery.update(id="two", workflow_id="integration_failure_recovery", entity_type="integration_operation", entity_id="queue-one")
    recovery["contact_record"].update(recipient_id="other", address="other@example.invalid", contact_email="other@example.invalid")
    recovery["consent_record"].update(recipient_id="other", contact_email="other@example.invalid")
    recovery["inputs"] = [
        {"delivery_id": "initial-two", "type": "estimate", "facts": {}},
        {"delivery_id": "queue", "type": "queued_message", "after": ["milestone:one:send_outreach"], "facts": {
            "entity_id": "queue-one", "workflow_id": "integration_failure_recovery", "item_id": "queue-one",
            "recipient_id": origin["entity_id"], "origin_workflow_id": "prospect_to_meeting",
            "origin_entity_id": origin["entity_id"], "original_action": "send_outreach"}},
    ]
    fixtures["cases"].append(recovery)
    if dispatch == "blocked":
        origin["consent_record"] = None
    seen, item_ids = [], []
    provider = DeterministicMockProvider({"one": [native(), send(), native("close", "done")],
                                         "two": [native("finish", "done")]})
    prepare = provider.prepare
    def capture(context):
        seen.append((context["case_id"], [s["delivery_id"] for s in context["signals"]]))
        if context["case_id"] == "two":
            item_ids.append(context.get("item_id"))
        return prepare(context)
    provider.prepare = capture
    r = Run(async_db_session, [], fixtures=fixtures, provider=provider)
    # Second case waits for a queue fact, then completes without a second send.
    r.workflows["integration_failure_recovery"] = {
        **deepcopy(WORKFLOW), "id": "integration_failure_recovery", "entity_type": "integration_operation"}
    # "queue integrity verified" is a real, non-self-fulfilling predicate
    # (REQUIREMENT_RULES): it checks world.queue for this case's own entity_id,
    # never a same-named fixture fact (P13B-02's ban on that pattern).
    r.workflows["integration_failure_recovery"]["transitions"][-1]["requirements"] = ["queue integrity verified"]
    # Run stores the supplied pack by reference; world has no ingested signals yet.
    provider.decisions["two"] = [native("finish", "done"), native("finish", "done")]
    if dispatch == "prepared":
        r.adapter.execute = AsyncMock(return_value="invalid receipt")
    events = await r.run()
    record = next(iter(r.world.dispatches.values()))
    assert record["status"] == dispatch
    if dispatch == "blocked":
        assert "queue-one" not in r.world.queue
        assert "queue" in r.report.pending["two"]["signals"]
    else:
        assert r.world.queue["queue-one"]["dispatch_id"] == record["dispatch_id"]
        two_contexts = [signals for case, signals in seen if case == "two"]
        assert "queue" not in two_contexts[0] and "queue" in two_contexts[1]
        assert r.report.outcomes["two"] == "terminal"
        # P13B-10: the snapshot only carries item_id once the queue item is
        # actually bound -- before that, find_dispatch/resolve_recipient's
        # queue-scope cross-check has nothing to check against.
        assert item_ids == [None, "queue-one"]
    assert events[0]["entity_id"] == origin["entity_id"]
    assert events[1]["entity_id"] == "queue-one"  # round robin, not case exhaustion


def test_snapshot_item_id_makes_queue_scope_mismatch_detectable():
    """P13B-10: resolve_recipient's queue-scope cross-check (company_os_
    sandbox_dispatch.py) only runs when the snapshot actually carries
    item_id; without it, a real queue-scope mismatch for an aggregate-
    workflow case would pass unnoticed for any harness run.
    """
    from app.services.company_os_sandbox_dispatch import resolve_recipient
    from app.services.company_os_synthetic_adapters import SyntheticComplianceBlock

    world = SyntheticWorld("item-id-test", datetime(2026, 9, 25, tzinfo=UTC))
    world.set_contact("r-1", {"recipient_id": "r-1", "address": "r-1@example.test", "consent_verified": True})
    world.queue["queue-one"] = {"item_id": "queue-one", "recipient_id": "r-1",
        "workflow_id": "integration_failure_recovery", "entity_id": "queue-one",
        "origin_workflow_id": "prospect_to_meeting", "origin_entity_id": "case-a", "original_action": "send_outreach"}
    instance = SimpleNamespace(workflow_id="integration_failure_recovery", entity_type="integration_operation",
                               entity_id="queue-one")
    action = "resume_preserved_queue_within_original_approval"
    # Without item_id in the snapshot (the pre-fix state), the cross-check
    # never runs at all -- a wrong item_id would pass unnoticed.
    resolve_recipient(world, instance, {}, action)
    # With the real item_id carried at the snapshot's top level, a genuine
    # mismatch is now caught.
    with pytest.raises(SyntheticComplianceBlock):
        resolve_recipient(world, instance, {"item_id": "wrong-item"}, action)
    # And the correct item_id still resolves cleanly.
    assert resolve_recipient(world, instance, {"item_id": "queue-one"}, action)[0] == "r-1"


def test_chained_recovery_milestone_matches_receive_not_consumer_registration():
    """P13B-04: dependency_ready must evaluate the identical predicate
    SyntheticWorld.receive uses to match a queued_message against a dispatch
    record -- the record's own origin_workflow_id/origin_entity_id and its
    frozen action -- never "is this case's instance a registered consumer of
    some record". Those diverge for a chained recovery: case B becomes a
    consumer of case A's original record (by reusing A's queued dispatch)
    without ever originating a record of its own for B's own action, so a
    consumer-based check would falsely report a milestone on B's own action
    as satisfied when receive() could never bind a queued_message against it.
    """
    world = SyntheticWorld("chain-test", datetime(2026, 9, 25, tzinfo=UTC))
    record = world.prepare_dispatch(
        message_key="origin-msg", recipient_id="r-1", adapter_kind="mail",
        adapter_idempotency_key="k-1", origin_workflow_instance_id=1,
        origin_state_before="ready_to_send", origin_workflow_version=0,
        frozen_native_decision={"action": "send_outreach"}, rendered_payload={},
        consumers={}, origin_workflow_id="prospect_to_meeting", origin_entity_id="case-a",
    )
    # Case B (a downstream recovery case) becomes a *consumer* of case A's
    # record with its own, different action -- it never originates a record.
    record["consumers"][99] = {"expected_state_before": "x", "expected_version": 0,
        "consumer_decision": {"action": "retry_send"}, "approval_id": None}
    runner = harness._Runner(None, {}, world, None, {}, {}, [], harness.HarnessReport())
    runner.cases["a"] = harness._Case(
        {"id": "a", "workflow_id": "prospect_to_meeting", "entity_id": "case-a"}, SimpleNamespace(id=1))
    runner.cases["b"] = harness._Case(
        {"id": "b", "workflow_id": "integration_failure_recovery", "entity_id": "queue-b"}, SimpleNamespace(id=99))
    assert runner.dependency_ready("milestone:b:retry_send") is False
    # The record's real origin -- case A's own send_outreach -- is exactly
    # what receive() matches, and is correctly satisfied.
    assert runner.dependency_ready("milestone:a:send_outreach") is True


def test_queued_message_milestone_mismatch_rejected_at_pack_build():
    """P13B-04: a queued_message's own origin facts must agree with its
    declared milestone dependency; a mismatch would let the scheduler drain a
    signal receive() can never bind to that dispatch record, producing a
    silent unmatched queue entry instead of a loud pack-build failure.
    """
    fixtures = pack()
    origin = fixtures["cases"][0]
    recovery = deepcopy(origin)
    recovery.update(id="two", workflow_id="integration_failure_recovery", entity_type="integration_operation",
                    entity_id="queue-one")
    recovery["inputs"] = [
        {"delivery_id": "initial-two", "type": "estimate", "facts": {}},
        {"delivery_id": "queue", "type": "queued_message", "after": ["milestone:one:send_outreach"], "facts": {
            "entity_id": "queue-one", "workflow_id": "integration_failure_recovery", "item_id": "queue-one",
            "recipient_id": origin["entity_id"], "origin_workflow_id": "prospect_to_meeting",
            "origin_entity_id": origin["entity_id"], "original_action": "record_opt_out"}},  # wrong action
    ]
    fixtures["cases"].append(recovery)
    with pytest.raises(harness.FixtureAuthoringError, match="queued message origin facts"):
        harness.validate_pack(fixtures, {"prospect_to_meeting": WORKFLOW,
            "integration_failure_recovery": {**WORKFLOW, "id": "integration_failure_recovery",
                                             "entity_type": "integration_operation"}}, [], REGISTRY)


def test_queued_message_recipient_must_match_person_origin_at_pack_build():
    fixtures = pack()
    origin = fixtures["cases"][0]
    recovery = deepcopy(origin)
    recovery.update(id="two", workflow_id="integration_failure_recovery", entity_type="integration_operation",
                    entity_id="queue-one")
    recovery["inputs"] = [
        {"delivery_id": "initial-two", "type": "estimate", "facts": {}},
        {"delivery_id": "queue", "type": "queued_message", "after": ["milestone:one:send_outreach"], "facts": {
            "entity_id": "queue-one", "workflow_id": "integration_failure_recovery", "item_id": "queue-one",
            "recipient_id": "different-recipient", "origin_workflow_id": "prospect_to_meeting",
            "origin_entity_id": origin["entity_id"], "original_action": "send_outreach"}},
    ]
    fixtures["cases"].append(recovery)
    with pytest.raises(harness.FixtureAuthoringError, match="queued message recipient differs"):
        harness.validate_pack(fixtures, {"prospect_to_meeting": WORKFLOW,
            "integration_failure_recovery": {**WORKFLOW, "id": "integration_failure_recovery",
                                             "entity_type": "integration_operation"}}, [], REGISTRY)


async def test_failure_injection_ingested_before_dispatch_attempt(async_db_session, monkeypatch):
    fixtures = pack()
    identity = ("prospect_to_meeting", fixtures["cases"][0]["entity_id"], "send_outreach")
    fixtures["cases"][0]["inputs"].append({"delivery_id": "fail", "type": "failure_injection",
        "after": ["signal:" + fixtures["cases"][0]["inputs"][0]["delivery_id"]],
        "facts": {"workflow_id": identity[0], "entity_id": identity[1], "action": identity[2], "after_effect": True}})
    controls = {"one": {"attempts": [{"ordinal": 2, "ref": "send"}], "control_commands": [
        {"id": "retry", "command_type": "retry", "precondition": {"type": "failed_attempt_persisted", "attempt_ref": "send"}}]}}
    r = Run(async_db_session, [native(), send(), native("close", "done")], fixtures=fixtures, controls=controls)
    original = harness.coordinate_sandbox_action
    async def coordinate(*a, **k):
        if k["idempotency_key"] == "one:attempt:2":
            assert r.world.action_failures[identity] is True
        return await original(*a, **k)
    monkeypatch.setattr(harness, "coordinate_sandbox_action", coordinate)
    # Preparing the callback does not invoke it during receipt reconciliation.
    # The mock needs a placeholder for that uncalled decision turn.
    r.provider.decisions["one"].insert(2, native("close", "done"))
    events = await r.run()
    assert [e["metadata"]["gate"] for e in events] == ["allow", "integration", "reconciliation", "allow"]
    assert len(r.world.outcomes) == 1


async def start_internal(db, *, controls=None, corrections=None):
    """Single-step driver for deliberate service collisions/replays outside normal scheduling."""
    r = Run(db, [], controls=controls, corrections=corrections)
    run = await create_sandbox_run(db, run_id=r.world.run_id, company_slug="acqivo", protocol_version="0.1.0", input_manifest={"provider": "deterministic_mock"})
    instance = await create_workflow_instance(db, sandbox_run_id=run.id, workflow_id=WORKFLOW["id"],
        entity_type="prospect", entity_id=r.pack["cases"][0]["entity_id"], initial_state="research")
    options = {k: v for k, v in r.options.items() if k != "workflows"}
    runner = harness._Runner(db, r.pack, r.world, r.provider, r.workflows, options, r.corrections, r.report)
    case = deepcopy(r.pack["cases"][0])
    case.update((controls or {}).get("one", {}))
    state = harness._Case(case, instance)
    runner.cases["one"] = state
    return r, runner, state


@pytest.mark.parametrize("scenario,outcome", [
    ("allow", "ready"), ("observed", "ready"), ("terminal", "terminal"),
    ("contract", "native_decision_defect"), ("policy", "policy_blocked"),
    ("transition", "transition_defect"), ("missing_evidence", "waiting_on_evidence"),
    ("integration", "integration_failure"), ("provider", "provider_failure"),
    ("missing_decision", "approval_collision"), ("pending_collision", "approval_collision"),
    ("unresumed_collision", "approval_collision"), ("approval", "waiting_on_approval"),
    ("resume_success", "ready"), ("resume_defect", "resume_defect"),
    ("unsupported_execute", "waiting_on_reconciliation"), ("compliance", "compliance_blocked"),
    ("dispatch_review", "waiting_on_reconciliation"),
])
async def test_decision_loop_covers_every_real_service_signature(async_db_session, scenario, outcome):
    controls = approval_controls()
    r, runner, state = await start_internal(async_db_session, controls=controls, corrections=[resolution()])
    decision = native()
    if scenario == "observed":
        decision = native(state="research")
    elif scenario == "terminal":
        decision = native("finish", "done")
    elif scenario == "contract":
        decision = {"unexpected": "output"}
    elif scenario == "policy":
        decision = native("bypass_opt_out", "research", "RED")
    elif scenario == "transition":
        decision = native(state="sent")
    elif scenario == "missing_evidence":
        r.workflows[WORKFLOW["id"]]["transitions"][0]["requirements"] = ["absent fact"]
    elif scenario == "integration":
        decision = native(state="research", integration={"name": "missing", "phase": "execute"})
    elif scenario == "provider":
        async def failure(_):
            raise CompanyOSMalformedOutputError("synthetic malformed output")
        runner.decide = failure
    elif scenario in {"missing_decision", "pending_collision", "unresumed_collision", "approval", "resume_success", "resume_defect"}:
        decision = native("pricing_change", "research", "RED")
        if scenario == "missing_decision":
            state.source["attempts"] = []
    elif scenario == "unsupported_execute":
        registry = deepcopy(REGISTRY)
        registry["integrations"]["sendgrid"]["desired_use"] = ["crm_update"]
        runner.options["integration_registry"] = registry
        decision = native(state="research", integration={"name": "sendgrid", "phase": "execute", "use": "crm_update"})
    elif scenario in {"compliance", "dispatch_review"}:
        r.provider.decisions["one"] = [native()]
        await runner.advance(state)
        decision = send()
        if scenario == "compliance":
            r.world.consents[state.instance.entity_id] = {}
        else:
            r.world.review_blocked_recipients.add(state.instance.entity_id)
    if scenario != "provider":
        r.provider.decisions["one"] = [decision]
        r.provider.positions.clear()
    pair = await runner.advance(state)
    if scenario in {"pending_collision", "unresumed_collision", "resume_success", "resume_defect"}:
        assert pair[0]["event_type"] == "approval_requested"
        approval = await runner.approval(state, "D")
        if scenario != "pending_collision":
            await runner.call(state, resolve_sandbox_approval, approval_id=approval.id, status="approved",
                founder_id="synthetic-founder", founder_minutes=0, resolution_key="resolve",
                event_schema=SCHEMA, workflow=r.workflows[WORKFLOW["id"]])
        if scenario in {"resume_success", "resume_defect"}:
            if scenario == "resume_defect":
                runner.options["policy"] = {**POLICY, "hard_denies": ["pricing_change"]}
            event = await runner.call(state, resume_sandbox_approval, approval_id=approval.id, **runner.options_for(state))
            pair = await runner.record(state, event)
        else:
            state.outcome = "ready"  # deliberate collision, never normal scheduler behavior
            state.source["attempts"].append({"ordinal": 2, "ref": "collision", "decision_id": "D2"})
            r.provider.decisions["one"].append(decision)
            pair = await runner.advance(state)
    assert harness.classify_outcome(pair[0]) == outcome
    assert state.outcome == outcome


async def test_expected_stale_workflow_state_rejection_recorded_as_evidence(async_db_session):
    controls = approval_controls()
    controls["one"]["expected_service_rejection"] = {"call": "resume_sandbox_approval",
        "exception_type": "SandboxApprovalError", "expects": "workflow changed since approval request",
        "binding": "D"}
    r, runner, state = await start_internal(async_db_session, controls=controls)
    r.provider.decisions["one"] = [native("pricing_change", "research", "RED")]
    await runner.advance(state)
    approval = await runner.approval(state, "D")
    await runner.call(state, resolve_sandbox_approval, approval_id=approval.id, status="approved",
        founder_id="founder", founder_minutes=1, resolution_key="resolve", event_schema=SCHEMA, workflow=WORKFLOW)
    state.instance.version += 1  # deliberately stale DB delivery, not a fabricated event
    await async_db_session.flush()
    event = await runner.call(state, resume_sandbox_approval, binding="D", approval_id=approval.id,
        **runner.options_for(state))
    assert event is None
    assert len(await r.rows()) == 2
    assert r.report.service_rejections[0]["exception_type"] == "SandboxApprovalError"


async def test_expected_resolution_conflict_rejection_recorded_as_evidence(async_db_session):
    """P13-REV-04: _Runner.resolve did not pass binding=decision_id to
    self.call (unlike resume, which does), so a fixture declaring an
    expected_service_rejection for a resolve_sandbox_approval call compared
    the required non-empty binding against None and always aborted as an
    unexpected harness defect instead of crediting the deliberate rejection.
    Drives resolve_sandbox_approval (via _Runner.resolve, not a raw
    runner.call) into its own real "only pending approval may be resolved"
    rejection -- a second, conflicting resolution attempt on an
    already-resolved approval -- and confirms it is credited as evidence.
    """
    controls = approval_controls()
    controls["one"]["expected_service_rejection"] = {"call": "resolve_sandbox_approval",
        "exception_type": "SandboxApprovalError", "expects": "only pending approval may be resolved",
        "binding": "D"}
    r, runner, state = await start_internal(async_db_session, controls=controls, corrections=[resolution()])
    r.provider.decisions["one"] = [native("pricing_change", "research", "RED")]
    await runner.advance(state)
    first = await runner.resolve(state, "D")
    assert await runner.record(state, first) is not None
    # A second, conflicting resolution of the same already-resolved approval
    # -- deliberately different founder_minutes, so it can never take the
    # service's own identical-replay branch -- reproduces the service's real
    # "only pending approval may be resolved" raise.
    runner.corrections[0]["founder_minutes"] = 99
    event = await runner.resolve(state, "D")
    assert event is None
    assert state.rejection_credited is True and state.outcome == "service_rejection"
    assert r.report.service_rejections == [{"case_id": "one", "call": "resolve_sandbox_approval",
        "exception_type": "SandboxApprovalError", "message": "only pending approval may be resolved",
        "binding": "D", "expected": True}]
    assert len(await r.rows()) == 2  # approval_requested + the one successful approval_resolved


async def test_needs_more_evidence_unblocks_via_new_decision_identity(async_db_session):
    controls = approval_controls()
    controls["one"]["attempts"].append({"ordinal": 2, "ref": "new", "decision_id": "D2"})
    controls["one"]["control_commands"].append({"id": "resolve2", "command_type": "founder_resolution", "decision_id": "D2",
        "precondition": {"type": "approval_pending", "decision_id": "D2"}})
    r, runner, state = await start_internal(async_db_session, controls=controls,
        corrections=[resolution("needs_more_evidence"), resolution("approved", "D2")])
    r.provider.decisions["one"] = [native("pricing_change", "research", "RED"), native("pricing_change", "research", "RED")]
    await runner.advance(state)
    first = [p async for p in runner.controls()]
    assert len(first) == 1 and state.outcome == "waiting_on_evidence"
    assert await runner.advance(state) is None
    state.source["inputs"].append({"type": "prospect", "delivery_id": "fresh", "facts": {"public_source": "new-evidence"}})
    request = await runner.advance(state)
    assert request[0]["approval"]["decision_id"] == "D2"
    second = [p async for p in runner.controls()]
    assert [p[0]["metadata"]["gate"] for p in second] == ["founder_resolution", "approved_resume"]
    approvals = list(await async_db_session.scalars(select(CompanyOSSandboxApproval).order_by(CompanyOSSandboxApproval.id)))
    assert [a.status for a in approvals] == ["needs_more_evidence", "approved"]
    assert approvals[0].id != approvals[1].id


async def test_resolution_replay_through_driver_exports_once(async_db_session):
    r, runner, state = await start_internal(async_db_session, controls=approval_controls(), corrections=[resolution()])
    r.provider.decisions["one"] = [native("pricing_change", "research", "RED")]
    await runner.advance(state)
    first = await runner.resolve(state, "D")
    assert await runner.record(state, first) is not None
    second = await runner.resolve(state, "D")
    assert second.event_id == first.event_id
    assert await runner.record(state, second) is None
    assert len(await r.rows()) == 2


async def test_loop_stops_at_max_attempts_on_misconfigured_retry_fixture(async_db_session):
    fixtures = pack()
    fixtures["scripted_failure_types"] = ["ScriptedProviderTimeout"]
    controls = {"one": {"attempts": [
        {"ordinal": n, "ref": f"fail{n}", "scripted_failure": "ScriptedProviderTimeout"}
        for n in range(1, 5)], "control_commands": [
        {"id": f"retry{n}", "command_type": "retry", "precondition": {"type": "failed_attempt_persisted", "attempt_ref": f"fail{n}"}}
        for n in range(1, 5)]}}
    r = Run(async_db_session, [], fixtures=fixtures, controls=controls)
    assert len(await r.run()) == 4
    assert r.report.outcomes["one"] == "retry_budget_exhausted"
    assert not r.report.environment_failure


@pytest.mark.parametrize("structured", [False, True])
async def test_cli_exports_commit_order_hashes_and_refuses_occupied_paths(async_db_session, tmp_path, monkeypatch, structured):
    import hashlib
    import json

    from scripts.stage2_harness_export import export
    r = Run(async_db_session, [])
    runtime = {k: v for k, v in r.options.items() if k != "synthetic_adapters"}
    runtime.update(compliance_policy=r.world.compliance_policy, synthetic_adapter_kinds={"sendgrid": "mail"})
    if structured:
        r.pack["infrastructure_only"] = False
        runtime["workflow_registry"] = {"workflows": {WORKFLOW["id"]: {"owner_agent": "email_outreach"}}}
        # Exercise CLI owner selection and the actual Stage 2 method, never a process.
        monkeypatch.setattr(BasePolsiaAgent, "_run_claude_structured",
                            lambda self, prompt, schema, **kw: (native("finish", "done"), "mock envelope"))
    for name, value in (("pack.json", r.pack), ("runtime.json", runtime), ("decisions.json", {"one": [native("finish", "done")]})):
        (tmp_path / name).write_text(json.dumps(value))
    args = SimpleNamespace(provider="deterministic_mock", infrastructure_only=True, run_id="cli-test",
        fixture_pack=tmp_path / "pack.json", runtime_inputs=tmp_path / "runtime.json", mock_decisions=tmp_path / "decisions.json",
        founder_resolutions=None, driver_controls=None, events=tmp_path / "events.ndjson", native_evidence=tmp_path / "native.ndjson",
        service_rejections=tmp_path / "rejections.ndjson", database=tmp_path / "test.db")
    if structured:
        args.provider, args.infrastructure_only, args.mock_decisions = "stage2_structured", False, None
    assert await export(args) == 0
    raw = args.events.read_bytes()
    events = [json.loads(line) for line in raw.splitlines()]
    evidence = [json.loads(line) for line in args.native_evidence.read_bytes().splitlines()]
    assert len(events) == len(evidence) == 1
    assert events[0]["event_id"] == evidence[0]["event_id"]
    assert args.service_rejections.read_bytes() == b""
    report = json.loads((tmp_path / "events.ndjson_driver_report.json").read_text())
    assert report["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["provider"] == ("stage2_structured" if structured else "deterministic_mock")
    assert report["infrastructure_only"] is (not structured)
    with pytest.raises(ValueError, match="unoccupied"):
        await export(args)
    assert args.events.read_bytes() == raw


@pytest.mark.parametrize("kind", ["preparation", "method", "transport", "malformed"])
async def test_cli_failure_markers_preserve_partial_evidence(async_db_session, tmp_path, monkeypatch, kind):
    import json

    import scripts.stage2_harness_export as cli
    r = Run(async_db_session, [])
    runtime = {k: v for k, v in r.options.items() if k != "synthetic_adapters"}
    runtime.update(compliance_policy=r.world.compliance_policy, synthetic_adapter_kinds={"sendgrid": "mail"})
    for name, value in (("pack.json", r.pack), ("runtime.json", runtime), ("decisions.json", {})):
        (tmp_path / name).write_text(json.dumps(value))
    args = SimpleNamespace(provider="deterministic_mock", infrastructure_only=True, run_id="cli-test",
        fixture_pack=tmp_path / "pack.json", runtime_inputs=tmp_path / "runtime.json", mock_decisions=tmp_path / "decisions.json",
        founder_resolutions=None, driver_controls=None, events=tmp_path / "events.ndjson", native_evidence=tmp_path / "native.ndjson",
        service_rejections=tmp_path / "rejections.ndjson", database=tmp_path / "test.db")
    class Provider:
        provider = "deterministic_mock"
        count = 0
        def __init__(self, _):
            pass
        def prepare(self, context):
            self.count += 1
            if self.count == 1:
                async def first(_):
                    return native()
                return first
            if kind == "preparation":
                raise RuntimeError("broken preparation")
            error = {"method": CompanyOSDecisionMethodDefect, "transport": CompanyOSTransportError,
                     "malformed": CompanyOSMalformedOutputError}[kind]("synthetic")
            async def fail(_):
                raise error
            return fail
    monkeypatch.setattr(cli, "DeterministicMockProvider", Provider)
    assert await cli.export(args) == (0 if kind == "malformed" else 1)
    assert len(args.events.read_text().splitlines()) == (1 if kind == "preparation" else 2)
    assert len(args.native_evidence.read_text().splitlines()) == len(args.events.read_text().splitlines())
    assert (tmp_path / "events.ndjson_harness_defect.json").exists() == (kind in {"preparation", "method"})
    assert (tmp_path / "events.ndjson_environment_failure.json").exists() == (kind == "transport")


@pytest.mark.parametrize("malformed", ["pack", "controls"])
async def test_cli_malformed_pack_or_controls_is_harness_defect_not_environment_failure(
    async_db_session, tmp_path, malformed,
):
    """P13B-06: a malformed pack/controls file is a code/fixture defect and
    must never be misfiled as infrastructure flakiness -- widened from a
    fixed (KeyError, TypeError, ValueError) tuple that let most other
    exceptions (e.g. AttributeError from a case that isn't even an object)
    fall through to the CLI's own broad except Exception.
    """
    import json

    from scripts.stage2_harness_export import export
    r = Run(async_db_session, [])
    runtime = {k: v for k, v in r.options.items() if k != "synthetic_adapters"}
    runtime.update(compliance_policy=r.world.compliance_policy, synthetic_adapter_kinds={"sendgrid": "mail"})
    pack_payload = r.pack
    driver_controls = None
    if malformed == "pack":
        pack_payload = deepcopy(r.pack)
        pack_payload["cases"] = ["not-a-case-object"]
    else:
        driver_controls = ["not-an-object"]
    for name, value in (("pack.json", pack_payload), ("runtime.json", runtime),
                        ("decisions.json", {"one": [native("finish", "done")]})):
        (tmp_path / name).write_text(json.dumps(value))
    if driver_controls is not None:
        (tmp_path / "controls.json").write_text(json.dumps(driver_controls))
    args = SimpleNamespace(provider="deterministic_mock", infrastructure_only=True, run_id="cli-test",
        fixture_pack=tmp_path / "pack.json", runtime_inputs=tmp_path / "runtime.json", mock_decisions=tmp_path / "decisions.json",
        founder_resolutions=None, driver_controls=(tmp_path / "controls.json") if driver_controls is not None else None,
        events=tmp_path / "events.ndjson", native_evidence=tmp_path / "native.ndjson",
        service_rejections=tmp_path / "rejections.ndjson", database=tmp_path / "test.db")
    assert await export(args) == 1
    assert (tmp_path / "events.ndjson_harness_defect.json").exists()
    assert not (tmp_path / "events.ndjson_environment_failure.json").exists()


async def test_cli_case_missing_contact_record_is_harness_defect_not_environment_failure(async_db_session, tmp_path):
    """P13-REV-03: validate_artifact_shapes only checks outer containers (the
    pack is an object with a list of case objects) -- a case missing
    contact_record entirely slips past it, then build_stage2_snapshot (the
    CLI's own direct pre-build call, outside run_stage2_fixture_pack) raises
    Stage2InputError, a plain ValueError that isn't a HarnessDefect. It must
    be classified as a harness defect (a malformed fixture), never an
    environment failure.
    """
    import json

    from scripts.stage2_harness_export import export
    r = Run(async_db_session, [])
    runtime = {k: v for k, v in r.options.items() if k != "synthetic_adapters"}
    runtime.update(compliance_policy=r.world.compliance_policy, synthetic_adapter_kinds={"sendgrid": "mail"})
    pack_payload = deepcopy(r.pack)
    del pack_payload["cases"][0]["contact_record"]
    for name, value in (("pack.json", pack_payload), ("runtime.json", runtime),
                        ("decisions.json", {"one": [native("finish", "done")]})):
        (tmp_path / name).write_text(json.dumps(value))
    args = SimpleNamespace(provider="deterministic_mock", infrastructure_only=True, run_id="cli-test",
        fixture_pack=tmp_path / "pack.json", runtime_inputs=tmp_path / "runtime.json", mock_decisions=tmp_path / "decisions.json",
        founder_resolutions=None, driver_controls=None,
        events=tmp_path / "events.ndjson", native_evidence=tmp_path / "native.ndjson",
        service_rejections=tmp_path / "rejections.ndjson", database=tmp_path / "test.db")
    assert await export(args) == 1
    assert (tmp_path / "events.ndjson_harness_defect.json").exists()
    assert not (tmp_path / "events.ndjson_environment_failure.json").exists()


async def test_genuine_database_error_is_not_misfiled_as_harness_defect(async_db_session, monkeypatch):
    """P13-REV-03: the round-1 fix's widened except Exception (P13B-06) --
    which presumes anything raised inside run_stage2_fixture_pack's own
    generator is a code/fixture defect -- must not swallow a genuine
    database/infrastructure I/O failure (sqlalchemy.exc.DBAPIError, the
    DBAPI driver's own transport error, as opposed to an application-level
    SQLAlchemy misuse) and reclassify it as a harness defect; it must keep
    propagating unconverted so the caller can classify it as an environment
    failure instead.
    """
    from sqlalchemy.exc import DBAPIError

    async def broken(*a, **k):
        raise DBAPIError("stmt", {}, Exception("connection reset"))
    monkeypatch.setattr(harness, "coordinate_sandbox_action", broken)
    r = Run(async_db_session, [native()])
    with pytest.raises(DBAPIError):
        await r.run()
    assert r.report.harness_defect is None


async def test_evidence_query_outage_preserves_environment_classification(async_db_session, monkeypatch):
    from sqlalchemy.exc import OperationalError

    async def broken_context(*args, **kwargs):
        raise OperationalError("select events", {}, Exception("connection reset"))

    monkeypatch.setattr(harness, "evidence_context", broken_context)
    r = Run(async_db_session, [native()])
    with pytest.raises(OperationalError):
        await r.run()
    assert r.report.harness_defect is None
    assert r.report.environment_failure is None


@pytest.mark.parametrize("exception_type", ["IntegrityError", "DataError", "ProgrammingError"])
async def test_constraint_violation_is_harness_defect_not_environment_failure(
    async_db_session, monkeypatch, exception_type,
):
    """P13-REV2-03: IntegrityError/DataError/ProgrammingError all inherit
    from DBAPIError too, but each means the application or fixture issued a
    bad constraint/value/statement -- a genuine code/fixture defect, never
    an infrastructure outage. The previous blanket "except DBAPIError:
    raise" carve-out (P13-REV-03) folded these into environment_failure
    alongside real connection/availability failures. They must instead
    reach the generic classification and be reported as a harness defect,
    exactly like any other code/fixture bug -- proven here for all three,
    directly alongside the genuine-outage proof above for the same call site.
    """
    import sqlalchemy.exc as sa_exc

    error_cls = getattr(sa_exc, exception_type)

    async def broken(*a, **k):
        raise error_cls("stmt", {}, Exception("synthetic constraint/value/statement defect"))
    monkeypatch.setattr(harness, "coordinate_sandbox_action", broken)
    r = Run(async_db_session, [native()])
    with pytest.raises(harness.HarnessDefect):
        await r.run()
    assert r.report.harness_defect is not None
    assert r.report.harness_defect["type"] == exception_type
    assert r.report.environment_failure is None


@pytest.mark.parametrize("declared", ["CompanyOSTransportError", "CompanyOSDecisionMethodDefect", "UnknownError"])
def test_provider_failures_cannot_be_whitelisted_as_scripted(declared):
    fixtures = pack()
    fixtures["scripted_failure_types"] = [declared]
    with pytest.raises(harness.FixtureAuthoringError, match="unknown scripted failure"):
        harness.validate_pack(fixtures, {WORKFLOW["id"]: WORKFLOW}, [], REGISTRY)


@pytest.mark.parametrize("mode,error", [
    ("transport", CompanyOSTransportError), ("json", CompanyOSMalformedOutputError),
    ("envelope", CompanyOSMalformedOutputError), ("defect", CompanyOSDecisionMethodDefect),
])
async def test_stage2_real_structured_path_types_mocked_transport_results(monkeypatch, mode, error):
    import subprocess

    monkeypatch.setattr("app.agents.base_agent.claude_structured_output_available", lambda: True)
    def process(*args, **kwargs):
        if mode == "transport":
            raise subprocess.CalledProcessError(1, "synthetic", stderr="offline")
        if mode == "defect":
            raise RuntimeError("synthetic method defect")
        return SimpleNamespace(stdout="not JSON" if mode == "json" else "{}")
    monkeypatch.setattr("app.agents.base_agent.subprocess.run", process)
    context = {"workflow": WORKFLOW, "canonical_actions": ACTIONS,
               "canonical_agents": ["email_outreach"], "canonical_handoffs": ["orchestrator"]}
    with pytest.raises(error):
        await BasePolsiaAgent().run_company_os_stage2_decision({}, context)


def _record_provider_context(provider):
    seen = []
    original = provider.prepare

    def prepare(context):
        seen.append(deepcopy(context.get("prior_founder_resolutions", "<absent>")))
        return original(context)
    provider.prepare = prepare
    return seen


@pytest.mark.parametrize("status", ["approved", "rejected", "expired", "cancelled"])
async def test_provider_sees_resolved_founder_history_on_next_decision(async_db_session, status):
    provider = DeterministicMockProvider({"one": [native("pricing_change", "research", "RED"), native("finish", "done")]})
    seen = _record_provider_context(provider)
    r = Run(async_db_session, [], controls=approval_controls(), corrections=[resolution(status)], provider=provider)
    await r.run()
    assert seen == [[], [{"decision_id": "D", "action": "pricing_change", "status": status}]]


async def test_pending_founder_request_is_not_listed_as_resolved(async_db_session):
    r = Run(async_db_session, [native("pricing_change", "research", "RED"), native("finish", "done")],
            controls=approval_controls(), corrections=[resolution("rejected")])
    await r.run()
    row = await async_db_session.scalar(select(CompanyOSSandboxApproval))
    instance = SimpleNamespace(id=row.workflow_instance_id, sandbox_run_id=row.sandbox_run_id)
    assert await harness.prior_approval_resolutions(async_db_session, instance) == [
        {"decision_id": "D", "action": "pricing_change", "status": "rejected"}]
    row.status = "pending"
    await async_db_session.flush()
    assert await harness.prior_approval_resolutions(async_db_session, instance) == []


async def test_founder_history_stays_out_of_persisted_snapshot_and_fingerprint(async_db_session):
    r = Run(async_db_session, [native("pricing_change", "research", "RED"), native("finish", "done")],
            controls=approval_controls(), corrections=[resolution("rejected")])
    await r.run()
    row = await async_db_session.scalar(select(CompanyOSSandboxApproval))
    assert "prior_founder_resolutions" not in row.input_snapshot
    context = await harness.evidence_context(r.pack["cases"][0], [], r.world, REGISTRY, async_db_session)
    assert "prior_founder_resolutions" not in context


async def test_stage2_prompt_carries_founder_history_and_no_repeat_instruction():
    history = [{"decision_id": "D", "action": "pricing_change", "status": "rejected"}]
    context = {"workflow": WORKFLOW, "canonical_actions": ACTIONS, "canonical_agents": ["email_outreach"],
               "canonical_handoffs": ["orchestrator"], "prior_founder_resolutions": history}
    prompts = []

    def transport(prompt, schema):
        prompts.append(prompt)
        return native(), "raw"
    agent = SimpleNamespace(agent_type="email_outreach", company_os_instructions="instructions")
    await stage2_decision(agent, {"case_id": "one"}, context, structured_transport=transport)
    assert '"prior_founder_resolutions": [{"decision_id": "D", "action": "pricing_change", "status": "rejected"}]' in prompts[0]
    assert "do not request that same action again" in prompts[0]
