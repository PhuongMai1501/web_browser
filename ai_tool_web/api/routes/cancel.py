import json

from fastapi import APIRouter, HTTPException

from models import CancelResponse
from store import session_store
from store.redis_client import get_async_redis

router = APIRouter()

_TERMINAL = frozenset({"done", "failed", "cancelled", "timed_out"})


@router.post("/v1/sessions/{session_id}/cancel", response_model=CancelResponse)
async def cancel_session(session_id: str):
    redis = get_async_redis()
    sess = await session_store.get_async(redis, session_id)
    if not sess:
        raise HTTPException(404, detail="Session not found")
    if sess["status"] in _TERMINAL:
        raise HTTPException(409, detail="SESSION_FINISHED")

    await session_store.update_async(redis, session_id, cancel_requested="1")

    # If worker is blocked waiting for resume, unblock it with cancel signal
    if sess["status"] == "waiting_for_user":
        msg = json.dumps({"type": "cancel"}, ensure_ascii=False)
        await redis.rpush(f"resume:{session_id}", msg)

    return CancelResponse(
        status="cancelled",
        steps_completed=int(sess.get("current_step", 0)),
    )
