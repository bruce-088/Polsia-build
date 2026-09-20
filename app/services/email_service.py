"""SendGrid email sending. Two callers only: app/api/v1/emails.py's
POST /send (explicitly human-triggered, any recipient), and
celery_app/tasks/agent_tasks.py's narrow customer_support auto-reply path
(only reachable after app/services/auto_send_policy.py's multi-layer gate
passes — see that module for what "narrow" actually means). email_outreach
and every other agent-drafted reply still only ever produces a draft."""
from app.config import settings


def send_email(to_email: str, subject: str, body: str, from_email: str | None = None) -> str | None:
    """Send a real email via SendGrid. Raises RuntimeError if SendGrid isn't
    configured, so a misconfigured send fails loudly instead of silently
    no-op-ing. Returns the SendGrid message id on success."""
    if not settings.sendgrid_api_key or not settings.sendgrid_from_email:
        raise RuntimeError("SendGrid is not configured (sendgrid_api_key/sendgrid_from_email)")

    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    message = Mail(
        from_email=from_email or settings.sendgrid_from_email,
        to_emails=to_email,
        subject=subject,
        plain_text_content=body,
    )
    client = SendGridAPIClient(settings.sendgrid_api_key)
    response = client.send(message)
    return response.headers.get("X-Message-Id")
