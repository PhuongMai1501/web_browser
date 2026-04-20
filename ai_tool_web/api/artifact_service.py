"""
api/artifact_service.py — Serve screenshot files from shared volume or CDN.

Reads screenshot value from Redis Hashes:
  session:{id}:screenshots  → step_num: value
  session:{id}:annotated    → step_num: value

value có thể là:
  - CDN URL (https://...)   → redirect 302 về CDN
  - Local path (/app/...)   → serve FileResponse từ shared volume (fallback)
"""

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from redis.asyncio import Redis

from store.session_store import get_screenshot_async


async def serve_screenshot(
    redis: Redis,
    session_id: str,
    step_number: int,
    annotated: bool = False,
):
    value = await get_screenshot_async(redis, session_id, step_number, annotated)

    if not value:
        label = "annotated screenshot" if annotated else "screenshot"
        raise HTTPException(404, detail=f"{label.capitalize()} for step {step_number} not found")

    # CDN URL → redirect client trực tiếp
    if value.startswith("https://") or value.startswith("http://"):
        return RedirectResponse(url=value, status_code=302)

    # Local path → serve từ shared volume (fallback khi upload chưa bật hoặc fail)
    path = Path(value)
    if not path.exists():
        raise HTTPException(404, detail=f"Screenshot file missing: {value}")

    return FileResponse(
        str(path),
        media_type="image/png",
        headers={"Cache-Control": "immutable, max-age=31536000"},
    )
