"""The one and only gate that lets an agent-drafted reply get sent without
a human explicitly clicking send. Deliberately narrow and layered — every
check below must pass independently; a single failure keeps the reply as
a draft (the safe default everywhere else in this codebase). Only
customer_support inbound replies are eligible; nothing else in the system
is allowed to auto-send.

Layer 1: the agent's own self-assessment (auto_send_eligible) — necessary
but not sufficient, since LLM self-assessment can be overconfident.
Layer 2: a hard keyword denylist over both the inbound message and the
draft — catches refunds/cancellations/legal/complaints regardless of what
the LLM thinks, independent of its judgment.
Layer 3: a daily rate limit — a circuit breaker against prompt drift or a
bug causing a burst of auto-sends, independent of any single message's
content.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun

DENYLIST_KEYWORDS = [
    "refund",
    "cancel",
    "discount",
    "unsubscribe",
    "legal",
    "lawsuit",
    "attorney",
    "complaint",
    "chargeback",
    "sue",
    "angry",
    "unacceptable",
]

DAILY_AUTO_SEND_LIMIT = 5


def _contains_denylisted_content(*texts: str) -> bool:
    combined = " ".join(t or "" for t in texts).lower()
    return any(keyword in combined for keyword in DENYLIST_KEYWORDS)


async def _auto_sends_today(db: AsyncSession) -> int:
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(AgentRun).where(
            AgentRun.agent_type == "customer_support",
            AgentRun.started_at >= today_start,
        )
    )
    runs = result.scalars().all()
    return sum(1 for r in runs if isinstance(r.output, dict) and r.output.get("auto_sent") is True)


async def is_safe_to_auto_send(db: AsyncSession, inbound_body: str, draft: dict) -> bool:
    """True only if every independent layer agrees this specific reply is
    safe to send without a human. Any single failed layer returns False —
    there is no override."""
    if not draft.get("auto_send_eligible"):
        return False
    if _contains_denylisted_content(inbound_body, draft.get("reply_draft", "")):
        return False
    if await _auto_sends_today(db) >= DAILY_AUTO_SEND_LIMIT:
        return False
    return True
