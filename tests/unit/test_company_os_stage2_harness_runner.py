"""Infrastructure-only driver proof. All decisions/transports are deterministic mocks.

These are small synthetic test scenarios, never smoke/scoreable fixture runs.
The coordinator, approval service, persistence and dispatch ledger are real.
"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.services.company_os_stage2_harness_runner as harness
from app.agents.base_agent import BasePolsiaAgent
from app.agents.company_os_stage2_provider import (
    CompanyOSDecisionMethodDefect,
    CompanyOSMalformedOutputError,
    CompanyOSTransportError,
    OwnerRoutedProvider,
)
from app.config import settings
from app.models.company_os_sandbox import CompanyOSSandboxApproval, CompanyOSSandboxEvent
from app.services.company_os_sandbox_approval_service import (
    resolve_sandbox_approval,
    resume_sandbox_approval,
)
from app.services.company_os_sandbox_service import create_sandbox_run, create_workflow_instance
from app.services.company_os_stage2_inputs import build_stage2_snapshot
from app.services.company_os_synthetic_adapters import SyntheticAdapter
from scripts.stage2_harness_export import DeterministicMockProvider
from tests.unit.test_company_os_stage2_required_paths import FIXTURES, REGISTRY, SCHEMA

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
ACTIONS = ["score_against_icp", "revisit", "send_outreach", "close", "finish", "pricing_change", "bypass_opt_out"]


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
            "exception_type": "SandboxCoordinatorError", "expects": "duplicate, stale, or terminal workflow delivery"}
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


async def test_unconsumed_control_command_is_explicit_runtime_failure(async_db_session):
    r = Run(async_db_session, [native("finish", "done")], controls=approval_controls(), corrections=[resolution()])
    await r.run()
    assert r.report.outcomes["one"] == "unconsumed_control_command"
    assert r.report.failures == [{"case_id": "one", "outcome": "unconsumed_control_command",
                                 "commands": ["resolve"], "last_outcome": "terminal"}]


async def test_alternating_decisions_hit_budget_not_infinite_loop(async_db_session):
    decisions = [native() if i % 2 == 0 else native("revisit", "research") for i in range(harness.MAX_DECISIONS_PER_CASE)]
    r = Run(async_db_session, decisions)
    events = await r.run()
    assert len(events) == harness.MAX_DECISIONS_PER_CASE
    assert r.report.outcomes["one"] == "decision_budget_exhausted"
    assert r.report.failures == [{"case_id": "one", "outcome": "decision_budget_exhausted"}]


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


@pytest.mark.parametrize("mutate,match", [
    (lambda p: p["cases"][0]["inputs"][0].update(after=["bare"]), "initial signals|ambiguous"),
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
    assert runner.record(None, row) is None and runner.record(None, resume) is None
    assert len(await r.rows()) == len(r.pairs)


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
    seen = []
    provider = DeterministicMockProvider({"one": [native(), send(), native("close", "done")],
                                         "two": [native("finish", "done")]})
    prepare = provider.prepare
    def capture(context):
        seen.append((context["case_id"], [s["delivery_id"] for s in context["signals"]]))
        return prepare(context)
    provider.prepare = capture
    r = Run(async_db_session, [], fixtures=fixtures, provider=provider)
    # Second case waits for a queue fact, then completes without a second send.
    r.workflows["integration_failure_recovery"] = {
        **deepcopy(WORKFLOW), "id": "integration_failure_recovery", "entity_type": "integration_operation"}
    r.workflows["integration_failure_recovery"]["transitions"][-1]["requirements"] = ["queue_arrived"]
    recovery["inputs"][1]["facts"]["queue_arrived"] = True
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
    assert events[0]["entity_id"] == origin["entity_id"]
    assert events[1]["entity_id"] == "queue-one"  # round robin, not case exhaustion


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
            pair = runner.record(state, event)
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
        "exception_type": "SandboxApprovalError", "expects": "workflow changed since approval request"}
    r, runner, state = await start_internal(async_db_session, controls=controls)
    r.provider.decisions["one"] = [native("pricing_change", "research", "RED")]
    await runner.advance(state)
    approval = await runner.approval(state, "D")
    await runner.call(state, resolve_sandbox_approval, approval_id=approval.id, status="approved",
        founder_id="founder", founder_minutes=1, resolution_key="resolve", event_schema=SCHEMA, workflow=WORKFLOW)
    state.instance.version += 1  # deliberately stale DB delivery, not a fabricated event
    await async_db_session.flush()
    event = await runner.call(state, resume_sandbox_approval, approval_id=approval.id, **runner.options_for(state))
    assert event is None
    assert len(await r.rows()) == 2
    assert r.report.service_rejections[0]["exception_type"] == "SandboxApprovalError"


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
    assert runner.record(state, first) is not None
    second = await runner.resolve(state, "D")
    assert second.event_id == first.event_id
    assert runner.record(state, second) is None
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
