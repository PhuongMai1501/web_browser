"""
scenarios/action_registry.py — Registry cho các action chuẩn của mode=flow.

Mỗi action là 1 function ký hiệu: (runtime, step) → ActionResult
- runtime: FlowRuntime (browser adapter, context, session_id, emit helper)
- step:    FlowStep instance

Action reference trong spec bằng TÊN string. Admin chỉ chọn được từ danh
sách đã register ở Python startup → runtime-editable vẫn an toàn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


ACTION_REGISTRY: dict[str, Callable] = {}


def action(name: str):
    """Decorator đăng ký 1 action function vào ACTION_REGISTRY."""
    def deco(fn: Callable) -> Callable:
        if name in ACTION_REGISTRY:
            raise ValueError(f"Action '{name}' đã register")
        ACTION_REGISTRY[name] = fn
        return fn
    return deco


def get_action(name: str) -> Callable:
    fn = ACTION_REGISTRY.get(name)
    if fn is None:
        raise KeyError(
            f"Action '{name}' chưa register. Action hợp lệ: {sorted(ACTION_REGISTRY)}"
        )
    return fn


def list_actions() -> list[str]:
    return sorted(ACTION_REGISTRY)


# ── Runtime types ─────────────────────────────────────────────────────────────

@dataclass
class ActionResult:
    """Kết quả 1 action."""

    ok: bool = True
    error: str = ""                 # nếu ok=False
    ask_user: bool = False          # nếu True → flow_runner yield ask, chờ resume
    ask_field: str = ""             # tên field trong context để ghi answer
    ask_prompt: str = ""
    url_before: str = ""
    url_after: str = ""
    ref_used: str = ""              # ref element mà action tác động (nếu có)
    text_typed: str = ""            # value đã điền (đã mask nếu secret)
    action_type: str = ""           # tên action (để log/event)
    reason: str = ""                # mô tả ngắn gọn cho UI
    nested: Optional[list] = None   # kết quả các action con (cho if_visible)
