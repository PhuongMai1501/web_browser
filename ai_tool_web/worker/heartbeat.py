"""
worker/heartbeat.py — Async heartbeat task for a browser worker.

Responsibilities:
1. Refresh worker registry TTL every LOCK_RENEW_S seconds
2. Renew session lock (lock:session:{id}) while holding a session

Must run as an asyncio Task alongside the main worker loop.
"""

import asyncio
import logging

from redis.asyncio import Redis

from config import LOCK_RENEW_S, LOCK_TTL_S
from store import worker_registry

_log = logging.getLogger(__name__)


async def run(worker_id: str, redis: Redis) -> None:
    """Run forever — cancel this task to stop the worker gracefully."""
    while True:
        try:
            current = await worker_registry.get_current_session(redis, worker_id)
            await worker_registry.update(
                redis,
                worker_id,
                status="busy" if current else "idle",
                current_session=current,
            )
            if current:
                renewed = await redis.set(
                    f"lock:session:{current}",
                    worker_id,
                    ex=LOCK_TTL_S,
                    xx=True,   # only update if key exists — detect stolen lock
                )
                if not renewed:
                    _log.warning(
                        f"[{worker_id}] Lock for session {current} expired or stolen — "
                        "session will be marked failed by recovery"
                    )
        except Exception as exc:
            _log.error(f"[{worker_id}] Heartbeat error: {exc}")
        await asyncio.sleep(LOCK_RENEW_S)
