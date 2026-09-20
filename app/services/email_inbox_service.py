"""Inbound email polling via IMAP. Read-only: fetches unread messages and
lets marking them \\Seen happen as a natural side effect of the fetch
(standard IMAP behavior for a non-PEEK body fetch) — never sends, deletes,
or otherwise modifies the mailbox. Sending a reply always goes through
app/api/v1/emails.py's explicit /send endpoint, never this module."""
import email
import imaplib
from email.header import decode_header

from app.config import settings


def fetch_unread_messages(limit: int = 10) -> list[dict]:
    """Fetch unread inbox messages via IMAP. Raises RuntimeError if IMAP
    isn't configured. Returns a list of {"from", "subject", "body",
    "message_id", "received_at"}."""
    if not all([settings.imap_host, settings.imap_username, settings.imap_password]):
        raise RuntimeError("IMAP is not configured (imap_host/imap_username/imap_password)")

    conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        conn.login(settings.imap_username, settings.imap_password)
        conn.select("INBOX")

        status, data = conn.search(None, "UNSEEN")
        if status != "OK" or not data or not data[0]:
            return []

        messages = []
        for msg_id in data[0].split()[:limit]:
            status, msg_data = conn.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            parsed = email.message_from_bytes(msg_data[0][1])
            messages.append(
                {
                    "from": _decode_header(parsed.get("From", "")),
                    "subject": _decode_header(parsed.get("Subject", "")),
                    "body": _extract_body(parsed),
                    "message_id": parsed.get("Message-ID"),
                    "received_at": parsed.get("Date"),
                }
            )
        return messages
    finally:
        conn.logout()


def _decode_header(value: str) -> str:
    parts = decode_header(value)
    return "".join(
        part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part
        for part, enc in parts
    )


def _extract_body(parsed: email.message.Message) -> str:
    if parsed.is_multipart():
        for part in parsed.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                part.get("Content-Disposition")
            ):
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                return payload.decode(charset, errors="replace") if payload else ""
        return ""
    charset = parsed.get_content_charset() or "utf-8"
    payload = parsed.get_payload(decode=True)
    return payload.decode(charset, errors="replace") if payload else ""
