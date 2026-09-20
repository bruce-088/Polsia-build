"""The Founder Inbox — decisions an agent isn't authorized to make on its
own. See app/models/approval.py for the schema rationale."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.approval import ApprovalRequest


async def create_approval_request(
    db: AsyncSession,
    requested_by_agent: str,
    decision_type: str,
    risk_level: str,
    title: str,
    summary: str,
    evidence: list | None = None,
    options: list | None = None,
    recommended_option: str | None = None,
    cost_or_commitment: str | None = None,
    payload: dict | None = None,
    task_id: int | None = None,
) -> ApprovalRequest:
    request = ApprovalRequest(
        task_id=task_id,
        requested_by_agent=requested_by_agent,
        decision_type=decision_type,
        risk_level=risk_level,
        title=title,
        summary=summary,
        evidence=evidence or [],
        options=options or [],
        recommended_option=recommended_option,
        cost_or_commitment=cost_or_commitment,
        payload=payload,
    )
    db.add(request)
    await db.flush()
    await db.refresh(request)
    return request


async def list_approval_requests(db: AsyncSession, status: str | None = None) -> list[ApprovalRequest]:
    stmt = select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc())
    if status is not None:
        stmt = stmt.where(ApprovalRequest.status == status)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_approval_request(db: AsyncSession, request_id: int) -> ApprovalRequest | None:
    return await db.get(ApprovalRequest, request_id)


async def resolve_approval_request(
    db: AsyncSession, request_id: int, status: str, resolved_by: str = "founder"
) -> ApprovalRequest | None:
    request = await db.get(ApprovalRequest, request_id)
    if request is None:
        return None
    request.status = status
    request.resolved_by = resolved_by
    request.resolved_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(request)
    return request
