"""
api/routes/result.py — GET /v1/sessions/{session_id}/result

Trả về nội dung result.json sau khi session kết thúc.
Chỉ available khi session ở terminal state (done/failed/cancelled/timed_out).
"""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from config import ARTIFACTS_ROOT
from services.session_persist import get_session_artifact_dir
from store import session_store
from store.redis_client import get_async_redis

_log = logging.getLogger(__name__)
router = APIRouter()

_TERMINAL = frozenset({"done", "failed", "cancelled", "timed_out"})


@router.get("/v1/sessions/{session_id}/result")
async def get_result(session_id: str):
    """
    Lấy result.json của session.

    - 404 nếu session không tồn tại
    - 404 với detail "Result not ready" nếu session chưa kết thúc
    - 200 với nội dung result.json nếu đã có
    """
    redis = get_async_redis()
    sess = await session_store.get_async(redis, session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")

    status = sess.get("status", "")
    if status not in _TERMINAL:
        raise HTTPException(
            status_code=404,
            detail=f"Result not ready. Session status: {status}",
        )

    # Ưu tiên path được lưu trong session hash
    result_path = Path(sess.get("result_path", "") or "")

    # Fallback: tìm theo convention path
    if not result_path or not result_path.exists():
        result_path = get_session_artifact_dir(session_id) / "result.json"

    if not result_path.exists():
        _log.warning(f"result.json not found for session {session_id}")
        raise HTTPException(
            status_code=404,
            detail="Result file not found. Session may have ended before result was written.",
        )

    try:
        with open(result_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _log.error(f"Failed to read result.json for {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to read result file")
