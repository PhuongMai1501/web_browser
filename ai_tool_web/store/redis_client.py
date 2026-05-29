"""
state/redis_client.py — Redis connection management.

- get_async_redis(): shared async connection pool (for API routes, SSE)
- get_sync_redis():  fresh sync client per call (for worker threads)

Backward-compat với Redis cũ (< 6.0) — vd Redis 3.2.12 ở 1 số deploy:
- protocol=2: dùng RESP2 thay vì RESP3 (skip HELLO command — Redis < 6.0 không hỗ trợ)
- client_no_setinfo=True: skip `CLIENT SETINFO` lib metadata (Redis < 7.2 không hỗ trợ;
  redis-py 5.0.1+ mặc định gửi → cần param này để tắt)

Redis 6.0+/7.0+ vẫn hoạt động bình thường với RESP2 (backward compat ở Redis).
"""

import redis as _sync_redis
from redis.asyncio import ConnectionPool, Redis

from config import REDIS_URL

_async_pool: ConnectionPool | None = None


def _supports_client_no_setinfo() -> bool:
    """redis-py >= 5.0.1 mới có param `client_no_setinfo`. Image cũ có thể vẫn
    pin redis-py 5.0.0 (param không tồn tại) → TypeError khi truyền.
    Check version runtime để conditional pass kwarg.
    """
    try:
        ver = tuple(int(x) for x in _sync_redis.__version__.split(".")[:3])
        return ver >= (5, 0, 1)
    except Exception:
        return False


# Build sẵn extra kwargs theo redis-py version. Tránh check lại mỗi connection.
_EXTRA_KW: dict = {"client_no_setinfo": True} if _supports_client_no_setinfo() else {}


def get_async_redis() -> Redis:
    global _async_pool
    if _async_pool is None:
        _async_pool = ConnectionPool.from_url(
            REDIS_URL,
            decode_responses=True,
            protocol=2,                  # Skip HELLO (compat Redis < 6.0)
            **_EXTRA_KW,                 # client_no_setinfo nếu redis-py >= 5.0.1
        )
    return Redis(connection_pool=_async_pool)


def get_sync_redis() -> _sync_redis.Redis:
    """Return a new sync Redis client. Caller must call .close() when done."""
    return _sync_redis.from_url(
        REDIS_URL,
        decode_responses=True,
        protocol=2,
        **_EXTRA_KW,
    )
