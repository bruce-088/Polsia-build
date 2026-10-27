"""Test /api/v1/ventures — founder controls for the venture lifecycle."""
from unittest.mock import patch

import pytest

from app.agents.venture import lifecycle
from app.models.agent_run import AgentRun

ENQUEUE = "app.api.v1.ventures._enqueue"


async def create(api_client, auth_headers, name="Grant Match Weekly"):
    resp = await api_client.post(
        "/api/v1/ventures",
        headers=auth_headers,
        json={"name": name, "idea": "Weekly grant matches for small nonprofits", "business_model": "$99/mo"},
    )
    assert resp.status_code == 201
    return resp.json()


def passing(phase_key):
    return [{"metric": c.metric, "value": c.threshold} for c in lifecycle.get_phase(phase_key).exit_gate]


@pytest.mark.asyncio
async def test_requires_auth(api_client):
    assert (await api_client.get("/api/v1/ventures")).status_code == 401


@pytest.mark.asyncio
async def test_lifecycle_definition(api_client, auth_headers):
    resp = await api_client.get("/api/v1/ventures/lifecycle", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert [p["key"] for p in data["phases"]] == list(lifecycle.PHASE_ORDER)
    assert data["shared_agents"] == ["commander", "tracker", "predictor"]
    assert "automation_fit" in data["idea_rubric"]


@pytest.mark.asyncio
async def test_create_list_get(api_client, auth_headers):
    venture = await create(api_client, auth_headers)
    assert venture["phase"] == "research_validation"
    assert venture["gate"]["passed"] is False
    listed = (await api_client.get("/api/v1/ventures", headers=auth_headers)).json()
    assert [v["name"] for v in listed] == ["Grant Match Weekly"]
    got = await api_client.get(f"/api/v1/ventures/{venture['id']}", headers=auth_headers)
    assert got.json()["id"] == venture["id"]
    assert (await api_client.get("/api/v1/ventures/999", headers=auth_headers)).status_code == 404
    dup = await api_client.post(
        "/api/v1/ventures", headers=auth_headers, json={"name": "Grant Match Weekly", "idea": "again"}
    )
    assert dup.status_code == 409


@pytest.mark.asyncio
async def test_run_queues_permitted_agents_only(api_client, auth_headers):
    venture = await create(api_client, auth_headers)
    with patch(ENQUEUE) as enqueue:
        ok = await api_client.post(
            f"/api/v1/ventures/{venture['id']}/run", headers=auth_headers, json={"agent_type": "commander"}
        )
        bad = await api_client.post(
            f"/api/v1/ventures/{venture['id']}/run", headers=auth_headers, json={"agent_type": "email_outreach"}
        )
    assert ok.status_code == 202
    assert ok.json()["status"] == "queued"
    enqueue.assert_called_once_with([ok.json()["task_id"]])
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_ideate_queues_research_validation(api_client, auth_headers):
    with patch(ENQUEUE) as enqueue:
        resp = await api_client.post(
            "/api/v1/ventures/ideate", headers=auth_headers, json={"constraints": "online only", "count": 5}
        )
    assert resp.status_code == 202
    enqueue.assert_called_once()
    task = (await api_client.get(f"/api/v1/tasks/{resp.json()['task_id']}", headers=auth_headers)).json()
    assert task["agent_type"] == "research_validation"


@pytest.mark.asyncio
async def test_founder_metrics_are_verified_and_move_the_gate(api_client, auth_headers):
    venture = await create(api_client, auth_headers)
    resp = await api_client.post(
        f"/api/v1/ventures/{venture['id']}/metrics",
        headers=auth_headers,
        json={"metrics": passing("research_validation")},
    )
    assert resp.status_code == 200
    assert resp.json()["gate"]["passed"] is True


@pytest.mark.asyncio
async def test_full_advance_flow_through_founder_inbox(api_client, auth_headers):
    venture = await create(api_client, auth_headers)
    vid = venture["id"]
    blocked = await api_client.post(f"/api/v1/ventures/{vid}/advance/request", headers=auth_headers)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["gate"]["passed"] is False

    await api_client.post(
        f"/api/v1/ventures/{vid}/metrics", headers=auth_headers, json={"metrics": passing("research_validation")}
    )
    requested = await api_client.post(f"/api/v1/ventures/{vid}/advance/request", headers=auth_headers)
    assert requested.status_code == 201
    approval_id = requested.json()["approval_id"]

    early = await api_client.post(f"/api/v1/ventures/{vid}/advance", headers=auth_headers, json={"approval_id": approval_id})
    assert early.status_code == 409

    inbox = (await api_client.get("/api/v1/approvals", headers=auth_headers)).json()
    assert inbox[0]["decision_type"] == "advance_venture_phase"
    assert inbox[0]["risk_level"] == "RED"
    approved = await api_client.post(f"/api/v1/approvals/{approval_id}/approve", headers=auth_headers)
    assert approved.status_code == 200

    advanced = await api_client.post(
        f"/api/v1/ventures/{vid}/advance", headers=auth_headers, json={"approval_id": approval_id}
    )
    assert advanced.status_code == 200
    assert advanced.json()["phase"] == "launch"
    assert advanced.json()["gate"]["gate"] == "Launched"


@pytest.mark.asyncio
async def test_apply_commander_output_creates_and_queues_tasks(api_client, auth_headers, async_db_session):
    venture = await create(api_client, auth_headers)
    vid = venture["id"]
    with patch(ENQUEUE):
        run = await api_client.post(f"/api/v1/ventures/{vid}/run", headers=auth_headers, json={"agent_type": "commander"})
    task_id = run.json()["task_id"]

    not_ready = await api_client.post(f"/api/v1/ventures/{vid}/tasks/{task_id}/apply", headers=auth_headers)
    assert not_ready.status_code == 409

    async_db_session.add(
        AgentRun(
            task_id=task_id,
            agent_type="commander",
            status="completed",
            output={"dispatch": [{"agent_type": "research_validation", "title": "Plan validation"}]},
        )
    )
    await async_db_session.flush()
    with patch(ENQUEUE) as enqueue:
        applied = await api_client.post(f"/api/v1/ventures/{vid}/tasks/{task_id}/apply", headers=auth_headers)
        replay = await api_client.post(f"/api/v1/ventures/{vid}/tasks/{task_id}/apply", headers=auth_headers)
    assert applied.status_code == 200
    assert applied.json()["queued"] is True
    assert len(applied.json()["created_task_ids"]) == 1
    enqueue.assert_called_once_with(applied.json()["created_task_ids"])
    assert replay.json()["already_applied"] is True
    assert replay.json()["queued"] is False


@pytest.mark.asyncio
async def test_status_changes(api_client, auth_headers):
    venture = await create(api_client, auth_headers)
    resp = await api_client.post(
        f"/api/v1/ventures/{venture['id']}/status", headers=auth_headers, json={"status": "paused", "reason": "focus"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"
    bad = await api_client.post(
        f"/api/v1/ventures/{venture['id']}/status", headers=auth_headers, json={"status": "exploring"}
    )
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_import_bbuilder_brief(api_client, auth_headers):
    brief = {
        "name": "Recall Desk",
        "description": "Automated patient recall for solo dental practices.",
        "target_market": "Solo dental practices",
        "value_prop": "Recover lost patients",
        "pricing_model": {"plans": [{"name": "Starter", "price": 49}]},
        "fit": {"score": 74, "signals": [], "warnings": ["Needs a human closer"]},
        "source": "bbuilder-saved-idea",
    }
    resp = await api_client.post("/api/v1/ventures/import/bbuilder", headers=auth_headers, json=brief)
    assert resp.status_code == 201
    data = resp.json()
    assert data["source"] == "bbuilder"
    assert data["business_model"] == "Starter $49/mo"
    assert data["scores"]["bbuilder_fit"]["warnings"] == ["Needs a human closer"]
    bad = await api_client.post("/api/v1/ventures/import/bbuilder", headers=auth_headers, json={"name": "x"})
    assert bad.status_code == 422
