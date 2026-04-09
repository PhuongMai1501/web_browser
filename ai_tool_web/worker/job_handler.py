"""
worker/job_handler.py — Agent job execution (Phase 1b: Redis-backed).

run_job_sync() runs in a thread via asyncio.to_thread().
Uses sync Redis for all I/O (state updates, event push, BLPOP resume).
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import redis as _sync_redis
from openai import RateLimitError

from config import ASK_TIMEOUT_S, SESSION_HARD_CAP_S
from models import (
    record_to_ask_event,
    record_to_done_event,
    record_to_step_event,
)
from store.event_store import push_event_sync
from store.session_store import set_screenshot_sync, update_sync

_log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def friendly_error(e: Exception) -> tuple[str, str]:
    if isinstance(e, RateLimitError):
        return "RATE_LIMIT", "OpenAI API rate limit. Vui lòng thử lại sau vài phút."
    if isinstance(e, TimeoutError):
        return "BROWSER_TIMEOUT", "Browser không phản hồi (timeout)."
    if isinstance(e, json.JSONDecodeError):
        return "LLM_INVALID_RESPONSE", "LLM trả về response không hợp lệ."
    if isinstance(e, ConnectionError):
        return "CONNECTION_ERROR", "Mất kết nối mạng."
    if isinstance(e, ValueError) and "Domain" in str(e):
        return "DOMAIN_BLOCKED", f"URL bị chặn: {e}"
    return "INTERNAL_ERROR", f"Lỗi: {e}"


def _is_cancelled(sync_r: _sync_redis.Redis, session_id: str) -> bool:
    return sync_r.hget(f"session:{session_id}", "cancel_requested") == "1"


def run_job_sync(
    session_id: str,
    worker_id: str,
    api_key: str,
    sync_r: _sync_redis.Redis,
) -> None:
    """
    Execute one agent session. Runs in a thread pool.
    All Redis operations use sync_r (blocking).
    """
    # LLM_base imports — path already set by browser_worker.py before this call
    import browser_adapter as browser
    from runner import run_agent_autonomous
    from scenarios.chang_login import CHANG_URL, run_chang_login_autonomous

    sess_data = sync_r.hgetall(f"session:{session_id}")
    if not sess_data:
        _log.error(f"Session {session_id} not found in Redis")
        return

    scenario = sess_data.get("scenario", "chang_login")
    max_steps = int(sess_data.get("max_steps", 20))
    scenario_config = json.loads(sess_data.get("scenario_config", "{}"))
    context = scenario_config.get("context")

    def push(event_type: str, payload: dict) -> None:
        push_event_sync(sync_r, session_id, event_type, payload)

    update_sync(sync_r, session_id, status="running", started_at=_now())

    session_start = time.time()

    try:
        if scenario == "chang_login":
            gen = run_chang_login_autonomous(
                api_key=api_key,
                context=context,
                max_steps=max_steps,
                session_id=session_id,
            )
        else:
            target_url = scenario_config.get("url") or CHANG_URL
            browser.open_url(target_url)
            browser.wait_ms(2000)
            goal = scenario_config.get("goal") or f"Thực hiện tác vụ trên {target_url}"
            gen = run_agent_autonomous(
                goal=goal,
                api_key=api_key,
                context=context,
                max_steps=max_steps,
                session_id=session_id,
            )

        answer = None

        while True:
            # Check cancel before each step
            if _is_cancelled(sync_r, session_id):
                push("cancelled", {"reason": "Cancelled by user"})
                update_sync(sync_r, session_id, status="cancelled", finished_at=_now())
                break

            # Hard cap
            if time.time() - session_start > SESSION_HARD_CAP_S:
                push("failed", {
                    "code": "SESSION_TIMEOUT",
                    "message": "Session vượt quá 10 phút. Tự động huỷ.",
                })
                update_sync(sync_r, session_id, status="failed",
                            error_msg="Session timeout", finished_at=_now())
                break

            try:
                record = gen.send(answer)
                answer = None
                update_sync(sync_r, session_id, current_step=str(record.step))

                # Store screenshot paths
                if record.screenshot_path:
                    set_screenshot_sync(sync_r, session_id, record.step,
                                        record.screenshot_path, annotated=False)
                annotated = (
                    record.screenshot_path.replace(".png", "_annotated.png")
                    if record.screenshot_path else ""
                )
                if annotated and Path(annotated).exists():
                    set_screenshot_sync(sync_r, session_id, record.step,
                                        annotated, annotated=True)

                if record.is_blocked:
                    ask_ev = record_to_ask_event(record, session_id)
                    push("ask", ask_ev.model_dump())

                    deadline = datetime.now(timezone.utc).timestamp() + ASK_TIMEOUT_S
                    update_sync(sync_r, session_id,
                                status="waiting_for_user",
                                ask_deadline_at=datetime.fromtimestamp(deadline, timezone.utc).isoformat())

                    result = sync_r.blpop(f"resume:{session_id}", timeout=ASK_TIMEOUT_S + 10)

                    if result is None:
                        push("timed_out", {
                            "elapsed_seconds": ASK_TIMEOUT_S,
                            "message": f"Không nhận được câu trả lời sau {ASK_TIMEOUT_S}s.",
                        })
                        update_sync(sync_r, session_id, status="timed_out", finished_at=_now())
                        break

                    msg = json.loads(result[1])
                    if msg.get("type") == "cancel":
                        push("cancelled", {"reason": "Cancelled while waiting for user"})
                        update_sync(sync_r, session_id, status="cancelled", finished_at=_now())
                        break

                    answer = msg.get("answer", "")
                    update_sync(sync_r, session_id, status="running", ask_deadline_at="")
                    continue

                if record.is_done:
                    duration = time.time() - session_start
                    done_ev = record_to_done_event(record, session_id, record.step, duration)
                    push("done", done_ev.model_dump())
                    update_sync(sync_r, session_id, status="done", finished_at=_now())
                    break

                step_ev = record_to_step_event(record, session_id)
                push("step", step_ev.model_dump())

            except StopIteration:
                update_sync(sync_r, session_id, status="done", finished_at=_now())
                break

    except Exception as exc:
        code, msg = friendly_error(exc)
        _log.error(f"Session {session_id} error: {exc}", exc_info=True)
        push("failed", {"code": code, "message": msg})
        update_sync(sync_r, session_id, status="failed", error_msg=msg, finished_at=_now())
    finally:
        # Clean up resume queue
        sync_r.delete(f"resume:{session_id}")
        _log.info(f"[{worker_id}] Session {session_id} finished")
