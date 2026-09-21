from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import require_api_key
from app.services.email_service import send_email

router = APIRouter(prefix="/api/v1/emails", tags=["emails"], dependencies=[Depends(require_api_key)])


class SendEmailRequest(BaseModel):
    to: str
    subject: str
    body: str
    from_email: str | None = None  # override the default sender, e.g. a domain-isolated identity


@router.post("/send")
def send(request: SendEmailRequest):
    """The only code path that ever sends a real email — always an explicit,
    human-triggered call, never invoked automatically by an agent."""
    try:
        message_id = send_email(request.to, request.subject, request.body, from_email=request.from_email)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"SendGrid error: {exc}")
    return {"status": "sent", "sendgrid_message_id": message_id}
