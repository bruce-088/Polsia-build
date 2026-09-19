from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_api_key
from app.core.database import get_db
from app.models.social import SocialPost
from app.services.twitter_service import post_tweet

router = APIRouter(prefix="/api/v1/social", tags=["social"], dependencies=[Depends(require_api_key)])


class PublishPostRequest(BaseModel):
    content: str


@router.post("/posts/publish")
async def publish_post(request: PublishPostRequest, db: AsyncSession = Depends(get_db)):
    """The only code path that ever posts a real tweet — always an
    explicit, human-triggered call, never invoked automatically by an
    agent."""
    try:
        tweet_id = post_tweet(request.content)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Twitter/X error: {exc}")

    post = SocialPost(
        platform="twitter",
        content=request.content,
        status="published",
        tweet_id=tweet_id,
        published_at=datetime.now(timezone.utc),
    )
    db.add(post)
    await db.flush()
    await db.refresh(post)
    return {"status": "published", "tweet_id": tweet_id, "post_id": post.id}


@router.get("/posts")
async def list_posts(status: str | None = None, db: AsyncSession = Depends(get_db)):
    stmt = select(SocialPost)
    if status is not None:
        stmt = stmt.where(SocialPost.status == status)
    result = await db.execute(stmt)
    return [
        {
            "id": p.id,
            "platform": p.platform,
            "content": p.content,
            "status": p.status,
            "tweet_id": p.tweet_id,
            "scheduled_for": p.scheduled_for,
            "published_at": p.published_at,
        }
        for p in result.scalars().all()
    ]
