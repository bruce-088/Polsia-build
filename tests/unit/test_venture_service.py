"""Venture service: enforcement of delegation, metrics, idempotency and
the RED phase-advance path."""
import pytest

from app.agents.venture import lifecycle
from app.models.agent_run import AgentRun
from app.models.task import Task
from app.services import approval_service, venture_service
from app.services.venture_service import GateNotPassed, VentureConflict, VentureError


async def make_venture(db, name="Venue Inquiry Desk", **kwargs):
    return await venture_service.create_venture(
        db,
        name=name,
        idea="AI desk that answers inquiries for independent event venues",
        target_customer="Owner-run U.S. venues",
        business_model="$450/mo",
        **kwargs,
    )


def gate_metrics(phase_key, **overrides):
    metrics = [{"metric": c.metric, "value": c.threshold} for c in lifecycle.get_phase(phase_key).exit_gate]
    for item in metrics:
        if item["metric"] in overrides:
            item["value"] = overrides[item["metric"]]
    return metrics


async def run_task(db, venture, agent_type, output, **kwargs):
    task = await venture_service.create_venture_task(db, venture, agent_type, **kwargs)
    db.add(AgentRun(task_id=task.id, agent_type=agent_type, status="completed", output=output))
    await db.flush()
    return task


@pytest.mark.asyncio
async def test_create_venture_starts_in_research_validation(async_db_session):
    venture = await make_venture(async_db_session)
    assert venture.phase == "research_validation"
    assert venture.status == "exploring"
    assert venture.history[0]["event"] == "created"
    snap = venture_service.snapshot(venture)
    assert snap["gate"]["passed"] is False
    assert snap["gate"]["gate"] == "Validated"


@pytest.mark.asyncio
async def test_duplicate_names_and_bad_overrides_are_rejected(async_db_session):
    await make_venture(async_db_session)
    with pytest.raises(VentureConflict):
        await make_venture(async_db_session)
    with pytest.raises(VentureError):
        await make_venture(async_db_session, name="Other", gate_overrides={"launch": {"nope": 1}})
    with pytest.raises(VentureError):
        await venture_service.create_venture(async_db_session, name=" ", idea="x")


@pytest.mark.asyncio
async def test_metrics_record_with_caller_authority(async_db_session):
    venture = await make_venture(async_db_session)
    founder = await venture_service.record_metrics(
        async_db_session, venture, [{"metric": "competitors_analyzed", "value": 4}], verified=True, recorded_by="founder"
    )
    tracker = await venture_service.record_metrics(
        async_db_session,
        venture,
        [
            {"metric": "smoke_test_visitors", "value": 180, "source": "analytics"},
            {"metric": "committed_signals", "value": 2},
            {"metric": "BAD", "value": 1, "source": "x"},
        ],
        verified=False,
        recorded_by="tracker:task-1",
    )
    assert founder["accepted"] == ["competitors_analyzed"]
    assert tracker["accepted"] == ["smoke_test_visitors"]
    assert len(tracker["rejected"]) == 2
    assert venture.metrics["competitors_analyzed"]["verified"] is True
    assert venture.metrics["smoke_test_visitors"]["verified"] is False
    gate = venture_service.gate_status(venture)
    assert "smoke_test_visitors" in gate["unverified"]

    result = await venture_service.verify_metrics(async_db_session, venture, ["smoke_test_visitors", "ghost"])
    assert result == {"verified": ["smoke_test_visitors"], "unknown": ["ghost"]}
    assert venture_service.gate_status(venture)["unverified"] == []


@pytest.mark.asyncio
async def test_only_permitted_agents_can_be_started(async_db_session):
    venture = await make_venture(async_db_session)
    for agent_type in ("commander", "tracker", "predictor", "research_validation"):
        task = await venture_service.create_venture_task(async_db_session, venture, agent_type)
        assert task.task_metadata["venture"]["id"] == venture.id
        assert task.task_metadata["venture_phase"] == "research_validation"
    for agent_type in ("launch", "email_outreach", "orchestrator", "made_up"):
        with pytest.raises(VentureError):
            await venture_service.create_venture_task(async_db_session, venture, agent_type)


