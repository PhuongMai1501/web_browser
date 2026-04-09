"""
api.py - FastAPI AI Tool Web — v1 API  [DEPRECATED — Phase 1a entry point]

# DEPRECATED: Phase 1b entry point is api/app.py
# Run: uvicorn api.app:app --host 0.0.0.0 --port 8000
# This file kept for reference only.

Protocol: SSE + REST Hybrid
- POST /v1/sessions              → tạo session, trả session_id ngay
- GET  /v1/sessions/{id}/stream  → SSE stream (GIỮ MỞ qua ask/resume)
- POST /v1/sessions/{id}/resume  → gửi answer, stream tự tiếp tục
- POST /v1/sessions/{id}/cancel  → huỷ
- GET  /v1/sessions/{id}         → status
- GET  /v1/sessions/{id}/steps/{n}/screenshot → lazy load ảnh
- GET  /v1/health
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

# ── Import LLM_base engine ─────────────────────────────────────────────────────
_LLM_BASE = Path(__file__).parent.parent / "LLM_base"
sys.path.insert(0, str(_LLM_BASE))

import browser_adapter as browser

from models import (
    CancelResponse, ErrorEvent,
    ResumeRequest, ResumeResponse, RunRequest,
    SessionCreatedResponse, SessionStatusResponse,
)
from session_manager import (
    ASK_TIMEOUT_SECONDS, SessionData,
    session_manager,
)
from config import MAX_STEPS_CAP, MIN_STEPS
from worker.job_handler import push_event, run_job

# ── App setup ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
)
_log = logging.getLogger(__name__)

app = FastAPI(title="AI Tool Web", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Phase 2: thu hẹp lại origin cụ thể
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Startup: background cleanup task ─────────────────────────────────────────

@app.on_event("startup")
async def _startup():
    asyncio.create_task(_cleanup_loop())
    asyncio.create_task(_ask_timeout_loop())


async def _cleanup_loop():
    """Sweep expired sessions mỗi 60 giây."""
    while True:
        await asyncio.sleep(60)
        n = session_manager.cleanup_expired()
        if n:
            _log.info(f"Cleaned up {n} expired sessions")


async def _ask_timeout_loop():
    """Tự cancel session bị blocked > ASK_TIMEOUT_SECONDS."""
    while True:
        await asyncio.sleep(30)
        now = datetime.now()
        for sess in session_manager.all_sessions():
            if sess.status == "blocked" and sess.blocked_at:
                elapsed = (now - sess.blocked_at).total_seconds()
                if elapsed > ASK_TIMEOUT_SECONDS:
                    _log.info(f"Ask timeout for session {sess.id}")
                    session_manager.mark_cancelled(sess.id)
                    push_event(sess, "error", ErrorEvent(
                        code="ASK_TIMEOUT",
                        message=f"Không nhận được câu trả lời sau {ASK_TIMEOUT_SECONDS}s. Session bị huỷ.",
                        recoverable=False,
                        timestamp=datetime.now().isoformat(),
                    ).model_dump())




# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/v1/health")
def health():
    running = session_manager.get_running()
    return {
        "status": "ok",
        "active_session": running.id if running else None,
    }


@app.post("/v1/sessions", response_model=SessionCreatedResponse, status_code=201)
async def create_session(req: RunRequest):
    """Tạo và bắt đầu agent session. Trả session_id ngay để client connect SSE."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise HTTPException(500, detail="OPENAI_API_KEY chưa được set")

    # Concurrency guard: chỉ 1 session chạy cùng lúc (1 browser)
    active = session_manager.get_running()
    if active:
        raise HTTPException(429, detail=f"Đang có session chạy: {active.id}. Huỷ trước khi tạo mới.")

    max_steps = max(MIN_STEPS, min(req.max_steps, MAX_STEPS_CAP))
    req = req.model_copy(update={"max_steps": max_steps})

    sess = session_manager.create(req.scenario, max_steps)
    loop = asyncio.get_event_loop()

    # Chạy agent trong thread pool (blocking subprocess)
    sess.task = asyncio.create_task(
        asyncio.to_thread(run_job, sess, req, api_key, loop)
    )

    _log.info(f"Session created: {sess.id} scenario={req.scenario} max_steps={max_steps}")

    return SessionCreatedResponse(
        session_id=sess.id,
        status="running",
        stream_url=f"/v1/sessions/{sess.id}/stream",
        created_at=sess.created_at.isoformat(),
    )


