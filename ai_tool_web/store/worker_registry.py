"""
state/worker_registry.py — Worker heartbeat and ownership tracking.

Schema: Redis Hash  key=worker:{worker_id}  TTL=30s
  {
    "worker_id": "worker-1",
    "status": "idle" | "busy",
    "current_session": "<session_id>" | "",
    "started_at": "<ISO>",
    "last_heartbeat": "<ISO>"
  }
"""

import json
from datetime import datetime, timezone

from redis.asyncio import Redis

_WORKER_KEY = "worker:{}"
_WORKER_TTL_S = 30          # heartbeat must refresh within this window
_DEAD_THRESHOLD_S = 45      # worker considered dead after 45s without heartbeat


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def register(
    redis: Redis,
    worker_id: str,
    status: str,
    current_session: str,
    started_at: str = "",
) -> None:
    key = _WORKER_KEY.format(worker_id)
    info = {
        "worker_id": worker_id,
        "status": status,
        "current_session": current_session,
        "started_at": started_at or _now(),
        "last_heartbeat": _now(),
    }
    await redis.setex(key, _WORKER_TTL_S, json.dumps(info, ensure_ascii=False))


async def update(redis: Redis, worker_id: str, **fields) -> None:
    """Refresh worker info + reset TTL. Only updates provided fields."""
    key = _WORKER_KEY.format(worker_id)
    raw = await redis.get(key)
    info = json.loads(raw) if raw else {"worker_id": worker_id, "started_at": _now()}
    info.update(fields)
    info["last_heartbeat"] = _now()
    await redis.setex(key, _WORKER_TTL_S, json.dumps(info, ensure_ascii=False))


async def get_current_session(redis: Redis, worker_id: str) -> str:
    """Return current_session for a worker, or empty string if unknown."""
    raw = await redis.get(_WORKER_KEY.format(worker_id))
    if not raw:
        return ""
    return json.loads(raw).get("current_session", "")


async def get_all(redis: Redis) -> list[dict]:
    keys = await redis.keys("worker:*")
    if not keys:
        return []
    workers = []
    for key in keys:
        raw = await redis.get(key)
        if raw:
            workers.append(json.loads(raw))
    return workers


async def find_dead(redis: Redis) -> list[str]:
    """
    Return list of worker_ids whose Redis key has expired (TTL expired = no heartbeat).
    Since key TTL=30s and we refresh every 15s, an expired key means dead worker.
    """
    # We can't iterate expired keys directly; instead, scan for keys that still exist
    # and check last_heartbeat age.
    import time
    now_ts = datetime.now(timezone.utc)
    dead = []
    for worker in await get_all(redis):
        try:
            last_hb = datetime.fromisoformat(worker["last_heartbeat"])
            # Make timezone-aware if naive
            if last_hb.tzinfo is None:
                from datetime import timezone as _tz
                last_hb = last_hb.replace(tzinfo=_tz.utc)
            elapsed = (now_ts - last_hb).total_seconds()
            if elapsed > _DEAD_THRESHOLD_S:
                dead.append(worker["worker_id"])
        except (KeyError, ValueError):
            dead.append(worker.get("worker_id", "unknown"))
    return dead


async def remove(redis: Redis, worker_id: str) -> None:
    await redis.delete(_WORKER_KEY.format(worker_id))
