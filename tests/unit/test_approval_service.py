"""Test approval_service — the Founder Inbox's CRUD/resolve logic."""
import pytest

from app.services import approval_service


@pytest.mark.asyncio
async def test_create_approval_request_defaults(async_db_session):
    request = await approval_service.create_approval_request(
        async_db_session,
        requested_by_agent="customer_support",
        decision_type="send_customer_reply",
        risk_level="YELLOW",
        title="Send reply: Cost",
        summary="A customer reply is drafted and awaiting review.",
    )
    await async_db_session.commit()

    assert request.id is not None
    assert request.status == "pending"
    assert request.evidence == []
    assert request.options == []


@pytest.mark.asyncio
async def test_list_approval_requests_filters_by_status(async_db_session):
    await approval_service.create_approval_request(
        async_db_session,
        requested_by_agent="customer_support",
        decision_type="send_customer_reply",
        risk_level="YELLOW",
        title="Pending one",
        summary="s",
    )
    resolved = await approval_service.create_approval_request(
        async_db_session,
        requested_by_agent="customer_support",
        decision_type="send_customer_reply",
        risk_level="YELLOW",
        title="Resolved one",
        summary="s",
    )
    await async_db_session.commit()
    await approval_service.resolve_approval_request(async_db_session, resolved.id, status="approved")
    await async_db_session.commit()

    pending = await approval_service.list_approval_requests(async_db_session, status="pending")
    assert len(pending) == 1
    assert pending[0].title == "Pending one"

    all_requests = await approval_service.list_approval_requests(async_db_session, status=None)
    assert len(all_requests) == 2


@pytest.mark.asyncio
async def test_resolve_approval_request_sets_fields(async_db_session):
    request = await approval_service.create_approval_request(
        async_db_session,
        requested_by_agent="customer_support",
        decision_type="send_customer_reply",
        risk_level="YELLOW",
        title="Send reply: Cost",
        summary="s",
    )
    await async_db_session.commit()

    resolved = await approval_service.resolve_approval_request(
        async_db_session, request.id, status="rejected", resolved_by="bruce"
    )
    await async_db_session.commit()

    assert resolved.status == "rejected"
    assert resolved.resolved_by == "bruce"
    assert resolved.resolved_at is not None


@pytest.mark.asyncio
async def test_resolve_nonexistent_request_returns_none(async_db_session):
    result = await approval_service.resolve_approval_request(async_db_session, 999, status="approved")
    assert result is None