@pytest.mark.asyncio
async def test_killed_ventures_accept_no_new_work(async_db_session):
    venture = await make_venture(async_db_session)
    await venture_service.set_status(async_db_session, venture, "killed", "no demand")
    with pytest.raises(VentureConflict):
        await venture_service.create_venture_task(async_db_session, venture, "commander")
    with pytest.raises(VentureError):
        await venture_service.set_status(async_db_session, venture, "exploring")


@pytest.mark.asyncio
async def test_commander_output_creates_only_permitted_tasks_once(async_db_session):
    venture = await make_venture(async_db_session)
    output = {
        "summary": "Validate demand first",
        "dispatch": [
            {"agent_type": "research_validation", "title": "Plan validation"},
            {"agent_type": "predictor", "title": "Score the idea", "mode": "score_idea"},
            {"agent_type": "email_outreach", "title": "Sneaky direct send"},
            {"agent_type": "launch", "title": "Skip ahead"},
        ],
        "founder_decisions": [
            {"decision": "Approve $50 for a domain", "why": "smoke test page"},
            {"decision": "Advance to launch", "why": "should not appear"},
        ],
    }
    commander_task = await run_task(async_db_session, venture, "commander", output)
    summary = await venture_service.apply_agent_output(async_db_session, venture, commander_task, output)

    assert len(summary["created_task_ids"]) == 2
    assert len(summary["rejected"]) == 2
    created = [await async_db_session.get(Task, task_id) for task_id in summary["created_task_ids"]]
    assert [t.agent_type for t in created] == ["research_validation", "predictor"]
    assert created[1].task_metadata["mode"] == "score_idea"
    assert all(t.task_metadata["parent_task_id"] == commander_task.id for t in created)

    approvals = await approval_service.list_approval_requests(async_db_session, status="pending")
    assert [a.risk_level for a in approvals] == ["RED"]
    assert "domain" in approvals[0].title
    assert venture.status == "active"

    again = await venture_service.apply_agent_output(async_db_session, venture, commander_task, output)
    assert again["already_applied"] is True
    tasks = (await async_db_session.execute(Task.__table__.select())).all()
    assert len(tasks) == 3


@pytest.mark.asyncio
async def test_phase_lead_output_creates_worker_tasks_with_venture_brief(async_db_session):
    venture = await make_venture(async_db_session)
    output = {
        "proposed_tasks": [
            {"agent_type": "competitor_research", "title": "Map venue CRMs", "description": "List 5 competitors"},
            {"agent_type": "ads_management", "title": "Run ads"},
        ]
    }
    lead_task = await run_task(async_db_session, venture, "research_validation", output)
    summary = await venture_service.apply_agent_output(async_db_session, venture, lead_task, output)
    assert len(summary["created_task_ids"]) == 1
    assert "cannot be run" in summary["rejected"][0]["reason"]
    worker = await async_db_session.get(Task, summary["created_task_ids"][0])
    assert worker.agent_type == "competitor_research"
    assert worker.description.startswith("[Venture: Venue Inquiry Desk")
    assert "List 5 competitors" in worker.description
    assert "venture" not in worker.task_metadata  # workers get the brief, not the snapshot


@pytest.mark.asyncio
async def test_stale_phase_output_is_rejected(async_db_session):
    venture = await make_venture(async_db_session)
    lead_task = await run_task(async_db_session, venture, "research_validation", {"proposed_tasks": []})
    venture.phase = "launch"
    with pytest.raises(VentureConflict):
        await venture_service.apply_agent_output(async_db_session, venture, lead_task, {"proposed_tasks": []})


