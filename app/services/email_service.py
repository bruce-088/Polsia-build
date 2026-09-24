"""SendGrid email sending behind the shared production-write boundary.

Callers are the explicit email API, the narrow customer-support auto-reply
path, and approved customer replies. Agent drafting paths do not call this
service. Sandbox mode blocks all callers before the SendGrid client is
constructed."""
from app.config import settings
from app.services.external_access import require_production_write_allowed


def send_email(to_email: str, subject: str, body: str, from_email: str | None = None) -> str | None:
    """Send a real email via SendGrid. Raises RuntimeError if SendGrid isn't
    configured, so a misconfigured send fails loudly instead of silently
    no-op-ing. Returns the SendGrid message id on success."""
    require_production_write_allowed("sendgrid.email")

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
