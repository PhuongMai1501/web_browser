"""
api/recovery.py — Background task: detect dead workers, handle orphaned sessions.

Rule:
  queued | assigned (not started) → requeue
  running | waiting_for_user      → mark failed (browser state lost)

Two detection strategies:
  1. Stale heartbeat: worker key exists but last_heartbeat > DEAD_THRESHOLD_S
  2. Orphaned sessions: session in active state but assigned_worker key gone from Redis
     (covers the case where the container is killed and its key expires completely)
"""

import asyncio
import logging

from redis.asyncio import Redis

from store import job_queue, session_store, worker_registry
from store.event_store import push_event_async

_log = logging.getLogger(__name__)

_RECOVERABLE_STATUSES = frozenset({"queued", "assigned"})
_LOST_STATUSES = frozenset({"running", "waiting_for_user"})
_ACTIVE_STATUSES = _RECOVERABLE_STATUSES | _LOST_STATUSES


async def recovery_loop(redis: Redis) -> None:
    """Run as an asyncio background task on API startup."""
    while True:
        try:
            await _run_once(redis)
        except Exception as exc:
            _log.error(f"Recovery loop error: {exc}", exc_info=True)
        await asyncio.sleep(30)


async def _handle_lost_session(redis: Redis, session_id: str, reason: str) -> None:
    _log.warning(f"Recovery: marking session {session_id} failed ({reason})")
    await session_store.update_async(
        redis, session_id,
        status="failed",
        error_msg="Worker crashed mid-session. Please start a new session.",
        finished_at="",
    )
    await push_event_async(redis, session_id, "failed", {
        "code": "WORKER_CRASH",
        "message": "Worker crashed mid-session. Please start a new session.",
    })


async def _handle_recoverable_session(redis: Redis, session_id: str, reason: str) -> None:
    _log.warning(f"Recovery: requeuing session {session_id} ({reason})")
    await session_store.update_async(redis, session_id, status="queued", assigned_worker="")
    await job_queue.push_job(redis, session_id)


async def _run_once(redis: Redis) -> None:
    # Strategy 1: workers with stale heartbeat (key still exists but old timestamp)
    dead_workers = await worker_registry.find_dead(redis)
    for worker_id in dead_workers:
        session_id = await worker_registry.get_current_session(redis, worker_id)
        if not session_id:
            await worker_registry.remove(redis, worker_id)
            continue

        sess = await session_store.get_async(redis, session_id)
        if not sess:
            await worker_registry.remove(redis, worker_id)
            continue

        status = sess.get("status", "")
        reason = f"worker {worker_id} stale heartbeat, status={status}"

        if status in _RECOVERABLE_STATUSES:
            await _handle_recoverable_session(redis, session_id, reason)
        elif status in _LOST_STATUSES:
            await _handle_lost_session(redis, session_id, reason)

        await worker_registry.remove(redis, worker_id)

    # Strategy 2: orphaned sessions — assigned_worker key has expired from Redis
    # (happens when worker container is killed and its key TTL expires)
    await _recover_orphaned_sessions(redis)


async def _recover_orphaned_sessions(redis: Redis) -> None:
    """
    Scan sessions in active states whose assigned_worker key no longer exists.
    This handles worker container crashes where the Redis key expired completely.
    """
    # Scan session:{uuid} keys (exclude sub-keys like session:{id}:buffer)
    all_keys = await redis.keys("session:*")
    session_keys = [k for k in all_keys if k.count(":") == 1]

    for key in session_keys:
        sess = await redis.hgetall(key)
        if not sess:
            continue

        status = sess.get("status", "")
        if status not in _ACTIVE_STATUSES:
            continue

        assigned_worker = sess.get("assigned_worker", "")
        if not assigned_worker:
            # Queued with no worker — already waiting, skip
            continue

        worker_alive = await redis.exists(f"worker:{assigned_worker}")
        if worker_alive:
            continue

        # Worker key gone → orphaned session
        session_id = sess.get("session_id", "")
        if not session_id:
            continue

        reason = f"assigned_worker '{assigned_worker}' key expired (container killed)"

        if status in _RECOVERABLE_STATUSES:
            await _handle_recoverable_session(redis, session_id, reason)
        elif status in _LOST_STATUSES:
            await _handle_lost_session(redis, session_id, reason)
