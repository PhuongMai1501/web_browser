"""
state/event_store.py — Event push / subscribe / buffer via Redis.

Event envelope:
  {
    "event_id": <int, monotonic per session>,
    "session_id": "...",
    "type": "step | ask | done | failed | cancelled | ...",
    "ts": "<ISO>",
    "payload": {}
  }

event_id is assigned via INCR session:{id}:event_seq.
Non-heartbeat events are buffered in session:{id}:buffer (last 50).
"""

import json
from datetime import datetime, timezone
from typing import AsyncIterator

import redis as _sync_redis
from redis.asyncio import Redis

from config import SESSION_TTL_S

_CHANNEL = "session:{}:events"
_BUFFER_KEY = "session:{}:buffer"
_SEQ_KEY = "session:{}:event_seq"
_BUFFER_MAX = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_event(event_id: int, session_id: str, event_type: str, payload: dict) -> dict:
    return {
        "event_id": event_id,
        "session_id": session_id,
        "type": event_type,
        "ts": _now(),
        "payload": payload,
    }


# ── Sync (worker thread) ───────────────────────────────────────────────────────

def push_event_sync(
    sync_r: _sync_redis.Redis,
    session_id: str,
    event_type: str,
    payload: dict,
) -> int:
    """Push event from worker thread. Returns event_id."""
    event_id = sync_r.incr(_SEQ_KEY.format(session_id))
    event = _build_event(event_id, session_id, event_type, payload)
    event_json = json.dumps(event, ensure_ascii=False)

    sync_r.publish(_CHANNEL.format(session_id), event_json)

    if event_type != "heartbeat":
        buf_key = _BUFFER_KEY.format(session_id)
        sync_r.rpush(buf_key, event_json)
        sync_r.ltrim(buf_key, -_BUFFER_MAX, -1)
        sync_r.expire(buf_key, SESSION_TTL_S)

    sync_r.hset(f"session:{session_id}", "last_event_id", str(event_id))
    return event_id


# ── Async (API / recovery) ────────────────────────────────────────────────────

async def push_event_async(
    redis: Redis,
    session_id: str,
    event_type: str,
    payload: dict,
) -> int:
    """Push event from async context (API, recovery loop). Returns event_id."""
    event_id = await redis.incr(_SEQ_KEY.format(session_id))
    event = _build_event(event_id, session_id, event_type, payload)
    event_json = json.dumps(event, ensure_ascii=False)

    await redis.publish(_CHANNEL.format(session_id), event_json)

    if event_type != "heartbeat":
        buf_key = _BUFFER_KEY.format(session_id)
        await redis.rpush(buf_key, event_json)
        await redis.ltrim(buf_key, -_BUFFER_MAX, -1)
        await redis.expire(buf_key, SESSION_TTL_S)

    await redis.hset(f"session:{session_id}", "last_event_id", str(event_id))
    return event_id




async def get_buffer_async(redis: Redis, session_id: str) -> list[dict]:
    """Return all buffered events for reconnect replay."""
    raw_list = await redis.lrange(_BUFFER_KEY.format(session_id), 0, -1)
    return [json.loads(r) for r in raw_list]


async def subscribe_async(
    redis: Redis,
    session_id: str,
    last_event_id: int,
) -> AsyncIterator[dict]:
    """
    Async generator yielding events for SSE stream.
    1. Subscribe first (avoids race condition)
    2. Replay buffer (events already stored)
    3. Stream new live events
    Deduplicates by event_id.
    """
    channel = _CHANNEL.format(session_id)
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)

    last_seen = last_event_id

    try:
        # Replay buffer — safe because subscribe is already active
        buffered = await redis.lrange(_BUFFER_KEY.format(session_id), 0, -1)
        for raw in buffered:
            event = json.loads(raw)
            if event["event_id"] > last_seen:
                last_seen = event["event_id"]
                yield event

        # Live stream
        while True:
            # get_message with timeout acts as a blocking read up to N seconds
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=15.0,
            )
            if message is None:
                # No message within 15s → yield heartbeat signal (not a real event)
                yield {"event_id": -1, "type": "heartbeat", "payload": {}}
                continue
            if message["type"] != "message":
                continue
            event = json.loads(message["data"])
            if event["event_id"] > last_seen:
                last_seen = event["event_id"]
                yield event

    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
