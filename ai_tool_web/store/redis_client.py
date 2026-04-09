"""
state/redis_client.py — Redis connection management.

- get_async_redis(): shared async connection pool (for API routes, SSE)
- get_sync_redis():  fresh sync client per call (for worker threads)
"""

import redis as _sync_redis
from redis.asyncio import ConnectionPool, Redis

from config import REDIS_URL

_async_pool: ConnectionPool | None = None


def get_async_redis() -> Redis:
    global _async_pool
    if _async_pool is None:
        _async_pool = ConnectionPool.from_url(REDIS_URL, decode_responses=True)
    return Redis(connection_pool=_async_pool)


def get_sync_redis() -> _sync_redis.Redis:
    """Return a new sync Redis client. Caller must call .close() when done."""
    return _sync_redis.from_url(REDIS_URL, decode_responses=True)
