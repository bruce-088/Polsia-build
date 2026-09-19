"""SendGrid email sending. Deliberately has no agent-triggered caller —
the only path that reaches this is app/api/v1/emails.py's POST /send,
which is a separate, explicitly human-triggered action. Agents
(email_outreach, customer_support) only ever produce drafts."""
from app.config import settings


def send_email(to_email: str, subject: str, body: str) -> str | None:
    """Send a real email via SendGrid. Raises RuntimeError if SendGrid isn't
    configured, so a misconfigured send fails loudly instead of silently
    no-op-ing. Returns the SendGrid message id on success."""
    if not settings.sendgrid_api_key or not settings.sendgrid_from_email:
        raise RuntimeError("SendGrid is not configured (sendgrid_api_key/sendgrid_from_email)")

    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    message = Mail(
        from_email=settings.sendgrid_from_email,
        to_emails=to_email,
        subject=subject,
        plain_text_content=body,
    )
    client = SendGridAPIClient(settings.sendgrid_api_key)
    response = client.send(message)
    return response.headers.get("X-Message-Id")
