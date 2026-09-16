"""Event publishing over Redis pub/sub.

Imports get_redis by name (from app.core.redis_client import get_redis) —
tests/conftest.py's mock_redis fixture patches app.core.events.get_redis
*separately* from app.core.redis_client.get_redis, which only works if this
module holds its own bound reference to the name (patch-where-used).
"""
import json

from app.core.redis_client import get_redis


async def publish_event(channel: str, payload: dict) -> None:
    redis = get_redis()
    await redis.publish(channel, json.dumps(payload))