@pytest.mark.asyncio
async def test_tracker_and_predictor_outputs_are_stored(async_db_session):
    venture = await make_venture(async_db_session)
    tracker_output = {
        "metric_updates": [{"metric": "problem_evidence_sources", "value": 6, "source": "research memo", "verified": True}]
    }
    tracker_task = await run_task(async_db_session, venture, "tracker", tracker_output)
    await venture_service.apply_agent_output(async_db_session, venture, tracker_task, tracker_output)
    assert venture.metrics["problem_evidence_sources"]["verified"] is False

    score = lifecycle.score_idea({name: 4 for name in lifecycle.IDEA_RUBRIC})
    predictor_output = {"mode": "score_idea", "idea_score": score}
    predictor_task = await run_task(async_db_session, venture, "predictor", predictor_output, mode="score_idea")
    summary = await venture_service.apply_agent_output(async_db_session, venture, predictor_task, predictor_output)
    assert summary["idea_score"] == 80.0
    assert venture.scores["idea"]["weighted_score"] == 80.0


@pytest.mark.asyncio
async def test_output_from_another_venture_is_rejected(async_db_session):
    first = await make_venture(async_db_session)
    second = await make_venture(async_db_session, name="Grant Match Weekly")
    task = await run_task(async_db_session, first, "commander", {"dispatch": []})
    with pytest.raises(VentureError):
        await venture_service.apply_agent_output(async_db_session, second, task, {"dispatch": []})


@pytest.mark.asyncio
async def test_advance_requires_passing_gate_then_founder_approval(async_db_session):
    venture = await make_venture(async_db_session)
    with pytest.raises(GateNotPassed) as excinfo:
        await venture_service.request_phase_advance(async_db_session, venture)
    assert excinfo.value.gate["missing"]

    await venture_service.record_metrics(
        async_db_session, venture, gate_metrics("research_validation"), verified=True, recorded_by="founder"
    )
    request = await venture_service.request_phase_advance(async_db_session, venture)
    assert request.risk_level == "RED"
    assert request.payload == {"venture_id": venture.id, "from_phase": "research_validation", "to_phase": "launch"}
    assert len(request.evidence) == len(lifecycle.get_phase("research_validation").exit_gate)
    assert (await venture_service.request_phase_advance(async_db_session, venture)).id == request.id

    with pytest.raises(VentureConflict):
        await venture_service.apply_phase_advance(async_db_session, venture, request.id)

    await approval_service.resolve_approval_request(async_db_session, request.id, status="approved")
    advanced = await venture_service.apply_phase_advance(async_db_session, venture, request.id)
    assert advanced.phase == "launch"
    assert advanced.status == "active"
    replay = await venture_service.apply_phase_advance(async_db_session, venture, request.id)
    assert replay.phase == "launch"
    assert [h["event"] for h in replay.history].count("phase_advanced") == 1


@pytest.mark.asyncio
async def test_advance_rechecks_gate_after_approval(async_db_session):
    venture = await make_venture(async_db_session)
    await venture_service.record_metrics(
        async_db_session, venture, gate_metrics("research_validation"), verified=True, recorded_by="founder"
    )
    request = await venture_service.request_phase_advance(async_db_session, venture)
    await approval_service.resolve_approval_request(async_db_session, request.id, status="approved")
    # New, unverified evidence lands after approval — the gate no longer passes.
    await venture_service.record_metrics(
        async_db_session,
        venture,
        [{"metric": "committed_signals", "value": 5, "source": "tracker"}],
        verified=False,
        recorded_by="tracker:task-9",
    )
    with pytest.raises(GateNotPassed):
        await venture_service.apply_phase_advance(async_db_session, venture, request.id)
    assert venture.phase == "research_validation"


@pytest.mark.asyncio
async def test_rejected_or_foreign_approvals_cannot_advance(async_db_session):
    venture = await make_venture(async_db_session)
    other = await make_venture(async_db_session, name="Other")
    for v in (venture, other):
        await venture_service.record_metrics(
            async_db_session, v, gate_metrics("research_validation"), verified=True, recorded_by="founder"
        )
    request = await venture_service.request_phase_advance(async_db_session, venture)
    with pytest.raises(VentureError):
        await venture_service.apply_phase_advance(async_db_session, other, request.id)
    await approval_service.resolve_approval_request(async_db_session, request.id, status="rejected")
    with pytest.raises(VentureConflict):
        await venture_service.apply_phase_advance(async_db_session, venture, request.id)
    with pytest.raises(VentureError):
        await venture_service.apply_phase_advance(async_db_session, venture, 9999)


