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


def _supports_protocol_kwarg() -> bool:
    """`protocol` kwarg thêm từ redis-py 5.0.0. redis-py 4.x reject kwarg này."""
    try:
        ver = tuple(int(x) for x in _sync_redis.__version__.split(".")[:3])
        return ver >= (5, 0, 0)
    except Exception:
        return False


# Build extra kwargs theo redis-py version. Skip `client_no_setinfo` hoàn toàn
# vì redis-py 5.0.1 có bug plumbing (param forward xuống AbstractConnection.__init__
# → TypeError). Nếu redis-py 4.x → KHÔNG gửi HELLO/SETINFO mặc định → compat
# Redis 3.2.12 OK luôn.
_EXTRA_KW: dict = {"protocol": 2} if _supports_protocol_kwarg() else {}


def get_async_redis() -> Redis:
    global _async_pool
    if _async_pool is None:
        _async_pool = ConnectionPool.from_url(
            REDIS_URL,
            decode_responses=True,
            **_EXTRA_KW,                 # protocol=2 nếu redis-py 5.x; bỏ trống nếu 4.x
        )
    return Redis(connection_pool=_async_pool)


def get_sync_redis() -> _sync_redis.Redis:
    """Return a new sync Redis client. Caller must call .close() when done."""
    return _sync_redis.from_url(
        REDIS_URL,
        decode_responses=True,
        **_EXTRA_KW,
    )
