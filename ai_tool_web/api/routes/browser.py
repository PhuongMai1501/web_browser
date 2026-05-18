"""
api/routes/browser.py — Browser-level reset endpoints.

POST /v1/browser/reset       →  cancel active session on current worker.
POST /v1/browser/kill-all    →  ADMIN — cancel ALL sessions + clear queue
                                  + force restart worker pod (escape hatch).

Note: in Phase 1b, the browser runs in a separate worker process.
We can't directly call browser.close_browser() from the API process.
The reset cancels the current session; the worker detects cancel_requested
and handles cleanup + browser close on its side.

Kill-all: ngoài clear Redis state, còn SET key `control:worker_emergency_exit=1`
(TTL 60s). Worker background task `_emergency_exit_watcher` poll key này
mỗi 5s, khi thấy = "1" sẽ os._exit(99) → K8s Deployment ReplicaSet auto
respawn pod mới. Đây là cách force kill worker process đang stuck trong
blocking I/O (LLM call timeout, subprocess deadlock) mà cancel_requested
không xử lý được vì không có check mid-blocking-call.
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import get_current_user
from auth.providers import AuthenticatedUser
from store import job_queue, session_store, worker_registry
from store.redis_client import get_async_redis

router = APIRouter()

_TERMINAL = frozenset({"done", "failed", "cancelled", "timed_out"})
_DEAD_WORKER_AGE_S = 60
# Tránh prefix "worker:" vì xung đột với worker_registry pattern (sẽ crash
# health endpoint khi get_all parse "1" thành JSON worker).
_EMERGENCY_EXIT_KEY = "control:worker_emergency_exit"
_EMERGENCY_EXIT_TTL_S = 60


@router.post("/v1/browser/reset")
async def reset_browser():
    """
    Cancel any running session and signal the worker to close browser.
    Safe to call even if no session is active.
    """
    redis = get_async_redis()
    cancelled_id = None

    workers = await worker_registry.get_all(redis)
    for worker in workers:
        session_id = worker.get("current_session")
        if not session_id:
            continue

        sess = await session_store.get_async(redis, session_id)
        if not sess or sess["status"] in _TERMINAL:
            continue

        await session_store.update_async(redis, session_id, cancel_requested="1")

        if sess["status"] == "waiting_for_user":
            msg = json.dumps({"type": "cancel"}, ensure_ascii=False)
            await redis.rpush(f"resume:{session_id}", msg)

        cancelled_id = session_id

    return {"status": "reset", "session_cancelled": cancelled_id}


@router.post("/v1/browser/kill-all")
async def kill_all_sessions(
    user: AuthenticatedUser = Depends(get_current_user),
):
    """ADMIN — Force kill toàn bộ: cancel sessions + clear queue
    + signal worker self-exit để K8s respawn pod.

    Dùng khi worker stuck với session cũ (LLM call timeout dài, browser
    treo, etc) → reset_browser không kịp tác dụng vì worker không check
    cancel_requested trong vòng lặp blocking. Endpoint này:

    1. Mark mọi session non-terminal → status=cancelled, cancel_requested=1
    2. Push 'cancel' message lên resume:* cho session waiting_for_user
    3. Xóa Redis key `pending_jobs` (clear queue đợi)
    4. Xóa worker:* keys nếu heartbeat > 60s (dead worker registry)
    5. SET `worker:emergency_exit=1` (TTL 60s) — worker background watcher
       poll key này mỗi 5s và force `os._exit(99)` khi thấy → K8s respawn

    Worker mới sẽ ready trong ~20-30s. Client nên poll `/v1/health` để
    biết khi nào `workers_busy=0` và worker_id thay đổi.

    Yêu cầu: user.is_admin = True (header X-Admin-Token == ADMIN_TOKEN env).
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Endpoint admin-only. Yêu cầu X-Admin-Token header.",
        )

    redis = get_async_redis()
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1) Cancel all non-terminal sessions
    cancelled_ids: list[str] = []
    waiting_count = 0
    session_keys = await redis.keys("session:*")
    for key in session_keys:
        # Bỏ qua nested keys (session:{id}:screenshots, :annotated)
        if isinstance(key, bytes):
            key_str = key.decode()
        else:
            key_str = key
        if key_str.count(":") > 1:
            continue
        session_id = key_str.split(":", 1)[1]

        sess = await session_store.get_async(redis, session_id)
        if not sess:
            continue
        status = sess.get("status", "")
        if status in _TERMINAL:
            continue

        # Mark cancelled
        await session_store.update_async(
            redis, session_id,
            cancel_requested="1",
            status="cancelled",
            error_msg="Killed by admin /v1/browser/kill-all",
            finished_at=now_iso,
        )

        # Resume signal nếu đang waiting_for_user — unblock generator
        if status == "waiting_for_user":
            msg = json.dumps({"type": "cancel"}, ensure_ascii=False)
            await redis.rpush(f"resume:{session_id}", msg)
            waiting_count += 1

        cancelled_ids.append(session_id)

    # 2) Clear pending queue
    queue_len_before = await job_queue.queue_length(redis)
    if queue_len_before > 0:
        await redis.delete("pending_jobs")

    # 3) Remove dead worker registry entries (heartbeat > 60s)
    workers_removed = 0
    for worker in await worker_registry.get_all(redis):
        try:
            last_hb_str = worker.get("last_heartbeat", "")
            if not last_hb_str:
                continue
            last_hb = datetime.fromisoformat(last_hb_str)
            if last_hb.tzinfo is None:
                last_hb = last_hb.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - last_hb).total_seconds()
            if elapsed > _DEAD_WORKER_AGE_S:
                await worker_registry.remove(redis, worker["worker_id"])
                workers_removed += 1
        except (KeyError, ValueError):
            # Malformed worker entry — remove it
            wid = worker.get("worker_id")
            if wid:
                await worker_registry.remove(redis, wid)
                workers_removed += 1

    # 4) Signal worker emergency exit — force K8s pod restart
    # Worker background watcher poll key này, khi thấy = "1" sẽ os._exit(99)
    # K8s Deployment ReplicaSet tự respawn pod mới (~20-30s).
    await redis.set(_EMERGENCY_EXIT_KEY, "1", ex=_EMERGENCY_EXIT_TTL_S)

    return {
        "status": "killed",
        "cancelled_sessions": cancelled_ids,
        "cancelled_count": len(cancelled_ids),
        "waiting_unblocked": waiting_count,
        "queue_cleared_count": queue_len_before,
        "dead_workers_removed": workers_removed,
        "worker_restart_signaled": True,
        "note": (
            "Worker đã được signal self-exit. Pod sẽ restart trong ~20-30s. "
            "Poll /v1/health đến khi workers_busy=0 trước khi run scenario mới."
        ),
    }
