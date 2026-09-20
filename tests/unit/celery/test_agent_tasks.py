"""Test Celery agent task dispatch (without real broker)."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_crew_factory_returns_result():
    """Test that crew_factory.run_agent_for_task dispatches correctly."""
    from app.agents.crew_factory import run_agent_for_task

    with patch("app.agents.social_media.agent.SocialMediaAgent.run") as mock_run:
        mock_run.return_value = {"summary": "Mocked result", "tweets": []}
        result = run_agent_for_task(
            "social_media",
            {"title": "Post tweets", "description": None},
            {"company": {"name": "Test Co"}},
        )
    assert result["summary"] == "Mocked result"


def test_crew_factory_raises_on_unknown_agent():
    from app.agents.crew_factory import run_agent_for_task
    with pytest.raises(ValueError, match="Unknown agent"):
        run_agent_for_task("nonexistent_agent", {}, {})


def test_run_email_sweep_creates_task_per_unread_message():
    from celery_app.tasks.agent_tasks import run_email_sweep

    fake_messages = [
        {"from": "a@hvacco.com", "subject": "Pricing question", "body": "How much?", "message_id": "<1>", "received_at": None},
        {"from": "b@hvacco.com", "subject": "Are you spam?", "body": "Unsubscribe me", "message_id": "<2>", "received_at": None},
    ]
    with patch("app.services.email_inbox_service.fetch_unread_messages", return_value=fake_messages):
        with patch("celery_app.tasks.agent_tasks._create_and_run") as mock_create:
            run_email_sweep()

    assert mock_create.call_count == 2
    first_call = mock_create.call_args_list[0]
    assert first_call.args[0] == "customer_support"
    assert "Pricing question" in first_call.args[1]
    assert "How much?" in first_call.kwargs["description"]


def test_run_email_sweep_noop_when_imap_not_configured():
    from celery_app.tasks.agent_tasks import run_email_sweep

    with patch(
        "app.services.email_inbox_service.fetch_unread_messages",
        side_effect=RuntimeError("IMAP is not configured"),
    ):
        with patch("celery_app.tasks.agent_tasks._create_and_run") as mock_create:
            run_email_sweep()

    mock_create.assert_not_called()
