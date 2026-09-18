"""Live Stripe polling — deliberately read-only (Balance.retrieve +
Subscription.list), never writes to Stripe. Persists/upserts today's
RevenueSnapshot so the finance agent can summarize real numbers."""
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.finance import RevenueSnapshot

# How many times a price's billing interval recurs per month, used to
# normalize any subscription price into a monthly (MRR) contribution.
_INTERVAL_TO_MONTHLY_MULTIPLIER = {"day": 30, "week": 4.345, "month": 1, "year": 1 / 12}


async def poll_stripe_snapshot(db: AsyncSession) -> RevenueSnapshot | None:
    """Pull live balance + active-subscription MRR from Stripe and upsert
    today's RevenueSnapshot. No-op (returns None) if no Stripe key is
    configured, so this is always safe to call."""
    if not settings.stripe_secret_key:
        return None

    import stripe

    stripe.api_key = settings.stripe_secret_key

    balance = stripe.Balance.retrieve()
    stripe_balance_cents = sum(b["amount"] for b in balance["available"])

    mrr_cents = 0.0
    active_subscribers = 0
    for sub in stripe.Subscription.list(status="active", limit=100).auto_paging_iter():
        active_subscribers += 1
        for item in sub["items"]["data"]:
            price = item["price"]
            recurring = price.get("recurring") or {}
            interval = recurring.get("interval", "month")
            interval_count = recurring.get("interval_count") or 1
            multiplier = _INTERVAL_TO_MONTHLY_MULTIPLIER.get(interval, 1) / interval_count
            mrr_cents += (price["unit_amount"] or 0) * (item["quantity"] or 1) * multiplier

    mrr_cents = int(mrr_cents)
    arr_cents = mrr_cents * 12

    today = date.today()
    result = await db.execute(select(RevenueSnapshot).where(RevenueSnapshot.snapshot_date == today))
    snap = result.scalars().first()
    if snap is None:
        snap = RevenueSnapshot(snapshot_date=today)
        db.add(snap)

    snap.mrr_cents = mrr_cents
    snap.arr_cents = arr_cents
    snap.active_subscribers = active_subscribers
    snap.stripe_balance_cents = stripe_balance_cents

    await db.flush()
    await db.refresh(snap)
    return snap
