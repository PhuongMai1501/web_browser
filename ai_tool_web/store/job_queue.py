"""
state/job_queue.py — Job queue via Redis List (FIFO).

Convention: RPUSH to enqueue (tail), BLPOP from head (FIFO).
Key: pending_jobs
"""

from redis.asyncio import Redis

_QUEUE_KEY = "pending_jobs"
_HARD_CAP = 100   # refuse new sessions if queue exceeds this


async def push_job(redis: Redis, session_id: str) -> int:
    """Enqueue a session. Returns new queue length."""
    return await redis.rpush(_QUEUE_KEY, session_id)


async def pop_job(redis: Redis, timeout: int = 30) -> str | None:
    """
    Block until a job is available or timeout expires.
    Returns session_id or None if timed out.
    """
    result = await redis.blpop(_QUEUE_KEY, timeout=timeout)
    return result[1] if result else None


async def queue_length(redis: Redis) -> int:
    return await redis.llen(_QUEUE_KEY)


async def is_over_capacity(redis: Redis) -> bool:
    return await queue_length(redis) >= _HARD_CAP
