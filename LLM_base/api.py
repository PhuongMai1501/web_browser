"""
api.py - FastAPI wrapper cho LLM_base agent.

Endpoints:
  GET  /health              → kiểm tra service
  POST /run                 → bắt đầu agent, stream steps qua SSE
  POST /resume/{session_id} → gửi answer khi agent bị blocked (action=ask)
"""

import json
import os
import queue
import threading
import uuid
from typing import Optional

import browser_adapter as browser
from openai import RateLimitError
from runner import run_agent_autonomous
from scenarios.chang_login import (
    run_chang_login_autonomous,
    CHANG_URL,
    CHANG_AUTONOMOUS_GOAL,
)
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="Chang LLM Agent API")

# session_id → session dict
_sessions: dict[str, dict] = {}


# ── Request models ─────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    scenario: str = "chang_login"     # "chang_login" | "custom"
    goal: Optional[str] = None        # chỉ dùng khi scenario="custom"
    url: Optional[str] = None         # chỉ dùng khi scenario="custom"
    context: Optional[dict] = None    # {"email": "...", "password": "..."}
    max_steps: int = 20               # số bước tối đa (3–30)


class ResumeRequest(BaseModel):
    answer: str


# ── Helpers ────────────────────────────────────────────────────────────────────

def _friendly_error(e: Exception) -> str:
    """Map exception → thông báo thân thiện."""
    if isinstance(e, RateLimitError):
        return "API key đã đạt giới hạn rate limit. Vui lòng thử lại sau ít phút."
    if isinstance(e, TimeoutError):
        return "Browser không phản hồi (timeout). Kiểm tra agent-browser còn chạy không."
    if isinstance(e, json.JSONDecodeError):
        return "LLM trả về response không hợp lệ (không phải JSON). Thử lại."
    if isinstance(e, ConnectionError):
        return "Mất kết nối. Kiểm tra mạng và agent-browser."
    if isinstance(e, ValueError) and "Domain" in str(e):
        return f"URL bị chặn bởi domain allowlist: {e}"
    return f"Lỗi: {e}"


def _record_to_dict(record) -> dict:
    return {
        "step": record.step,
        "action": record.action,
        "snapshot": record.snapshot,
        "url_before": record.url_before,
        "url_after": record.url_after,
        "is_blocked": record.is_blocked,
        "is_done": record.is_done,
        "error": record.error,
        "llm_prompt": record.llm_prompt,
        "llm_raw_response": record.llm_raw_response,
        "visual_fallback_used": record.visual_fallback_used,
        "screenshot_b64": record.screenshot_b64,
        "annotated_screenshot_b64": record.annotated_screenshot_b64,
        "post_snapshot": record.post_snapshot,
    }


def _build_generator(req: RunRequest, api_key: str):
    """
    Khởi tạo generator phù hợp theo scenario.
    Tái tạo đúng logic từ app.py gốc.
    """
    if req.scenario == "chang_login":
        # Dùng run_chang_login_autonomous — mở URL, wait, pre-check, goal chi tiết
        return run_chang_login_autonomous(
            api_key=api_key,
            context=req.context,
            max_steps=req.max_steps,
        )

    # scenario == "custom": mở URL do user cung cấp rồi chạy agent
    target_url = req.url or CHANG_URL
    try:
        browser.open_url(target_url)
        browser.wait_ms(2000)
    except Exception as e:
        raise RuntimeError(f"Không mở được URL '{target_url}': {e}")

    goal = req.goal or f"Thực hiện tác vụ trên {target_url}"
    return run_agent_autonomous(
        goal=goal,
        api_key=api_key,
        context=req.context,
        max_steps=req.max_steps,
    )


# ── Background thread chạy agent ──────────────────────────────────────────────

def _agent_thread(session_id: str, req: RunRequest, api_key: str):
    """Chạy agent trong thread riêng, đẩy kết quả vào step_queue."""
    sess = _sessions[session_id]
    step_q: queue.Queue = sess["step_queue"]

    try:
        gen = _build_generator(req, api_key)
        sess["gen"] = gen
        answer = None

        while True:
            try:
                record = gen.send(answer)
                answer = None
                step_q.put(("step", record))

                if record.is_blocked:
                    # Chờ caller gửi answer qua POST /resume/{session_id}
                    sess["answer_event"].wait()
                    sess["answer_event"].clear()
                    answer = sess.get("answer_value", "")

                if record.is_done:
                    break

            except StopIteration:
                break

    except Exception as exc:
        step_q.put(("error", _friendly_error(exc)))
    finally:
        step_q.put(("done", None))
        sess["finished"] = True


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run")
def run_agent(req: RunRequest):
    """
    Bắt đầu agent. Trả về SSE stream — mỗi event là 1 step JSON.
    Stream dừng khi agent bị blocked (action=ask) hoặc done.

    Body mặc định cho chang login:
      {"scenario": "chang_login", "context": {"email": "...", "password": "..."}}

    Body cho custom goal:
      {"scenario": "custom", "goal": "...", "url": "https://...", "max_steps": 15}
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise HTTPException(500, "OPENAI_API_KEY chưa được set trong .env")

    max_steps = max(3, min(req.max_steps, 30))
    req = req.model_copy(update={"max_steps": max_steps})

    session_id = str(uuid.uuid4())
    step_queue: queue.Queue = queue.Queue()
    answer_event = threading.Event()

    _sessions[session_id] = {
        "step_queue": step_queue,
        "answer_event": answer_event,
        "answer_value": None,
        "finished": False,
        "gen": None,
    }

    thread = threading.Thread(
        target=_agent_thread,
        args=(session_id, req, api_key),
        daemon=True,
    )
    thread.start()
    _sessions[session_id]["thread"] = thread

    def sse_stream():
        # Event đầu tiên: session_id để client biết ID cho /resume
        yield f"data: {json.dumps({'session_id': session_id})}\n\n"

        while True:
            try:
                kind, data = step_queue.get(timeout=120)
            except queue.Empty:
                yield f"data: {json.dumps({'error': 'timeout sau 120s'})}\n\n"
                break

            if kind == "step":
                yield f"data: {json.dumps(_record_to_dict(data), ensure_ascii=False)}\n\n"
                if data.is_blocked or data.is_done:
                    break
            elif kind == "error":
                yield f"data: {json.dumps({'error': data})}\n\n"
                break
            elif kind == "done":
                break

    return StreamingResponse(sse_stream(), media_type="text/event-stream")


@app.post("/resume/{session_id}")
def resume(session_id: str, req: ResumeRequest):
    """
    Gửi answer cho agent đang blocked (action=ask).
    Trả về list các steps tiếp theo cho đến khi blocked hoặc done.
    """
    sess = _sessions.get(session_id)
    if not sess:
        raise HTTPException(404, "Session không tồn tại")
    if sess.get("finished"):
        raise HTTPException(400, "Session đã kết thúc")

    step_q: queue.Queue = sess["step_queue"]
    sess["answer_value"] = req.answer
    sess["answer_event"].set()

    steps = []
    while True:
        try:
            kind, data = step_q.get(timeout=120)
        except queue.Empty:
            break

        if kind == "step":
            steps.append(_record_to_dict(data))
            if data.is_blocked or data.is_done:
                break
        elif kind in ("error", "done"):
            break

    return {
        "steps": steps,
        "finished": sess.get("finished", False),
    }
