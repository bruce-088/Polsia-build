"""Test POST /api/v1/emails/send — the sole path that ever sends a real
email, always explicit/human-triggered, never called by an agent."""
import pytest
from unittest.mock import patch


@pytest.mark.asyncio
async def test_send_email_requires_auth(api_client):
    resp = await api_client.post(
        "/api/v1/emails/send",
        json={"to": "test@example.com", "subject": "Hi", "body": "Hello"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_send_email_not_configured(api_client, auth_headers):
    """No sendgrid_api_key/sendgrid_from_email set — fails loudly, not silently."""
    from app.config import settings

    original_key, original_from = settings.sendgrid_api_key, settings.sendgrid_from_email
    settings.sendgrid_api_key = ""
    settings.sendgrid_from_email = ""
    try:
        resp = await api_client.post(
            "/api/v1/emails/send",
            json={"to": "test@example.com", "subject": "Hi", "body": "Hello"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
    finally:
        settings.sendgrid_api_key = original_key
        settings.sendgrid_from_email = original_from


@pytest.mark.asyncio
async def test_send_email_success(api_client, auth_headers):
    with patch("app.api.v1.emails.send_email", return_value="msg-123") as mock_send:
        resp = await api_client.post(
            "/api/v1/emails/send",
            json={"to": "test@example.com", "subject": "Hi", "body": "Hello"},
            headers=auth_headers,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "sent"
    assert data["sendgrid_message_id"] == "msg-123"
    mock_send.assert_called_once_with("test@example.com", "Hi", "Hello", from_email=None)


@pytest.mark.asyncio
async def test_send_email_with_from_email_override(api_client, auth_headers):
    with patch("app.api.v1.emails.send_email", return_value="msg-124") as mock_send:
        resp = await api_client.post(
            "/api/v1/emails/send",
            json={
                "to": "prospect@hvacco.com",
                "subject": "Hi",
                "body": "Hello",
                "from_email": "bruce@outreach.acqivo.com",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 200
    mock_send.assert_called_once_with(
        "prospect@hvacco.com", "Hi", "Hello", from_email="bruce@outreach.acqivo.com"
    )


@pytest.mark.asyncio
async def test_send_email_sendgrid_error(api_client, auth_headers):
    with patch("app.api.v1.emails.send_email", side_effect=Exception("SendGrid 5xx")):
        resp = await api_client.post(
            "/api/v1/emails/send",
            json={"to": "test@example.com", "subject": "Hi", "body": "Hello"},
            headers=auth_headers,
        )
    assert resp.status_code == 502
