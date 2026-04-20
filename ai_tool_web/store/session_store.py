"""
state/session_store.py — Session state CRUD in Redis.

Schema: Redis Hash  key=session:{session_id}
  session_id, status, scenario, current_step, max_steps,
  created_at, updated_at, started_at, finished_at,
  assigned_worker, last_event_id, ask_deadline_at,
  cancel_requested, artifact_root, scenario_config,
  error_msg, client_id

Async functions: for API routes (asyncio)
Sync  functions: for worker threads (blocking)
"""

import json
from datetime import datetime, timezone

import redis as _sync_redis
from redis.asyncio import Redis

from config import SESSION_TTL_S

_SESSION_KEY = "session:{}"
_SCREENSHOTS_KEY = "session:{}:screenshots"
_ANNOTATED_KEY = "session:{}:annotated"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Async (API) ────────────────────────────────────────────────────────────────

async def create_async(
    redis: Redis,
    session_id: str,
    scenario: str,
    max_steps: int,
    scenario_config: dict,
    client_id: str = "",
) -> None:
    key = _SESSION_KEY.format(session_id)
    await redis.hset(key, mapping={
        "session_id": session_id,
        "status": "queued",
        "scenario": scenario,
        "current_step": "0",
        "max_steps": str(max_steps),
        "created_at": _now(),
        "updated_at": _now(),
        "started_at": "",
        "finished_at": "",
        "assigned_worker": "",
        "last_event_id": "0",
        "ask_deadline_at": "",
        "cancel_requested": "0",
        "artifact_root": "",
        "result_path": "",
        "scenario_config": json.dumps(scenario_config, ensure_ascii=False),
        "error_msg": "",
        "client_id": client_id,
    })
    await redis.expire(key, SESSION_TTL_S)


async def get_async(redis: Redis, session_id: str) -> dict | None:
    data = await redis.hgetall(_SESSION_KEY.format(session_id))
    return data if data else None


async def update_async(redis: Redis, session_id: str, **fields) -> None:
    key = _SESSION_KEY.format(session_id)
    fields["updated_at"] = _now()
    await redis.hset(key, mapping={k: str(v) for k, v in fields.items()})
    await redis.expire(key, SESSION_TTL_S)


async def get_screenshot_async(redis: Redis, session_id: str, step: int, annotated: bool = False) -> str | None:
    hkey = _ANNOTATED_KEY.format(session_id) if annotated else _SCREENSHOTS_KEY.format(session_id)
    return await redis.hget(hkey, str(step))


# ── Sync (worker thread) ───────────────────────────────────────────────────────

def get_sync(sync_r: _sync_redis.Redis, session_id: str) -> dict | None:
    data = sync_r.hgetall(_SESSION_KEY.format(session_id))
    return data if data else None


def update_sync(sync_r: _sync_redis.Redis, session_id: str, **fields) -> None:
    key = _SESSION_KEY.format(session_id)
    fields["updated_at"] = _now()
    sync_r.hset(key, mapping={k: str(v) for k, v in fields.items()})
    sync_r.expire(key, SESSION_TTL_S)


def set_screenshot_sync(sync_r: _sync_redis.Redis, session_id: str, step: int, path: str, annotated: bool = False) -> None:
    hkey = _ANNOTATED_KEY.format(session_id) if annotated else _SCREENSHOTS_KEY.format(session_id)
    sync_r.hset(hkey, str(step), path)
    sync_r.expire(hkey, SESSION_TTL_S)
