"""Test email_inbox_service.fetch_unread_messages — IMAP mocked entirely,
no real network/mailbox access in unit tests."""
import email.message
import pytest
from unittest.mock import MagicMock, patch

from app.services.email_inbox_service import fetch_unread_messages


def test_fetch_unread_messages_raises_when_not_configured():
    from app.config import settings

    original = (settings.imap_host, settings.imap_username, settings.imap_password)
    settings.imap_host = ""
    settings.imap_username = ""
    settings.imap_password = ""
    try:
        with pytest.raises(RuntimeError):
            fetch_unread_messages()
    finally:
        settings.imap_host, settings.imap_username, settings.imap_password = original


def _make_raw_email(subject: str, from_addr: str, body: str) -> bytes:
    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["Message-ID"] = "<test-123@example.com>"
    msg["Date"] = "Mon, 1 Jan 2026 00:00:00 -0000"
    msg.set_content(body)
    return msg.as_bytes()


def test_fetch_unread_messages_parses_real_imap_response():
    from app.config import settings

    original = (settings.imap_host, settings.imap_username, settings.imap_password)
    settings.imap_host = "imap.example.com"
    settings.imap_username = "user@example.com"
    settings.imap_password = "secret"
    try:
        raw = _make_raw_email("Question about pricing", "prospect@hvacco.com", "Hi, how much does this cost?")

        mock_conn = MagicMock()
        mock_conn.search.return_value = ("OK", [b"1"])
        mock_conn.fetch.return_value = ("OK", [(b"1 (RFC822 {123}", raw)])

        with patch("imaplib.IMAP4_SSL", return_value=mock_conn):
            messages = fetch_unread_messages()

        assert len(messages) == 1
        assert messages[0]["subject"] == "Question about pricing"
        assert messages[0]["from"] == "prospect@hvacco.com"
        assert "how much does this cost" in messages[0]["body"]
        mock_conn.login.assert_called_once_with("user@example.com", "secret")
        mock_conn.logout.assert_called_once()
    finally:
        settings.imap_host, settings.imap_username, settings.imap_password = original


def test_fetch_unread_messages_returns_empty_when_no_unread():
    from app.config import settings

    original = (settings.imap_host, settings.imap_username, settings.imap_password)
    settings.imap_host = "imap.example.com"
    settings.imap_username = "user@example.com"
    settings.imap_password = "secret"
    try:
        mock_conn = MagicMock()
        mock_conn.search.return_value = ("OK", [b""])

        with patch("imaplib.IMAP4_SSL", return_value=mock_conn):
            messages = fetch_unread_messages()

        assert messages == []
    finally:
        settings.imap_host, settings.imap_username, settings.imap_password = original
