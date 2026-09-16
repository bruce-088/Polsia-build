"""ChromaDB collection accessor.

chromadb is lazy-imported inside the function body (not at module top level)
so unit tests can import models/services without chromadb installed as a
hard requirement at import time (per CLAUDE.md).
"""

_collection = None


def get_collection():
    global _collection
    if _collection is None:
        import chromadb

        from app.config import settings

        client = chromadb.PersistentClient(path=getattr(settings, "chroma_persist_dir", "./data/chroma"))
        _collection = client.get_or_create_collection("polsia_memory")
    return _collection
