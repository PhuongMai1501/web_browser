"""
llm_planner.py - Gọi OpenAI GPT-4o mini với vision để quyết định browser action.
"""

import json
import os
import re
import time
from datetime import datetime
from openai import OpenAI, RateLimitError
from prompts import SYSTEM_PROMPT, build_user_prompt, build_retry_prompt, build_visual_fallback_prompt, build_history_prompt

_LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
_LLM_TIMEOUT_S = int(os.getenv("LLM_TIMEOUT", "60"))
_LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
_RETRY_DELAYS = [1, 3, 8]  # seconds, exponential backoff khi gặp 429

_VALID_ACTIONS = {"click", "type", "wait", "done", "ask"}


# ── Trace (bật từ runner để log từng LLM call cho debug) ─────────────────────
_TRACE_ENABLED = False
_TRACE_BUFFER: list[dict] = []
_TRACE_CURRENT_STEP = 0
_TRACE_PROMPT_MAX = 20000     # prompt có thể có snapshot 10-20KB
_TRACE_RESPONSE_MAX = 8000


def start_trace() -> None:
    global _TRACE_ENABLED
    _TRACE_BUFFER.clear()
    _TRACE_ENABLED = True


def stop_trace() -> list[dict]:
    global _TRACE_ENABLED
    _TRACE_ENABLED = False
    data = list(_TRACE_BUFFER)
    _TRACE_BUFFER.clear()
    return data


def set_trace_step(step: int) -> None:
    global _TRACE_CURRENT_STEP
    _TRACE_CURRENT_STEP = step


def _truncate(s: str, limit: int) -> str:
    if not s:
        return ""
    if len(s) <= limit:
        return s
    return s[:limit] + f"... [truncated {len(s) - limit} chars]"


def _validate_action(action: dict) -> None:
    """
    Schema validation đầy đủ cho LLM response.
    Raise ValueError nếu action thiếu field bắt buộc hoặc sai kiểu dữ liệu.
    """
    action_type = action.get("action")
    if action_type not in _VALID_ACTIONS:
        raise ValueError(
            f"action không hợp lệ: '{action_type}'. Hợp lệ: {_VALID_ACTIONS}"
        )

    if action_type in ("click", "type"):
        ref = action.get("ref", "")
        if not ref or not re.match(r"^@?e\d+$", str(ref)):
            raise ValueError(
                f"action '{action_type}' thiếu hoặc sai ref: '{ref}'. "
                "Ref phải có dạng e<số> hoặc @e<số>."
            )

    if action_type == "type":
        if not action.get("text") and action.get("text") != "":
            raise ValueError("action 'type' thiếu field 'text'")
        # text rỗng vẫn cho phép (xóa nội dung field)

    if action_type == "wait":
        ms = action.get("ms")
        if ms is None:
            action["ms"] = 1000  # default
        elif not isinstance(ms, (int, float)) or ms <= 0:
            raise ValueError(f"action 'wait' có ms không hợp lệ: '{ms}'")

    if action_type == "ask":
        if not action.get("message"):
            raise ValueError("action 'ask' thiếu field 'message'")
        ask_type = action.get("ask_type", "question")
        if ask_type not in ("question", "error"):
            raise ValueError(f"ask_type không hợp lệ: '{ask_type}'")


def _sanitize(text: str) -> str:
    """
    Xóa null bytes và control characters không hợp lệ trong JSON string.
    Giữ lại: newline (\\n), tab (\\t), carriage return (\\r).
    Nguyên nhân lỗi 400: agent-browser trên Windows đôi khi trả về \\x00 trong output.
    """
    return "".join(c for c in text if c >= " " or c in "\n\r\t")


def _extract_prompt_text(user_content: list) -> str:
    """Trích xuất phần text từ user_content (bỏ qua base64 ảnh) để lưu log."""
    parts = []
    img_count = 0
    for item in user_content:
        if item.get("type") == "text":
            parts.append(item["text"])
        elif item.get("type") == "image_url":
            img_count += 1
    if img_count:
        parts.append(f"[+ {img_count} ảnh đính kèm]")
    return "\n\n".join(parts)


def _sanitize_content(user_content: list) -> list:
    """Sanitize tất cả text parts trong user_content trước khi gửi lên API."""
    result = []
    for item in user_content:
        if item.get("type") == "text":
            result.append({**item, "text": _sanitize(item["text"])})
        else:
            result.append(item)
    return result


def _compose_system(extra: str) -> str:
    """Nối SYSTEM_PROMPT chung với phần scenario-specific (nếu có)."""
    if not extra:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n--- SCENARIO-SPECIFIC ---\n{extra.strip()}"


