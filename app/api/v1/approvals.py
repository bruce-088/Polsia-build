from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_api_key
from app.core.database import get_db
from app.models.approval import ApprovalRequest
from app.services import approval_service
from app.services.email_service import send_email

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"], dependencies=[Depends(require_api_key)])


def _to_dict(r: ApprovalRequest) -> dict:
    return {
        "id": r.id,
        "task_id": r.task_id,
        "requested_by_agent": r.requested_by_agent,
        "decision_type": r.decision_type,
        "risk_level": r.risk_level,
        "title": r.title,
        "summary": r.summary,
        "evidence": r.evidence,
        "options": r.options,
        "recommended_option": r.recommended_option,
        "cost_or_commitment": r.cost_or_commitment,
        "status": r.status,
        "resolved_by": r.resolved_by,
        "resolved_at": r.resolved_at,
        "created_at": r.created_at,
    }


@router.get("")
async def list_approvals(status: str | None = "pending", db: AsyncSession = Depends(get_db)):
    requests = await approval_service.list_approval_requests(db, status=status)
    return [_to_dict(r) for r in requests]


@router.post("/{approval_id}/approve")
async def approve(approval_id: int, db: AsyncSession = Depends(get_db)):
    """Approving IS the trigger — if this decision carries an executable
    payload (e.g. a pending customer reply), it's performed now, for
    real, as part of approval. Never before."""
    request = await approval_service.get_approval_request(db, approval_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if request.status != "pending":
        raise HTTPException(status_code=400, detail=f"Already resolved (status={request.status})")

    if request.decision_type == "send_customer_reply" and request.payload:
        try:
            send_email(
                to_email=request.payload["reply_to"],
                subject=request.payload["subject"],
                body=request.payload["reply_draft"],
                from_email=request.payload.get("from_email"),
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Approved, but send failed: {exc}")

    resolved = await approval_service.resolve_approval_request(db, approval_id, status="approved")
    return _to_dict(resolved)


@router.post("/{approval_id}/reject")
async def reject(approval_id: int, db: AsyncSession = Depends(get_db)):
    request = await approval_service.get_approval_request(db, approval_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if request.status != "pending":
        raise HTTPException(status_code=400, detail=f"Already resolved (status={request.status})")

    resolved = await approval_service.resolve_approval_request(db, approval_id, status="rejected")
    return _to_dict(resolved)
