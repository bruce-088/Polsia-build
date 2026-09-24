"""Regression coverage for the Stage 2 production-write boundary."""

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.services.external_access import ExternalAccessBlocked


def _configured_sendgrid(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_mode", True)
    monkeypatch.setattr(settings, "sendgrid_api_key", "configured-test-key")
    monkeypatch.setattr(settings, "sendgrid_from_email", "sender@example.com")

    client = MagicMock()
    sendgrid = ModuleType("sendgrid")
    sendgrid.SendGridAPIClient = client
    helpers = ModuleType("sendgrid.helpers")
    mail = ModuleType("sendgrid.helpers.mail")
    mail.Mail = MagicMock()
    return client, patch.dict(
        sys.modules,
        {
            "sendgrid": sendgrid,
            "sendgrid.helpers": helpers,
            "sendgrid.helpers.mail": mail,
        },
    )


def test_sendgrid_client_is_not_constructed_in_sandbox(monkeypatch):
    from app.services.email_service import send_email

    client, modules = _configured_sendgrid(monkeypatch)
    with modules, pytest.raises(ExternalAccessBlocked, match="sendgrid.email"):
        send_email("recipient@example.com", "Subject", "Body")

    client.assert_not_called()


def test_twitter_client_is_not_constructed_in_sandbox(monkeypatch):
    from app.services.twitter_service import post_tweet

    monkeypatch.setattr(settings, "sandbox_mode", True)
    monkeypatch.setattr(settings, "twitter_api_key", "key")
    monkeypatch.setattr(settings, "twitter_api_secret", "secret")
    monkeypatch.setattr(settings, "twitter_access_token", "token")
    monkeypatch.setattr(settings, "twitter_access_token_secret", "token-secret")

    client = MagicMock()
    tweepy = ModuleType("tweepy")
    tweepy.Client = client
    with patch.dict(sys.modules, {"tweepy": tweepy}), pytest.raises(
        ExternalAccessBlocked, match="twitter.publish"
    ):
        post_tweet("Never publish this")

    client.assert_not_called()


def test_github_repo_is_not_loaded_for_mutation_in_sandbox(monkeypatch):
    from app.services.github_service import open_pr

    monkeypatch.setattr(settings, "sandbox_mode", True)
    monkeypatch.setattr(settings, "github_token", "configured-token")
    monkeypatch.setattr(settings, "github_repo", "owner/repo")

    with patch(
        "app.services.github_service._get_repo",
        side_effect=AssertionError("production repo client must not be loaded"),
    ) as repo_client:
        with pytest.raises(ExternalAccessBlocked, match="github.open_pr"):
            open_pr("README.md", "new", "branch", "title", "body")

    repo_client.assert_not_called()


def test_customer_support_auto_send_wrapper_cannot_bypass_sandbox(monkeypatch):
    from celery_app.tasks.agent_tasks import _send_customer_support_reply

    client, modules = _configured_sendgrid(monkeypatch)
    with modules, pytest.raises(ExternalAccessBlocked, match="sendgrid.email"):
        _send_customer_support_reply(
            to_email="customer@example.com",
            subject="Re: request",
            body="Draft reply",
        )

    client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload", "capability"),
    [
        (
            "/api/v1/emails/send",
            {"to": "recipient@example.com", "subject": "Subject", "body": "Body"},
            "sendgrid.email",
        ),
        (
            "/api/v1/social/posts/publish",
            {"content": "Never publish this"},
            "twitter.publish",
        ),
        (
            "/api/v1/code/open-pr",
            {
                "file_path": "README.md",
                "new_content": "new",
                "pr_title": "Never open this",
            },
            "github.open_pr",
        ),
    ],
)
async def test_human_triggered_write_routes_are_blocked_in_sandbox(
    api_client, auth_headers, monkeypatch, path, payload, capability
):
    monkeypatch.setattr(settings, "sandbox_mode", True)
    monkeypatch.setattr(settings, "sendgrid_api_key", "configured-test-key")
    monkeypatch.setattr(settings, "sendgrid_from_email", "sender@example.com")
    monkeypatch.setattr(settings, "twitter_api_key", "key")
    monkeypatch.setattr(settings, "twitter_api_secret", "secret")
    monkeypatch.setattr(settings, "twitter_access_token", "token")
    monkeypatch.setattr(settings, "twitter_access_token_secret", "token-secret")
    monkeypatch.setattr(settings, "github_token", "configured-token")
    monkeypatch.setattr(settings, "github_repo", "owner/repo")

    response = await api_client.post(path, json=payload, headers=auth_headers)

    assert response.status_code == 400
    assert capability in response.json()["detail"]


@pytest.mark.asyncio
async def test_approval_triggered_send_is_blocked_and_remains_pending(
    api_client, auth_headers, async_db_session, monkeypatch
):
    from app.services import approval_service

    monkeypatch.setattr(settings, "sandbox_mode", True)
    monkeypatch.setattr(settings, "sendgrid_api_key", "configured-test-key")
    monkeypatch.setattr(settings, "sendgrid_from_email", "sender@example.com")

    request = await approval_service.create_approval_request(
        async_db_session,
        requested_by_agent="customer_support",
        decision_type="send_customer_reply",
        risk_level="YELLOW",
        title="Send reply: Cost",
        summary="A reply is awaiting approval.",
        payload={
            "reply_to": "customer@example.com",
            "subject": "Re: Cost",
            "reply_draft": "Draft only.",
        },
    )
    await async_db_session.commit()

    response = await api_client.post(
        f"/api/v1/approvals/{request.id}/approve",
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert "sendgrid.email" in response.json()["detail"]
    persisted = await approval_service.get_approval_request(async_db_session, request.id)
    assert persisted.status == "pending"
