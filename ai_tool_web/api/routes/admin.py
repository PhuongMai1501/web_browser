"""
api/routes/admin.py — Admin monitoring endpoints.

Endpoints (yêu cầu X-Admin-Token == env ADMIN_TOKEN):
  GET  /v1/admin/sessions        → list session (default chỉ active, ?include_finished=1 để xem all)
  GET  /v1/admin/workers         → list worker registry + last heartbeat
  POST /v1/admin/sessions/{id}/cancel → cancel session từ màn admin (alias của /v1/sessions/{id}/cancel)

Tất cả endpoint đều check `user.is_admin` qua dependency `get_current_user`
(provider MockAuthProvider so sánh header X-Admin-Token với env ADMIN_TOKEN).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import get_current_user
from auth.providers import AuthenticatedUser
from models import (
    AdminSessionItem,
    AdminSessionsResponse,
    AdminWorkerItem,
    CancelResponse,
    TaskSessionsResponse,
)
from store import job_queue, session_store, worker_registry
from store.redis_client import get_async_redis


router = APIRouter(prefix="/v1/admin", tags=["admin"])

_TERMINAL = frozenset({"done", "failed", "cancelled", "timed_out"})


def _ensure_admin(user: AuthenticatedUser) -> None:
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Endpoint admin-only. Yêu cầu X-Admin-Token header.",
        )


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def _session_to_admin_item(sess: dict) -> AdminSessionItem:
    """Convert Redis hash → AdminSessionItem. Dùng chung cho /sessions + /tasks."""
    return AdminSessionItem(
        session_id=sess.get("session_id", ""),
        status=sess.get("status", ""),
        scenario=sess.get("scenario", ""),
        name=sess.get("name") or None,
        user_id=sess.get("user_id") or None,
        task_id=sess.get("task_id") or None,
        iteration=int(sess["iteration"]) if sess.get("iteration") else None,
        current_step=int(sess.get("current_step") or 0),
        max_steps=int(sess.get("max_steps") or 0),
        assigned_worker=sess.get("assigned_worker") or None,
        created_at=sess.get("created_at", ""),
        started_at=sess.get("started_at") or None,
        finished_at=sess.get("finished_at") or None,
        error_msg=sess.get("error_msg") or None,
    )


@router.get("/sessions", response_model=AdminSessionsResponse)
async def list_sessions(
    user: AuthenticatedUser = Depends(get_current_user),
    include_finished: bool = Query(
        default=False,
        description="True → bao gồm cả session đã done/failed/cancelled/timed_out",
    ),
    task_id: str | None = Query(
        default=None,
        description="Filter theo task_id (nếu set → chỉ trả session của task này)",
    ),
    limit: int = Query(default=200, ge=1, le=1000),
):
    """List session — mặc định chỉ active (queued/assigned/running/waiting_for_user).

    Trả về kèm worker pool snapshot để UI render 1 màn duy nhất.
    Optional filter `?task_id=` để xem 1 task cụ thể.
    """
    _ensure_admin(user)
    redis = get_async_redis()

    raw_sessions = await session_store.list_async(redis)

    items: list[AdminSessionItem] = []
    for sess in raw_sessions:
        status = sess.get("status", "")
        if not include_finished and status in _TERMINAL:
            continue
        if task_id and sess.get("task_id") != task_id:
            continue
        items.append(_session_to_admin_item(sess))

    # Sort: created_at desc (mới nhất lên đầu)
    items.sort(key=lambda x: x.created_at, reverse=True)
    items = items[:limit]

    workers = await worker_registry.get_all(redis)
    workers_alive = len(workers)
    workers_busy = sum(1 for w in workers if w.get("status") == "busy")
    q_len = await job_queue.queue_length(redis)

    return AdminSessionsResponse(
        sessions=items,
        total=len(items),
        workers_alive=workers_alive,
        workers_busy=workers_busy,
        queue_length=q_len,
    )


@router.get("/workers", response_model=list[AdminWorkerItem])
async def list_workers(
    user: AuthenticatedUser = Depends(get_current_user),
):
    """List worker registry — heartbeat, status, session đang gắn."""
    _ensure_admin(user)
    redis = get_async_redis()

    workers = await worker_registry.get_all(redis)
    now = datetime.now(timezone.utc)
    out: list[AdminWorkerItem] = []
    for w in workers:
        last_hb = _parse_iso(w.get("last_heartbeat", ""))
        elapsed = (now - last_hb).total_seconds() if last_hb else None
        out.append(
            AdminWorkerItem(
                worker_id=w.get("worker_id", "unknown"),
                status=w.get("status") or "unknown",
                current_session=w.get("current_session") or None,
                started_at=w.get("started_at") or None,
                last_heartbeat=w.get("last_heartbeat") or None,
                seconds_since_heartbeat=round(elapsed, 1) if elapsed is not None else None,
            )
        )
    out.sort(key=lambda x: x.worker_id)
    return out


@router.get("/tasks/{task_id}/sessions", response_model=TaskSessionsResponse)
async def list_task_iterations(
    task_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """List tất cả iterations của 1 task — sort theo iteration tăng dần.

    Use case Sup Agent:
      - Lấy lịch sử các lần thử cho 1 yêu cầu của user
      - Diff YAML giữa các iteration để học pattern
      - Biết iteration nào đang chạy (has_running)
    """
    _ensure_admin(user)
    redis = get_async_redis()

    raw_sessions = await session_store.list_async(redis)

    matched = [s for s in raw_sessions if s.get("task_id") == task_id]
    matched.sort(key=lambda s: int(s.get("iteration") or 0))

    items = [_session_to_admin_item(s) for s in matched]
    has_running = any(s.status not in _TERMINAL for s in items)
    latest_iter = max((s.iteration or 0 for s in items), default=0)

    return TaskSessionsResponse(
        task_id=task_id,
        iterations=items,
        total=len(items),
        has_running=has_running,
        latest_iteration=latest_iter,
    )


@router.post("/sessions/{session_id}/cancel", response_model=CancelResponse)
async def cancel_from_admin(
    session_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Cancel 1 session từ màn admin. Pattern giống /v1/sessions/{id}/cancel
    nhưng require admin (X-Admin-Token) thay vì public.
    """
    _ensure_admin(user)
    redis = get_async_redis()

    sess = await session_store.get_async(redis, session_id)
    if not sess:
        raise HTTPException(404, detail="Session not found")
    if sess["status"] in _TERMINAL:
        raise HTTPException(409, detail="SESSION_FINISHED")

    await session_store.update_async(redis, session_id, cancel_requested="1")

    if sess["status"] == "waiting_for_user":
        msg = json.dumps({"type": "cancel"}, ensure_ascii=False)
        await redis.rpush(f"resume:{session_id}", msg)

    return CancelResponse(
        status="cancelled",
        steps_completed=int(sess.get("current_step", 0)),
    )
