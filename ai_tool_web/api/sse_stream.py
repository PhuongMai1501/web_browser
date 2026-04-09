"""
api/sse_stream.py — SSE generator reading from Redis Pub/Sub.

Flow:
  1. Subscribe to session channel (before buffer replay — avoids race)
  2. Replay buffered events (event_id > last_event_id)
  3. Stream live events; yield heartbeat when no message for 15s
  4. Close when terminal event received (done/failed/cancelled/timed_out)
"""

import json
import logging

from redis.asyncio import Redis

from store.event_store import subscribe_async

_log = logging.getLogger(__name__)

_TERMINAL_TYPES = frozenset({"done", "failed", "cancelled", "timed_out"})


async def sse_generator(redis: Redis, session_id: str, last_event_id: int):
    """
    Async generator that yields SSE-formatted strings.
    last_event_id=0 means fresh connection (replay all buffer).
    """
    try:
        async for event in subscribe_async(redis, session_id, last_event_id):
            event_type = event["type"]

            if event_type == "heartbeat":
                yield "event: heartbeat\ndata: {}\n\n"
                continue

            event_id = event["event_id"]
            payload = event.get("payload", {})

            yield (
                f"id: {event_id}\n"
                f"event: {event_type}\n"
                f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            )

            if event_type in _TERMINAL_TYPES:
                break

    except Exception as exc:
        _log.error(f"SSE generator error for session {session_id}: {exc}", exc_info=True)
        yield f"event: error\ndata: {json.dumps({'code': 'STREAM_ERROR', 'message': str(exc)})}\n\n"