@pytest.mark.asyncio
async def test_gate_overrides_apply_per_phase(async_db_session):
    venture = await make_venture(
        async_db_session, gate_overrides={"research_validation": {"smoke_test_visitors": 50}}
    )
    await venture_service.record_metrics(
        async_db_session,
        venture,
        gate_metrics("research_validation", smoke_test_visitors=60),
        verified=True,
        recorded_by="founder",
    )
    assert venture_service.gate_status(venture)["passed"] is True


@pytest.mark.asyncio
async def test_ideation_output_creates_exploring_ventures_once(async_db_session):
    await make_venture(async_db_session)
    task = await venture_service.create_ideation_task(async_db_session, "Online only, no licenses", count=3)
    output = {
        "ideas": [
            {"name": "Grant Match Weekly", "one_liner": "Grant matches for small nonprofits", "smoke_test": "free matches"},
            {"name": "Venue Inquiry Desk", "one_liner": "duplicate of an existing venture"},
            {"name": "", "one_liner": "nameless"},
        ]
    }
    summary = await venture_service.apply_ideation_output(async_db_session, task, output)
    assert len(summary["created_venture_ids"]) == 1
    assert len(summary["skipped"]) == 2
    created = await venture_service.get_venture(async_db_session, summary["created_venture_ids"][0])
    assert created.status == "exploring"
    assert created.source == f"ideation:task-{task.id}"
    assert "Smoke test: free matches" in created.idea
    again = await venture_service.apply_ideation_output(async_db_session, task, output)
    assert again["already_applied"] is True


BBUILDER_BRIEF = {
    "name": "InboxZero for Dentists",
    "mission": "Help solo dental practices recover lost patients",
    "vision": "Reach $10,000/month in net profit within 12 months",
    "description": "A web app that connects to the practice inbox and sends recall sequences.",
    "target_market": "Solo dental practices in the US with 1-3 chairs.",
    "value_prop": "Recover lost patients with an automated recall sequence.",
    "pricing_model": {"plans": [{"name": "Starter plan", "price": 49}, {"name": "Growth plan", "price": 149}]},
    "goals": {"first_30_days": ["Launch landing page"]},
    "kpis": {"mrr_usd": 0, "target_mrr_usd": 10000},
    "product_type": "SaaS",
    "industry": "Dental practice marketing SaaS",
    "agent_plan": {"orchestrator": "Plan each day"},
    "onboarding_prompt": "InboxZero for Dentists is a saas...",
    "fit": {"score": 88, "signals": ["Digital-only product"], "warnings": []},
    "source": "bbuilder-saved-idea",
}


@pytest.mark.asyncio
async def test_import_bbuilder_brief_creates_exploring_venture(async_db_session):
    venture = await venture_service.import_bbuilder_brief(async_db_session, BBUILDER_BRIEF)
    assert venture.name == "InboxZero for Dentists"
    assert venture.phase == "research_validation"
    assert venture.status == "exploring"
    assert venture.source == "bbuilder"
    assert venture.business_model == "Starter plan $49/mo, Growth plan $149/mo"
    assert venture.target_customer.startswith("Solo dental practices")
    assert "Value proposition: Recover lost patients" in venture.idea
    assert venture.scores["bbuilder_fit"]["score"] == 88.0
    assert venture.metrics == {}  # bbuilder's placeholder KPIs never become gate evidence
    assert venture.history[-1] == {**venture.history[-1], "event": "imported", "origin": "bbuilder-saved-idea"}
    with pytest.raises(VentureConflict):
        await venture_service.import_bbuilder_brief(async_db_session, BBUILDER_BRIEF)


@pytest.mark.asyncio
async def test_import_bbuilder_brief_requires_a_name_and_idea(async_db_session):
    with pytest.raises(VentureError):
        await venture_service.import_bbuilder_brief(async_db_session, {"name": "", "description": "x"})
    with pytest.raises(VentureError):
        await venture_service.import_bbuilder_brief(async_db_session, {"name": "Empty"})
