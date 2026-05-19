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
    # Nếu `scenario_yaml` được set, tool-web bỏ qua DB lookup và dùng YAML
    # inline trực tiếp; `scenario` khi đó chỉ là tên hiển thị cho log.
    scenario: str = "chang_login"
    name: Optional[str] = Field(default=None, max_length=120)  # label hiển thị màn admin
    goal: Optional[str] = None           # override goal của spec (như custom cũ)
    url: Optional[str] = None            # override start_url của spec
    context: Optional[dict] = None       # {"email": "...", "password": "..."}
    max_steps: int = Field(default=20, ge=3, le=30)
    callback_url: Optional[str] = None   # set → callback mode (không cần SSE)
    callback_secret: Optional[str] = None  # HMAC signature (optional)
    # Custom YAML inline — Sup Agent paste YAML scenario trực tiếp, KHÔNG cần
    # tạo scenario trong DB trước. Tool-web parse + validate qua yaml_normalizer,
    # tạo spec ad-hoc (không lưu DB), bỏ qua DB lookup scenario.
    scenario_yaml: Optional[str] = None
    # Query mode — user gõ NL description tự nhiên, API tự gọi LLM gen YAML
    # rồi chạy luôn. YAML đã gen trả về trong SessionCreatedResponse.scenario_yaml
    # để Sup Agent hiển thị editor cho user xem/sửa rồi chạy lại bằng mode
    # scenario_yaml. Chỉ 1 trong 3 mode được set tại 1 thời điểm.
    query: Optional[str] = None
    query_site_hint: Optional[str] = None   # optional: domain hint cho LLM (vd "chang.fpt.net")
    # Nếu True: với các inputs[].required+source=context thiếu trong request
    # context, tool-web TỰ ĐỘNG đổi sang source=ask_user để worker hỏi user
    # runtime qua SSE thay vì reject 422. Default None → auto-enable cho mode
    # `query` (vì user gõ NL thường không khai báo trước credentials), tắt
    # cho mode `scenario`/`scenario_yaml` để giữ behavior cũ.
    ask_missing_inputs: Optional[bool] = None
    # Task tracking — group nhiều session iterations (lần thử) cho cùng 1 yêu
    # cầu của end-user. Sup Agent sinh task_id 1 lần khi user mở chat, gửi
    # kèm mọi POST /v1/sessions tiếp theo. Nếu rỗng → API auto-gen UUID.
    # Có thể dùng external_id (vd Sup Agent's conversation_id, Linear ticket)
    # làm task_id để cross-trace.
    task_id: Optional[str] = Field(default=None, max_length=128)
    # Nếu True (default): khi tạo session với task_id, API tự cancel mọi
    # session non-terminal CÙNG task_id để giải phóng worker. User loop sửa
    # YAML → iteration cũ tự bị kill, không tốn worker. Set False khi muốn
    # chạy parallel iteration (A/B benchmark).
    cancel_prev_iterations: bool = True


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
    # upload_download — file vừa upload lên CDN/MinIO. Sup Agent route file
    # cho user qua `downloaded_cdn_url`. Rỗng cho mọi action khác.
    downloaded_filename: str = ""
    downloaded_cdn_url: str = ""
    # Text/label readable của element vừa tác động (click button "Đăng nhập",
    # fill field "Email"...). UI Sup Agent hiển thị "Tool-web vừa click nút
    # <X>" thay vì hiển thị ref selector. Rỗng cho action không có target
    # (goto, wait_for, eval_js, extract_data).
    target_label: str = ""


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
    # Khi tạo session bằng mode `query`, API trả lại YAML đã LLM-generate
    # để Sup Agent hiển thị cho user xem/sửa rồi chạy lại bằng mode scenario_yaml.
    # Rỗng cho mode `scenario` và `scenario_yaml`.
    scenario_yaml: Optional[str] = None
    scenario_id: Optional[str] = None    # id auto-gen của YAML ad-hoc (vd "_q_abc123")
    generated_from_query: bool = False   # True nếu YAML được sinh từ query
    model_used: Optional[str] = None     # model LLM dùng gen (vd "gpt-4o-mini")
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    # Task tracking — Sup Agent dùng để group session iterations.
    # task_id = Sup Agent gửi (nếu có), hoặc API auto-gen UUID.
    # iteration = số thứ tự lần thử trong task (1, 2, 3...).
    task_id: str = ""
    iteration: int = 1
    # cancelled_prev_count = số iteration cũ đã bị auto-cancel khi tạo cái này.
    # 0 nếu không có iteration trước, hoặc cancel_prev_iterations=false.
    cancelled_prev_count: int = 0


class SessionStatusResponse(BaseModel):
    session_id: str
    status: str   # queued|assigned|running|waiting_for_user|done|failed|cancelled|timed_out
    scenario: str
    name: Optional[str] = None
    current_step: int
    max_steps: int
    created_at: str
    assigned_worker: Optional[str] = None
    ask_deadline_at: Optional[str] = None
    error_msg: Optional[str] = None
    finished_at: Optional[str] = None
    # Task tracking — đồng bộ với SessionCreatedResponse
    task_id: Optional[str] = None
    iteration: Optional[int] = None
    # Legacy fields kept for UI compatibility
    blocked_at: Optional[str] = None
    blocked_message: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None


# ── Admin endpoint payloads ────────────────────────────────────────────────────

class AdminSessionItem(BaseModel):
    """1 row trong màn admin — list session đang chạy."""
    session_id: str
    status: str
    scenario: str
    name: Optional[str] = None
    user_id: Optional[str] = None
    task_id: Optional[str] = None        # Task grouping (phase task_id)
    iteration: Optional[int] = None      # Iteration trong task (1, 2, 3...)
    current_step: int = 0
    max_steps: int = 0
    assigned_worker: Optional[str] = None
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error_msg: Optional[str] = None


class AdminWorkerItem(BaseModel):
    worker_id: str
    status: str          # "idle" | "busy" | "unknown"
    current_session: Optional[str] = None
    started_at: Optional[str] = None
    last_heartbeat: Optional[str] = None
    seconds_since_heartbeat: Optional[float] = None


class AdminSessionsResponse(BaseModel):
    sessions: list[AdminSessionItem]
    total: int
    workers_alive: int
    workers_busy: int
    queue_length: int


class TaskSessionsResponse(BaseModel):
    """Response cho GET /v1/tasks/{task_id}/sessions — list iterations."""
    task_id: str
    iterations: list[AdminSessionItem]   # sort theo iteration tăng dần
    total: int
    has_running: bool                    # còn iteration non-terminal không
    latest_iteration: int                # iteration number lớn nhất


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
    # Chỉ quảng cáo annotated_url khi worker đã thật sự lưu (override truthy).
    # Fallback `?annotated=true` cũ gây 404 silent ở UI khi annotated chưa upload.
    annotated_url = annotated_url_override

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
        downloaded_filename=action.get("downloaded_filename") or "",
        downloaded_cdn_url=action.get("downloaded_cdn_url") or "",
        target_label=action.get("target_label") or "",
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
