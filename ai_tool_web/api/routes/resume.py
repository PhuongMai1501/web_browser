import json

from fastapi import APIRouter, HTTPException

from models import ResumeRequest, ResumeResponse
from store import session_store
from store.redis_client import get_async_redis

router = APIRouter()


@router.post("/v1/sessions/{session_id}/resume", response_model=ResumeResponse)
async def resume_session(session_id: str, req: ResumeRequest):
    """Send answer to a waiting agent. Worker unblocks via BLPOP."""
    redis = get_async_redis()
    sess = await session_store.get_async(redis, session_id)
    if not sess:
        raise HTTPException(404, detail="Session not found")
    if sess["status"] in ("done", "failed", "cancelled", "timed_out"):
        raise HTTPException(409, detail="SESSION_FINISHED")
    if sess["status"] != "waiting_for_user":
        raise HTTPException(409, detail="SESSION_NOT_WAITING")

    msg = json.dumps({"type": "answer", "answer": req.answer}, ensure_ascii=False)
    await redis.rpush(f"resume:{session_id}", msg)

    return ResumeResponse(status="resumed", session_id=session_id)
