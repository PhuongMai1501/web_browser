"""
api/routes/browser.py — Browser-level reset endpoint.

POST /v1/browser/reset  →  cancel active session + close browser on worker.

Note: in Phase 1b, the browser runs in a separate worker process.
We can't directly call browser.close_browser() from the API process.
The reset cancels the current session; the worker detects cancel_requested
and handles cleanup + browser close on its side.
"""

import json

from fastapi import APIRouter

from store import job_queue, session_store, worker_registry
from store.redis_client import get_async_redis

router = APIRouter()

_TERMINAL = frozenset({"done", "failed", "cancelled", "timed_out"})


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
