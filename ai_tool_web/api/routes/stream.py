from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from api.sse_stream import sse_generator
from store import session_store
from store.redis_client import get_async_redis

router = APIRouter()


@router.get("/v1/sessions/{session_id}/stream")
async def stream_session(
    session_id: str,
    lastEventId: Optional[int] = Query(default=None),
):
    """
    SSE stream. Stays open across ask/resume.
    Supports reconnect via Last-Event-ID query param.
    """
    redis = get_async_redis()
    sess = await session_store.get_async(redis, session_id)
    if not sess:
        raise HTTPException(404, detail="Session not found")

    last_event_id = lastEventId if lastEventId is not None else 0

    return StreamingResponse(
        sse_generator(redis, session_id, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
