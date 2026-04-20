"""
worker/job_handler.py — Agent job execution (Phase 1b: Redis-backed).

run_job_sync() runs in a thread via asyncio.to_thread().
Uses sync Redis for all I/O (state updates, event push, BLPOP resume).

Supports 2 modes:
  - SSE mode (default): events pushed via Redis Pub/Sub, client listens via SSE
  - Callback mode: events POSTed to callback_url, no SSE needed
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import redis as _sync_redis
from openai import RateLimitError

from config import ASK_TIMEOUT_S, SESSION_HARD_CAP_S
from models import (
    record_to_ask_event,
    record_to_done_event,
    record_to_step_event,
)
from services.artifact_uploader import get_uploader
from services.callback_service import CallbackService
from services.log_service import get_log_service
from services.session_persist import (
    get_session_artifact_dir,
    write_result_json,
    write_session_jsonl,
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
    import scenarios.hooks  # noqa: F401  (trigger hook registration)
    from scenarios.generic_runner import run_scenario
    from scenarios.spec import ScenarioSpec
    from services.scenario_service import get_sync as get_scenario_sync

    sess_data = sync_r.hgetall(f"session:{session_id}")
    if not sess_data:
        _log.error(f"Session {session_id} not found in Redis")
        return

    scenario = sess_data.get("scenario", "chang_login")
    max_steps = int(sess_data.get("max_steps", 20))
    scenario_config = json.loads(sess_data.get("scenario_config", "{}"))
    context = scenario_config.get("context")

    # ── Callback mode setup ──────────────────────────────────────────────
    callback_url = scenario_config.get("callback_url")
    callback_secret = scenario_config.get("callback_secret", "")
    callback_svc: Optional[CallbackService] = None
    is_callback_mode = bool(callback_url)

    if is_callback_mode:
        callback_svc = CallbackService(callback_url, callback_secret)
        _log.info(f"[{session_id}] Callback mode enabled → {callback_url}")

    # Collect events locally để ghi session.jsonl khi kết thúc
    _collected_events: list[dict] = []

    def push(event_type: str, payload: dict) -> None:
        # Always push to Redis (SSE clients can still listen)
        push_event_sync(sync_r, session_id, event_type, payload)
        _collected_events.append({
            "type": event_type,
            "ts": _now(),
            "session_id": session_id,
            "payload": payload,
        })
        # Callback mode: POST to supervisor
        if callback_svc:
            callback_svc.send(session_id, event_type, payload)

    def _persist_artifacts(status: str, summary: str, url_after: str,
                           total_steps: int, error_msg: str = "") -> None:
        """Ghi result.json + session.jsonl vào artifact dir, rồi upload lên CDN."""
        try:
            artifact_dir = get_session_artifact_dir(session_id)
            write_session_jsonl(session_id, _collected_events, artifact_dir)
            result_path = write_result_json(
                session_id=session_id,
                status=status,
                scenario=scenario,
                summary=summary,
                url_after=url_after,
                total_steps=total_steps,
                duration_seconds=time.time() - session_start,
                finished_at=_now(),
                artifact_dir=artifact_dir,
                error_msg=error_msg,
                uploader=uploader,
            )
            update_sync(sync_r, session_id, result_path=str(result_path))
        except Exception as e:
            _log.error(f"[{session_id}] Failed to persist artifacts: {e}")

    update_sync(sync_r, session_id, status="running", started_at=_now())

    session_start = time.time()
    uploader = get_uploader()  # None nếu UPLOAD_ENABLED=False
    log_svc = get_log_service()

    log_svc.log_session(session_id, "session_start", scenario=scenario,
                        max_steps=max_steps, mode="callback" if is_callback_mode else "sse")

    try:
        # Load spec: ưu tiên snapshot tại enqueue (Step 4 sẽ ghi field này),
        # fallback đọc live từ Redis để tương thích trong lúc rollout.
        spec_snapshot = scenario_config.get("spec_snapshot")
        if spec_snapshot:
            spec = ScenarioSpec.model_validate(spec_snapshot)
        else:
            spec = get_scenario_sync(sync_r, scenario)
            if spec is None:
                raise ValueError(
                    f"Scenario '{scenario}' không tồn tại trong registry. "
                    f"Seed builtin hoặc tạo qua POST /v1/scenarios trước."
                )

        gen = run_scenario(
            spec=spec,
            api_key=api_key,
            context=context,
            max_steps=max_steps,
            session_id=session_id,
            goal_override=scenario_config.get("goal") or None,
            url_override=scenario_config.get("url") or None,
        )

        answer = None

        while True:
            # Check cancel before each step
            if _is_cancelled(sync_r, session_id):
                push("cancelled", {"reason": "Cancelled by user"})
                update_sync(sync_r, session_id, status="cancelled", finished_at=_now())
                _persist_artifacts("cancelled", "Session bị huỷ bởi user.", "", 0)
                break

            # Hard cap
            if time.time() - session_start > SESSION_HARD_CAP_S:
                push("failed", {
                    "code": "SESSION_TIMEOUT",
                    "message": "Session vượt quá 10 phút. Tự động huỷ.",
                })
                update_sync(sync_r, session_id, status="failed",
                            error_msg="Session timeout", finished_at=_now())
                _persist_artifacts("failed", "Session timeout.", "", 0, "Session timeout")
                break

            try:
                record = gen.send(answer)
                answer = None
                update_sync(sync_r, session_id, current_step=str(record.step))

                # ── Upload screenshots (nếu UPLOAD_ENABLED và policy cho phép) ──
                screenshot_cdn: str = ""
                annotated_cdn: str = ""
                annotated_path = (
                    record.screenshot_path.replace(".png", "_annotated.png")
                    if record.screenshot_path else ""
                )

                if uploader and uploader.should_upload(record):
                    if record.screenshot_path:
                        screenshot_cdn = uploader.upload_screenshot(
                            record.screenshot_path, session_id, record.step
                        ) or ""
                    if annotated_path and Path(annotated_path).exists():
                        annotated_cdn = uploader.upload_screenshot(
                            annotated_path, session_id, record.step, suffix="-annotated"
                        ) or ""

                # ── Lưu vào Redis: CDN URL nếu có, fallback local path ──
                screenshot_redis = screenshot_cdn or record.screenshot_path or ""
                annotated_redis = annotated_cdn or (annotated_path if Path(annotated_path).exists() else "") if annotated_path else ""

                if screenshot_redis:
                    set_screenshot_sync(sync_r, session_id, record.step,
                                        screenshot_redis, annotated=False)
                if annotated_redis:
                    set_screenshot_sync(sync_r, session_id, record.step,
                                        annotated_redis, annotated=True)

                if record.is_blocked:
                    ask_ev = record_to_ask_event(record, session_id,
                                                 screenshot_url_override=screenshot_cdn)
                    push("ask", ask_ev.model_dump())
                    log_svc.log_session(session_id, "ask", step=record.step,
                                        message=record.action.get("message", ""))

                    # Callback mode: chờ vô hạn (timeout=0)
                    # SSE mode: chờ ASK_TIMEOUT_S
                    if is_callback_mode:
                        blpop_timeout = 0  # block indefinitely
                        update_sync(sync_r, session_id, status="waiting_for_user")
                    else:
                        deadline = datetime.now(timezone.utc).timestamp() + ASK_TIMEOUT_S
                        update_sync(sync_r, session_id,
                                    status="waiting_for_user",
                                    ask_deadline_at=datetime.fromtimestamp(deadline, timezone.utc).isoformat())
                        blpop_timeout = ASK_TIMEOUT_S + 10

                    result = sync_r.blpop(f"resume:{session_id}", timeout=blpop_timeout)

                    if result is None:
                        # Only SSE mode can timeout (callback mode blocks forever)
                        push("timed_out", {
                            "elapsed_seconds": ASK_TIMEOUT_S,
                            "message": f"Không nhận được câu trả lời sau {ASK_TIMEOUT_S}s.",
                        })
                        update_sync(sync_r, session_id, status="timed_out", finished_at=_now())
                        _persist_artifacts("timed_out", "Hết giờ chờ user.", "", record.step)
                        break

                    msg = json.loads(result[1])
                    if msg.get("type") == "cancel":
                        push("cancelled", {"reason": "Cancelled while waiting for user"})
                        update_sync(sync_r, session_id, status="cancelled", finished_at=_now())
                        _persist_artifacts("cancelled", "Session bị huỷ.", "", record.step)
                        break

                    answer = msg.get("answer", "")

                    # confirm_done: Sup-Agent xác nhận hoàn thành → done ngay, không gửi lại LLM
                    if answer.strip().lower() == "confirm_done":
                        duration = time.time() - session_start
                        done_payload = {
                            "step": record.step,
                            "message": "Sup-Agent xác nhận hoàn thành.",
                            "url_after": record.url_after or "",
                            "screenshot_url": screenshot_cdn or "",
                            "total_steps": record.step,
                            "duration_seconds": round(duration, 1),
                        }
                        if is_callback_mode:
                            done_payload["result_url"] = f"/v1/sessions/{session_id}/result"
                        push("done", done_payload)
                        update_sync(sync_r, session_id, status="done", finished_at=_now())
                        log_svc.log_session(session_id, "done", step=record.step,
                                            duration=round(duration, 1),
                                            message="confirm_done by sup-agent")
                        _persist_artifacts("done", "Sup-Agent xác nhận hoàn thành.",
                                           record.url_after or "", record.step)
                        break

                    update_sync(sync_r, session_id, status="running", ask_deadline_at="")
                    continue

                if record.is_done:
                    duration = time.time() - session_start
                    done_ev = record_to_done_event(record, session_id, record.step, duration,
                                                   screenshot_url_override=screenshot_cdn)
                    done_payload = done_ev.model_dump()
                    # Callback mode: thêm result_url vào done payload
                    if is_callback_mode:
                        done_payload["result_url"] = f"/v1/sessions/{session_id}/result"
                    push("done", done_payload)
                    update_sync(sync_r, session_id, status="done", finished_at=_now())
                    log_svc.log_session(session_id, "done", step=record.step,
                                        duration=round(duration, 1),
                                        message=done_ev.message or "")
                    _persist_artifacts(
                        "done",
                        done_ev.message or "Hoàn thành",
                        done_ev.url_after,
                        record.step,
                    )
                    break

                step_ev = record_to_step_event(record, session_id,
                                               screenshot_url_override=screenshot_cdn,
                                               annotated_url_override=annotated_cdn)
                push("step", step_ev.model_dump())
                log_svc.log_session(session_id, "step", step=record.step,
                                    action=record.action.get("action", ""),
                                    ref=record.action.get("ref", ""),
                                    url_after=record.url_after or "")

            except StopIteration:
                update_sync(sync_r, session_id, status="done", finished_at=_now())
                break

    except Exception as exc:
        code, msg = friendly_error(exc)
        _log.error(f"Session {session_id} error: {exc}", exc_info=True)
        push("failed", {"code": code, "message": msg})
        update_sync(sync_r, session_id, status="failed", error_msg=msg, finished_at=_now())
        _persist_artifacts("failed", msg, "", len(_collected_events), msg)
        log_svc.log_error(code, msg, session_id=session_id)
    finally:
        # Upload session log to DSC
        try:
            log_svc.upload_session_log(session_id)
        except Exception as e:
            _log.warning(f"[{session_id}] Session log upload failed: {e}")
        # Clean up resume queue
        sync_r.delete(f"resume:{session_id}")
        _log.info(f"[{worker_id}] Session {session_id} finished (mode={'callback' if is_callback_mode else 'sse'})")
