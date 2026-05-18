"""
api/routes/result.py — GET /v1/sessions/{session_id}/result

Trả về nội dung result.json sau khi session kết thúc.
Chỉ available khi session ở terminal state (done/failed/cancelled/timed_out).

K8s production: API pod KHÔNG share local FS với Worker pod → phải fetch
result.json từ CDN. Worker upload result.json + lưu `result_cdn_url` vào
Redis session hash. API ưu tiên: local file → CDN URL từ Redis → CDN URL
build theo convention (backward compat session cũ).
"""

import json
import logging
from pathlib import Path

import requests
from fastapi import APIRouter, HTTPException

from config import ARTIFACTS_ROOT
from services.session_persist import get_session_artifact_dir
from store import session_store
from store.redis_client import get_async_redis

_log = logging.getLogger(__name__)
router = APIRouter()

_TERMINAL = frozenset({"done", "failed", "cancelled", "timed_out"})

# CDN base — build URL convention nếu Redis không có result_cdn_url.
# Pattern khớp với build_artifact_remote_path trong artifact_uploader.
_CDN_BASE = "https://cdn.fstats.ai/changchatbot"


def _build_convention_cdn_url(sess: dict, session_id: str) -> str:
    """Build CDN URL theo convention path khi Redis không có result_cdn_url.

    Convention: public/tool-web/prod/sessions/{YYYY/MM/DD}/{task_id}/{session_id}/result.json
    (hoặc fallback không có task_id cho session legacy).

    Date lấy từ session.created_at (ISO format YYYY-MM-DDTHH:...) hoặc
    finished_at. Nếu cả 2 đều rỗng → trả "" (caller xử lý 404).
    """
    date_iso = sess.get("created_at") or sess.get("finished_at") or ""
    if not date_iso or len(date_iso) < 10:
        return ""
    date_str = date_iso[:10].replace("-", "/")  # YYYY-MM-DD → YYYY/MM/DD
    task_id = sess.get("task_id", "").strip()
    subpath = f"{task_id}/{session_id}" if task_id else session_id
    return f"{_CDN_BASE}/public/tool-web/prod/sessions/{date_str}/{subpath}/result.json"


def _fetch_cdn_json(url: str, session_id: str) -> dict:
    """Fetch result.json từ CDN. Raise HTTPException nếu fail."""
    try:
        # Pod K8s không có direct egress, requests.get default qua HTTP_PROXY env.
        # Reference: BUG-006 (vision_matcher fix 2026-05-08).
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        _log.warning(f"[{session_id}] Failed fetch result CDN {url}: {e}")
        raise HTTPException(
            status_code=404,
            detail=f"Result file not reachable from CDN: {url}",
        )
    except (ValueError, json.JSONDecodeError) as e:
        _log.error(f"[{session_id}] CDN result.json invalid JSON {url}: {e}")
        raise HTTPException(
            status_code=500,
            detail="Result file on CDN is not valid JSON",
        )


@router.get("/v1/sessions/{session_id}/result")
async def get_result(session_id: str):
    """
    Lấy result.json của session.

    - 404 nếu session không tồn tại
    - 404 với detail "Result not ready" nếu session chưa kết thúc
    - 200 với nội dung result.json nếu đã có

    Lookup order:
      1. Local file (sess.result_path hoặc convention dir) — work cho dev/local
      2. CDN URL từ Redis (sess.result_cdn_url) — work cho K8s production
      3. CDN URL build theo convention từ task_id + date — backward compat
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

    # ── 1. Local file ────────────────────────────────────────────────────────
    result_path = Path(sess.get("result_path", "") or "")
    if not result_path or not result_path.exists():
        result_path = get_session_artifact_dir(session_id) / "result.json"

    if result_path.exists():
        try:
            with open(result_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            _log.error(f"[{session_id}] Failed to read local result.json: {e}")
            # Đừng raise — fallback CDN bên dưới có thể work.

    # ── 2. CDN URL từ Redis (worker lưu sau khi upload) ──────────────────────
    cdn_url = sess.get("result_cdn_url", "").strip()
    if cdn_url:
        _log.info(f"[{session_id}] Fetching result from Redis-stored CDN URL")
        return _fetch_cdn_json(cdn_url, session_id)

    # ── 3. Convention CDN URL (backward compat session pre-fix) ──────────────
    convention_url = _build_convention_cdn_url(sess, session_id)
    if convention_url:
        _log.info(f"[{session_id}] Trying convention CDN URL (backward compat)")
        return _fetch_cdn_json(convention_url, session_id)

    raise HTTPException(
        status_code=404,
        detail=(
            "Result file not found. No local file, no result_cdn_url in Redis, "
            "and session missing task_id/created_at to build convention URL."
        ),
    )
