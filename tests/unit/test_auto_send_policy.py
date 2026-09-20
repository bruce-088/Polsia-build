"""Test auto_send_policy.is_safe_to_auto_send — the sole gate that lets a
customer_support draft get sent without a human. Every layer must be
independently testable and independently sufficient to block a send."""
import pytest

from app.models.agent_run import AgentRun
from app.services.auto_send_policy import DAILY_AUTO_SEND_LIMIT, is_safe_to_auto_send


@pytest.mark.asyncio
async def test_blocks_when_not_marked_eligible(async_db_session):
    draft = {"reply_draft": "Sure, it costs $1,250/month.", "auto_send_eligible": False}
    assert await is_safe_to_auto_send(async_db_session, "How much does it cost?", draft) is False


@pytest.mark.asyncio
async def test_blocks_on_denylisted_inbound_content(async_db_session):
    draft = {"reply_draft": "Happy to help with that.", "auto_send_eligible": True}
    assert await is_safe_to_auto_send(async_db_session, "I want a refund immediately.", draft) is False


@pytest.mark.asyncio
async def test_blocks_on_denylisted_draft_content(async_db_session):
    draft = {"reply_draft": "I can offer you a 20% discount.", "auto_send_eligible": True}
    assert await is_safe_to_auto_send(async_db_session, "What can you do for me?", draft) is False


@pytest.mark.asyncio
async def test_blocks_when_daily_limit_reached(async_db_session):
    for _ in range(DAILY_AUTO_SEND_LIMIT):
        async_db_session.add(
            AgentRun(agent_type="customer_support", status="completed", output={"auto_sent": True})
        )
    await async_db_session.commit()

    draft = {"reply_draft": "Sure, it costs $1,250/month.", "auto_send_eligible": True}
    assert await is_safe_to_auto_send(async_db_session, "How much does it cost?", draft) is False


@pytest.mark.asyncio
async def test_allows_when_every_layer_passes(async_db_session):
    draft = {"reply_draft": "Sure, it costs $1,250/month.", "auto_send_eligible": True}
    assert await is_safe_to_auto_send(async_db_session, "How much does it cost?", draft) is True


@pytest.mark.asyncio
async def test_non_auto_sent_runs_today_dont_count_toward_limit(async_db_session):
    """Runs from manual triggers or drafts that weren't auto-sent must not
    count toward the daily cap — only ones actually marked auto_sent."""
    for _ in range(DAILY_AUTO_SEND_LIMIT + 5):
        async_db_session.add(
            AgentRun(agent_type="customer_support", status="completed", output={"auto_sent": False})
        )
    await async_db_session.commit()

    draft = {"reply_draft": "Sure, it costs $1,250/month.", "auto_send_eligible": True}
    assert await is_safe_to_auto_send(async_db_session, "How much does it cost?", draft) is True
