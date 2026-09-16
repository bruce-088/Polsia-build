from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_api_key
from app.config import settings
from app.core.database import get_db
from app.models.finance import ExpenseRecord, RevenueSnapshot, StripeEvent

router = APIRouter(prefix="/api/v1/finance", tags=["finance"], dependencies=[Depends(require_api_key)])

# Webhook is Stripe-authenticated (signature), not API-key authenticated.
webhook_router = APIRouter(prefix="/api/v1/finance", tags=["finance"])


@router.get("/summary")
async def finance_summary(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(RevenueSnapshot).order_by(RevenueSnapshot.snapshot_date.desc()).limit(1))
    snap = result.scalars().first()
    if snap is None:
        return {
            "mrr_cents": 0,
            "arr_cents": 0,
            "active_subscribers": 0,
            "last_snapshot_date": None,
            "total_ad_spend_usd": 0,
            "total_expenses_month_cents": 0,
            "stripe_balance_cents": 0,
        }
    return {
        "mrr_cents": snap.mrr_cents,
        "arr_cents": snap.arr_cents,
        "active_subscribers": snap.active_subscribers,
        "last_snapshot_date": snap.snapshot_date,
        "total_ad_spend_usd": 0,
        "total_expenses_month_cents": 0,
        "stripe_balance_cents": snap.stripe_balance_cents,
    }


@router.get("/revenue")
async def revenue(limit: int = 30, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(RevenueSnapshot).order_by(RevenueSnapshot.snapshot_date.desc()).limit(limit)
    )
    snaps = result.scalars().all()
    return [
        {
            "snapshot_date": s.snapshot_date,
            "mrr_cents": s.mrr_cents,
            "arr_cents": s.arr_cents,
            "active_subscribers": s.active_subscribers,
        }
        for s in snaps
    ]


@router.get("/expenses")
async def expenses(category: str | None = None, db: AsyncSession = Depends(get_db)):
    stmt = select(ExpenseRecord)
    if category is not None:
        stmt = stmt.where(ExpenseRecord.category == category)
    result = await db.execute(stmt)
    return [
        {
            "id": e.id,
            "category": e.category,
            "vendor": e.vendor,
            "amount_cents": e.amount_cents,
            "currency": e.currency,
            "date": e.date,
        }
        for e in result.scalars().all()
    ]


@router.get("/events")
async def events(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(StripeEvent))
    return [
        {
            "id": e.id,
            "stripe_event_id": e.stripe_event_id,
            "event_type": e.event_type,
            "amount_cents": e.amount_cents,
            "status": e.status,
        }
        for e in result.scalars().all()
    ]


@webhook_router.post("/stripe/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="stripe-signature"),
    db: AsyncSession = Depends(get_db),
):
    if not settings.stripe_webhook_secret:
        raise HTTPException(status_code=500, detail="Stripe webhook secret not configured")

    payload = await request.body()

    import stripe

    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, settings.stripe_webhook_secret)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")

    db.add(
        StripeEvent(
            stripe_event_id=event["id"],
            event_type=event["type"],
            raw_payload=event,
        )
    )
    await db.flush()

    return {"received": True}
