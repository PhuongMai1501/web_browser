from fastapi import APIRouter, Query

from api.artifact_service import serve_screenshot
from store.redis_client import get_async_redis

router = APIRouter()


@router.get("/v1/sessions/{session_id}/steps/{step_number}/screenshot")
async def get_screenshot(
    session_id: str,
    step_number: int,
    annotated: bool = Query(default=False),
):
    """Serve screenshot PNG for a step. Lazy-loaded by the React UI."""
    redis = get_async_redis()
    return await serve_screenshot(redis, session_id, step_number, annotated)