def _call_llm(client: OpenAI, system: str, user_content: list) -> tuple[dict, str, str]:
    """
    Gọi API và trả về (action_dict, raw_response_text, prompt_text).
    Tự động retry với backoff khi gặp 429 rate limit.
    prompt_text là nội dung text gửi lên (không gồm base64 ảnh).
    """
    clean_content = _sanitize_content(user_content)
    prompt_text = _extract_prompt_text(clean_content)
    for attempt in range(len(_RETRY_DELAYS) + 1):
        if attempt > 0:
            time.sleep(_RETRY_DELAYS[attempt - 1])
        ts = datetime.now().isoformat()
        started = time.monotonic()
        try:
            response = client.chat.completions.create(
                model=_LLM_MODEL,
                messages=[
                    {"role": "system", "content": _sanitize(system)},
                    {"role": "user", "content": clean_content},
                ],
                response_format={"type": "json_object"},
                max_tokens=512,
                temperature=0.1,
                timeout=_LLM_TIMEOUT_S,
            )
            raw = response.choices[0].message.content
            duration_ms = int((time.monotonic() - started) * 1000)
            if _TRACE_ENABLED:
                _TRACE_BUFFER.append({
                    "step": _TRACE_CURRENT_STEP,
                    "timestamp": ts,
                    "model": _LLM_MODEL,
                    "attempt": attempt,
                    "duration_ms": duration_ms,
                    "system_prompt": _truncate(system, _TRACE_PROMPT_MAX),
                    "user_prompt": _truncate(prompt_text, _TRACE_PROMPT_MAX),
                    "raw_response": _truncate(raw or "", _TRACE_RESPONSE_MAX),
                    "image_count": sum(1 for it in clean_content if it.get("type") == "image_url"),
                })
            action = json.loads(raw)
            _validate_action(action)
            return action, raw, prompt_text
        except RateLimitError as exc:
            if _TRACE_ENABLED:
                _TRACE_BUFFER.append({
                    "step": _TRACE_CURRENT_STEP,
                    "timestamp": ts,
                    "model": _LLM_MODEL,
                    "attempt": attempt,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "exception": f"RateLimitError: {exc}",
                })
            if attempt == len(_RETRY_DELAYS):
                raise  # hết retry → để caller xử lý


def decide_action(
    goal: str,
    snapshot: str,
    api_key: str,
    step: int = 1,
) -> tuple[dict, str, str]:
    """
    Gọi GPT-4o mini với snapshot text only — không gửi ảnh.
    LLM suy luận từ metadata (href, icon, aria-label, text) trong snapshot.

    Returns:
        (action_dict, raw_response_text, prompt_text)
    """
    client = OpenAI(api_key=api_key)
    user_content = [{"type": "text", "text": build_user_prompt(goal, snapshot, step)}]
    return _call_llm(client, SYSTEM_PROMPT, user_content)


def decide_action_visual_fallback(
    goal: str,
    snapshot: str,
    screenshot_b64: str,
    undescribed_ref: str,
    api_key: str,
    step: int = 1,
    annotated_b64: str = "",
    system_prompt_extra: str = "",
) -> tuple[dict, str, str]:
    """
    Fallback khi element không có mô tả — gửi fresh snapshot + ảnh để LLM xác nhận
    bằng thị giác thay vì dựa vào text/label.

    Returns:
        (action_dict, raw_response_text, prompt_text)
    """
    client = OpenAI(api_key=api_key)

    # Dùng detail=low để tiết kiệm token — đủ để nhận diện vị trí element
    user_content = [
        {
            "type": "text",
            "text": build_visual_fallback_prompt(goal, snapshot, undescribed_ref, step),
        },
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{screenshot_b64}",
                "detail": "low",
            },
        },
    ]
    if annotated_b64 and annotated_b64 != screenshot_b64:
        user_content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{annotated_b64}",
                    "detail": "low",
                },
            }
        )

    return _call_llm(client, _compose_system(system_prompt_extra), user_content)


def decide_action_autonomous(
    goal: str,
    history: list[dict],
    snapshot: str,
    api_key: str,
    step: int = 1,
    context: dict | None = None,
    system_prompt_extra: str = "",
) -> tuple[dict, str, str]:
    """
    Gọi LLM với lịch sử hành động đầy đủ — autonomous mode.
    LLM tự suy luận bước tiếp theo dựa trên history + snapshot.

    Returns:
        (action_dict, raw_response_text, prompt_text)
    """
    client = OpenAI(api_key=api_key)
    user_content = [
        {"type": "text", "text": build_history_prompt(goal, history, snapshot, step, context)}
    ]
    return _call_llm(client, _compose_system(system_prompt_extra), user_content)


def decide_action_retry(
    goal: str,
    snapshot: str,
    invalid_ref: str,
    api_key: str,
    step: int = 1,
    system_prompt_extra: str = "",
) -> tuple[dict, str, str]:
    """
    Retry khi LLM trả về ref không hợp lệ — text only, không gửi ảnh.

    Returns:
        (action_dict, raw_response_text, prompt_text)
    """
    client = OpenAI(api_key=api_key)
    user_content = [
        {"type": "text", "text": build_retry_prompt(goal, snapshot, invalid_ref, step)}
    ]
    return _call_llm(client, _compose_system(system_prompt_extra), user_content)
