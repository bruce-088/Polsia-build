"""Redis client accessor.

Deliberately a plain (sync) function, not async def: it returns a
redis.asyncio.Redis instance whose *methods* are async, matching
tests/conftest.py's mock_redis fixture (`mocker.patch(..., return_value=mock)`
with no `new_callable=AsyncMock` on the patch itself).

Deliberately NOT a cached singleton: redis.asyncio connections are bound to
the event loop active when they're first used, but Celery tasks each run in
their own fresh event loop via asyncio.run() — a cached client from a prior
task's (now-closed) loop raises "Event loop is closed" on the next task
that calls it. A new client per call avoids that; redis.from_url() doesn't
open a real connection until first used, so this is cheap.
"""
import redis.asyncio as redis

from app.config import settings


def get_redis() -> "redis.Redis":
    return redis.from_url(settings.redis_url, decode_responses=True)
