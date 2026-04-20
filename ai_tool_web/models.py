"""
models.py - Pydantic request/response models cho ai_tool_web API.

StepRecord (internal) vs StepEvent (external):
- StepRecord: đầy đủ data kể cả debug fields (snapshot, llm_prompt, base64...)
- StepEvent: chỉ data cần thiết cho client — không expose debug/secret fields
"""

from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field
from datetime import datetime


# ── Request Models ─────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    # Scenario id — validate runtime qua ScenarioStore (không dùng Literal để
    # có thể thêm scenario mới qua admin API /v1/scenarios).
    scenario: str = "chang_login"
    goal: Optional[str] = None           # override goal của spec (như custom cũ)
    url: Optional[str] = None            # override start_url của spec
    context: Optional[dict] = None       # {"email": "...", "password": "..."}
    max_steps: int = Field(default=20, ge=3, le=30)
    callback_url: Optional[str] = None   # set → callback mode (không cần SSE)
    callback_secret: Optional[str] = None  # HMAC signature (optional)


class ResumeRequest(BaseModel):
    answer: str


# ── SSE Event Payloads ─────────────────────────────────────────────────────────

class StepEvent(BaseModel):
    """
    Dữ liệu 1 step trả ra ngoài qua SSE.
    Không có: snapshot, llm_prompt, llm_raw_response, screenshot_b64 (secrets/debug).
    """
    step: int
    action: str                          # click | type | wait | ask | done
    ref: str = ""
    text_typed: str = ""                 # text điền vào field (che password nếu là password field)
    reason: str = ""
    url_before: str = ""
    url_after: str = ""
    screenshot_url: str = ""             # GET /v1/sessions/{id}/steps/{n}/screenshot
    annotated_screenshot_url: str = ""
    has_error: bool = False
    error: str = ""
    visual_fallback_used: bool = False
    timestamp: str = ""


class AskEvent(BaseModel):
    """Agent bị blocked, cần user trả lời."""
    step: int
    ask_type: Literal["question", "error"] = "question"
    message: str
    reason: str = ""
    screenshot_url: str = ""
    timestamp: str = ""


class DoneEvent(BaseModel):
    """Agent hoàn thành."""
    step: int
    message: str = ""
    url_after: str = ""
    screenshot_url: str = ""
    total_steps: int = 0
    duration_seconds: float = 0
    timestamp: str = ""


class ErrorEvent(BaseModel):
    """Lỗi không recover được."""
    code: str
    message: str
    recoverable: bool = False
    timestamp: str = ""


# ── REST Response Models ───────────────────────────────────────────────────────

class SessionCreatedResponse(BaseModel):
    session_id: str
    status: str = "queued"
    stream_url: str
    mode: str = "sse"                    # "sse" | "callback"
    created_at: str
    queue_position: Optional[int] = None


class SessionStatusResponse(BaseModel):
    session_id: str
    status: str   # queued|assigned|running|waiting_for_user|done|failed|cancelled|timed_out
    scenario: str
    current_step: int
    max_steps: int
    created_at: str
    assigned_worker: Optional[str] = None
    ask_deadline_at: Optional[str] = None
    error_msg: Optional[str] = None
    finished_at: Optional[str] = None
    # Legacy fields kept for UI compatibility
    blocked_at: Optional[str] = None
    blocked_message: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None


class ResumeResponse(BaseModel):
    status: str = "resumed"
    session_id: str


class CancelResponse(BaseModel):
    status: str = "cancelled"
    steps_completed: int


# ── Helpers ────────────────────────────────────────────────────────────────────

_SECRET_FIELD_NAMES = frozenset({"password", "pass", "secret", "token", "otp", "pin", "passwd"})


def record_to_step_event(
    record,
    session_id: str,
    screenshot_url_override: str = "",
    annotated_url_override: str = "",
) -> StepEvent:
    """Chuyển StepRecord (internal) → StepEvent (external). Che secret fields.

    screenshot_url_override: CDN URL nếu đã upload, fallback về /v1/.../screenshot.
    annotated_url_override:  CDN URL của ảnh annotated nếu đã upload.
    """
    action = record.action or {}
    action_type = action.get("action") or "unknown"
    ref = action.get("ref") or ""
    text = action.get("text") or ""

    # Che password nếu field name liên quan đến secret
    # (heuristic: nếu snapshot có "password" gần ref này)
    snapshot_lower = (record.snapshot or "").lower()
    is_secret_field = any(k in snapshot_lower for k in _SECRET_FIELD_NAMES)
    safe_text = "***" if (action_type == "type" and text and is_secret_field) else text

    n = record.step
    base = f"/v1/sessions/{session_id}/steps/{n}"
    has_local = bool(record.screenshot_path)

    screenshot_url = screenshot_url_override or (f"{base}/screenshot" if has_local else "")
    annotated_url = annotated_url_override or (f"{base}/screenshot?annotated=true" if has_local else "")

    return StepEvent(
        step=n,
        action=action_type,
        ref=ref,
        text_typed=safe_text,
        reason=action.get("reason") or "",
        url_before=record.url_before or "",
        url_after=record.url_after or "",
        screenshot_url=screenshot_url,
        annotated_screenshot_url=annotated_url,
        has_error=bool(record.error),
        error=record.error or "",
        visual_fallback_used=record.visual_fallback_used,
        timestamp=record.timestamp or "",
    )


def record_to_ask_event(
    record,
    session_id: str,
    screenshot_url_override: str = "",
) -> AskEvent:
    action = record.action or {}
    n = record.step
    base = f"/v1/sessions/{session_id}/steps/{n}"
    screenshot_url = screenshot_url_override or (f"{base}/screenshot" if record.screenshot_path else "")
    return AskEvent(
        step=n,
        ask_type=action.get("ask_type") or "question",
        message=action.get("message") or "",
        reason=action.get("reason") or "",
        screenshot_url=screenshot_url,
        timestamp=record.timestamp or "",
    )


def record_to_done_event(
    record,
    session_id: str,
    total_steps: int,
    duration: float,
    screenshot_url_override: str = "",
) -> DoneEvent:
    action = record.action or {}
    n = record.step
    base = f"/v1/sessions/{session_id}/steps/{n}"
    screenshot_url = screenshot_url_override or (f"{base}/screenshot" if record.screenshot_path else "")
    return DoneEvent(
        step=n,
        message=action.get("message") or "Hoàn thành",
        url_after=record.url_after or "",
        screenshot_url=screenshot_url,
        total_steps=total_steps,
        duration_seconds=round(duration, 1),
        timestamp=record.timestamp or "",
    )
