"""Test GET/POST /api/v1/approvals/* — the Founder Inbox API."""
import pytest
from unittest.mock import patch

from app.services import approval_service


@pytest.mark.asyncio
async def test_list_approvals_requires_auth(api_client):
    resp = await api_client.get("/api/v1/approvals")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_approvals_defaults_to_pending(api_client, auth_headers, async_db_session):
    await approval_service.create_approval_request(
        async_db_session,
        requested_by_agent="customer_support",
        decision_type="send_customer_reply",
        risk_level="YELLOW",
        title="Send reply: Cost",
        summary="s",
    )
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/approvals", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_approve_with_email_payload_sends_and_resolves(api_client, auth_headers, async_db_session):
    request = await approval_service.create_approval_request(
        async_db_session,
        requested_by_agent="customer_support",
        decision_type="send_customer_reply",
        risk_level="YELLOW",
        title="Send reply: Cost",
        summary="s",
        payload={"reply_to": "prospect@hvacco.com", "subject": "Re: Cost", "reply_draft": "It costs $1,250/mo."},
    )
    await async_db_session.commit()

    with patch("app.api.v1.approvals.send_email", return_value="msg-1") as mock_send:
        resp = await api_client.post(f"/api/v1/approvals/{request.id}/approve", headers=auth_headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    mock_send.assert_called_once_with(
        to_email="prospect@hvacco.com",
        subject="Re: Cost",
        body="It costs $1,250/mo.",
        from_email=None,
    )


@pytest.mark.asyncio
async def test_approve_send_failure_returns_502(api_client, auth_headers, async_db_session):
    request = await approval_service.create_approval_request(
        async_db_session,
        requested_by_agent="customer_support",
        decision_type="send_customer_reply",
        risk_level="YELLOW",
        title="Send reply: Cost",
        summary="s",
        payload={"reply_to": "prospect@hvacco.com", "subject": "Re: Cost", "reply_draft": "It costs $1,250/mo."},
    )
    await async_db_session.commit()

    with patch("app.api.v1.approvals.send_email", side_effect=Exception("SendGrid 5xx")):
        resp = await api_client.post(f"/api/v1/approvals/{request.id}/approve", headers=auth_headers)

    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_reject_resolves_without_sending(api_client, auth_headers, async_db_session):
    request = await approval_service.create_approval_request(
        async_db_session,
        requested_by_agent="customer_support",
        decision_type="send_customer_reply",
        risk_level="YELLOW",
        title="Send reply: Cost",
        summary="s",
        payload={"reply_to": "prospect@hvacco.com", "subject": "Re: Cost", "reply_draft": "It costs $1,250/mo."},
    )
    await async_db_session.commit()

    with patch("app.api.v1.approvals.send_email") as mock_send:
        resp = await api_client.post(f"/api/v1/approvals/{request.id}/reject", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_approve_already_resolved_returns_400(api_client, auth_headers, async_db_session):
    request = await approval_service.create_approval_request(
        async_db_session,
        requested_by_agent="customer_support",
        decision_type="send_customer_reply",
        risk_level="YELLOW",
        title="Send reply: Cost",
        summary="s",
    )
    await async_db_session.commit()
    await approval_service.resolve_approval_request(async_db_session, request.id, status="approved")
    await async_db_session.commit()

    resp = await api_client.post(f"/api/v1/approvals/{request.id}/approve", headers=auth_headers)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_approve_nonexistent_returns_404(api_client, auth_headers):
    resp = await api_client.post("/api/v1/approvals/999/approve", headers=auth_headers)
    assert resp.status_code == 404
