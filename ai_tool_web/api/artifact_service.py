"""
api/artifact_service.py — Serve screenshot files from shared volume.

Reads screenshot paths (absolute) from Redis Hashes:
  session:{id}:screenshots  → step_num: path
  session:{id}:annotated    → step_num: path
"""

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from redis.asyncio import Redis

from store.session_store import get_screenshot_async


async def serve_screenshot(
    redis: Redis,
    session_id: str,
    step_number: int,
    annotated: bool = False,
) -> FileResponse:
    path_str = await get_screenshot_async(redis, session_id, step_number, annotated)

    if not path_str:
        label = "annotated screenshot" if annotated else "screenshot"
        raise HTTPException(404, detail=f"{label.capitalize()} for step {step_number} not found")

    path = Path(path_str)
    if not path.exists():
        raise HTTPException(404, detail=f"Screenshot file missing: {path_str}")

    return FileResponse(
        str(path),
        media_type="image/png",
        headers={"Cache-Control": "immutable, max-age=31536000"},
    )
