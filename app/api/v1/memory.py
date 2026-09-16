from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_api_key
from app.core.database import get_db
from app.services import memory_service

router = APIRouter(prefix="/api/v1/memory", tags=["memory"], dependencies=[Depends(require_api_key)])


class MemoryCreate(BaseModel):
    category: str
    title: str
    content: str
    tags: list[str] | None = None


@router.post("", status_code=201)
async def create_memory(body: MemoryCreate, db: AsyncSession = Depends(get_db)):
    entry = await memory_service.store_memory(
        db,
        category=body.category,
        title=body.title,
        content=body.content,
        tags=body.tags,
    )
    return {
        "id": entry.id,
        "category": entry.category,
        "title": entry.title,
        "content": entry.content,
        "tags": entry.tags,
    }


@router.get("")
async def list_or_search_memory(q: str | None = None, category: str | None = None, db: AsyncSession = Depends(get_db)):
    if q:
        return await memory_service.search_memory(db, q)
    entries = await memory_service.list_memories(db, category=category)
    return [
        {"id": e.id, "category": e.category, "title": e.title, "content": e.content, "tags": e.tags}
        for e in entries
    ]
