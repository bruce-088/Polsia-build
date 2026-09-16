import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.chroma_client import get_collection
from app.models.memory import MemoryEntry


async def store_memory(
    db: AsyncSession,
    category: str,
    title: str,
    content: str,
    source: str | None = None,
    tags: list[str] | None = None,
) -> MemoryEntry:
    chroma_id = uuid.uuid4().hex

    collection = get_collection()
    collection.add(
        documents=[content],
        metadatas=[{"category": category, "title": title, "source": source or ""}],
        ids=[chroma_id],
    )

    entry = MemoryEntry(
        category=category,
        title=title,
        content=content,
        source=source,
        tags=tags,
        chroma_id=chroma_id,
    )
    db.add(entry)
    await db.flush()
    await db.refresh(entry)
    return entry


async def search_memory(db: AsyncSession, query: str, n_results: int = 5) -> list[dict]:
    collection = get_collection()
    raw = collection.query(query_texts=[query], n_results=n_results)

    documents = raw.get("documents", [[]])[0]
    metadatas = raw.get("metadatas", [[]])[0]
    ids = raw.get("ids", [[]])[0]
    distances = raw.get("distances", [[]])[0]

    results = []
    for doc, meta, doc_id, distance in zip(documents, metadatas, ids, distances):
        results.append(
            {
                "content": doc,
                "category": meta.get("category"),
                "title": meta.get("title"),
                "id": doc_id,
                "distance": distance,
            }
        )
    return results


async def list_memories(db: AsyncSession, category: str | None = None) -> list[MemoryEntry]:
    stmt = select(MemoryEntry)
    if category is not None:
        stmt = stmt.where(MemoryEntry.category == category)
    result = await db.execute(stmt)
    return list(result.scalars().all())