@app.get("/v1/sessions/{session_id}/stream")
async def stream_session(
    session_id: str,
    last_event_id: Optional[int] = Query(default=None, alias="lastEventId"),
):
    """
    SSE stream. Giữ kết nối MỞ suốt session kể cả khi agent bị blocked (ask).
    Hỗ trợ reconnect qua Last-Event-ID header.
    """
    sess = session_manager.get(session_id)
    if not sess:
        raise HTTPException(404, detail="Session không tồn tại")

    async def sse_generator():
        # Replay từ buffer nếu client reconnect
        if last_event_id is not None:
            for buffered in sess.event_buffer:
                data = buffered.get("data", {})
                step = data.get("step", 0)
                if step > last_event_id:
                    event_type = buffered["type"]
                    yield f"id: {step}\nevent: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        # Heartbeat + live events
        heartbeat_task = None

        async def send_heartbeat(q: asyncio.Queue):
            while True:
                await asyncio.sleep(15)
                await q.put({"type": "heartbeat", "data": {}})

        heartbeat_task = asyncio.create_task(send_heartbeat(sess.step_queue))

        try:
            while True:
                try:
                    payload = await asyncio.wait_for(sess.step_queue.get(), timeout=120)
                except asyncio.TimeoutError:
                    yield "event: heartbeat\ndata: {}\n\n"
                    continue

                event_type = payload["type"]
                data = payload.get("data", {})

                if event_type == "_done":
                    break

                if event_type == "heartbeat":
                    yield "event: heartbeat\ndata: {}\n\n"
                    continue

                step = data.get("step", 0)
                yield f"id: {step}\nevent: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

                # Stream giữ mở khi ask — KHÔNG break ở đây
                # Chỉ đóng khi done hoặc error
                if event_type in ("done", "error"):
                    break

        finally:
            if heartbeat_task:
                heartbeat_task.cancel()

    return StreamingResponse(sse_generator(), media_type="text/event-stream")


@app.post("/v1/sessions/{session_id}/resume", response_model=ResumeResponse)
def resume_session(session_id: str, req: ResumeRequest):
    """Gửi answer cho agent đang blocked. SSE stream tự tiếp tục."""
    sess = session_manager.get(session_id)
    if not sess:
        raise HTTPException(404, detail="Session không tồn tại")
    if sess.status == "completed":
        raise HTTPException(409, detail="SESSION_FINISHED")
    if sess.status != "blocked":
        raise HTTPException(409, detail="SESSION_NOT_BLOCKED")

    sess.answer_value = req.answer
    sess.blocked_event.set()   # Wake up agent thread

    _log.info(f"Session {session_id} resumed")
    return ResumeResponse(status="resumed", session_id=session_id)


@app.post("/v1/sessions/{session_id}/cancel", response_model=CancelResponse)
async def cancel_session(session_id: str):
    """Huỷ session đang chạy."""
    sess = session_manager.get(session_id)
    if not sess:
        raise HTTPException(404, detail="Session không tồn tại")
    if sess.status in ("completed", "cancelled", "error"):
        raise HTTPException(409, detail="SESSION_FINISHED")

    # Nếu agent đang blocked, unblock nó với signal cancel
    if sess.status == "blocked":
        sess.answer_value = None
        sess.blocked_event.set()

    if sess.task and not sess.task.done():
        sess.task.cancel()

    session_manager.mark_cancelled(session_id)
    _log.info(f"Session {session_id} cancelled at step {sess.current_step}")

    return CancelResponse(status="cancelled", steps_completed=sess.current_step)


@app.post("/v1/browser/reset")
async def reset_browser():
    """
    Đóng browser và huỷ session đang chạy (nếu có).
    Dùng khi click nút Reset trên UI — không phụ thuộc vào trạng thái session.
    """
    cancelled_id = None

    # Cancel session đang active nếu có
    running = session_manager.get_running()
    if running:
        if running.status == "blocked":
            running.answer_value = None
            running.blocked_event.set()
        if running.task and not running.task.done():
            running.task.cancel()
        session_manager.mark_cancelled(running.id)
        cancelled_id = running.id
        _log.info(f"Browser reset: cancelled session {running.id}")

    # Đóng browser (blocking subprocess → chạy trong thread)
    try:
        await asyncio.to_thread(browser.close_browser)
        _log.info("Browser reset: browser closed")
    except Exception as exc:
        _log.warning(f"Browser reset: close_browser failed (ignored): {exc}")

    return {"status": "reset", "session_cancelled": cancelled_id}


@app.get("/v1/sessions/{session_id}", response_model=SessionStatusResponse)
def get_session(session_id: str):
    """Lấy trạng thái session."""
    sess = session_manager.get(session_id)
    if not sess:
        raise HTTPException(404, detail="Session không tồn tại")

    return SessionStatusResponse(
        session_id=sess.id,
        status=sess.status,
        scenario=sess.scenario,
        current_step=sess.current_step,
        max_steps=sess.max_steps,
        created_at=sess.created_at.isoformat(),
        blocked_at=sess.blocked_at.isoformat() if sess.blocked_at else None,
        blocked_message=sess.blocked_message,
        completed_at=sess.completed_at.isoformat() if sess.completed_at else None,
        duration_seconds=sess.duration_seconds,
    )


@app.get("/v1/sessions/{session_id}/steps/{step_number}/screenshot")
def get_screenshot(
    session_id: str,
    step_number: int,
    annotated: bool = Query(default=False),
):
    """Serve screenshot PNG cho 1 step. Lazy load từ client."""
    sess = session_manager.get(session_id)
    if not sess:
        raise HTTPException(404, detail="Session không tồn tại")

    path_map = sess.annotated_paths if annotated else sess.screenshot_paths
    file_path = path_map.get(step_number)

    if not file_path or not Path(file_path).exists():
        raise HTTPException(404, detail=f"Screenshot cho step {step_number} không tồn tại")

    return FileResponse(
        file_path,
        media_type="image/png",
        headers={"Cache-Control": "immutable, max-age=31536000"},
    )
