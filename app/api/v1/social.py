from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_api_key
from app.core.database import get_db
from app.models.social import SocialPost

router = APIRouter(prefix="/api/v1/social", tags=["social"], dependencies=[Depends(require_api_key)])


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
