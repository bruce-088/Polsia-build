"""Redis client accessor.

Deliberately a plain (sync) function, not async def: it returns a
redis.asyncio.Redis instance whose *methods* are async, matching
tests/conftest.py's mock_redis fixture (`mocker.patch(..., return_value=mock)`
with no `new_callable=AsyncMock` on the patch itself).
"""
import redis.asyncio as redis

from app.config import settings

_client: "redis.Redis | None" = None


def get_redis() -> "redis.Redis":
    global _client
    if _client is None:
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client
