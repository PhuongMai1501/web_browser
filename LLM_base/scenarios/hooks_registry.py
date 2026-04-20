"""
scenarios/hooks_registry.py — Python hook registry cho scenario.

Hook được đăng ký khi import module (decorator @hook). Scenario reference
hook bằng TÊN (string) qua ScenarioHooks, không eval code → an toàn khi
scenario được edit lúc runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


HOOK_REGISTRY: dict[str, Callable] = {}


def hook(name: str):
    """Decorator đăng ký 1 hook function vào HOOK_REGISTRY."""
    def deco(fn: Callable) -> Callable:
        if name in HOOK_REGISTRY:
            raise ValueError(f"Hook name already registered: {name}")
        HOOK_REGISTRY[name] = fn
        return fn
    return deco


def get_hook(name: Optional[str]) -> Optional[Callable]:
    if not name:
        return None
    fn = HOOK_REGISTRY.get(name)
    if fn is None:
        raise ValueError(
            f"Hook '{name}' chưa register. Các hook hợp lệ: {sorted(HOOK_REGISTRY)}"
        )
    return fn


def list_hooks() -> list[str]:
    return sorted(HOOK_REGISTRY)


@dataclass
class HookContext:
    """Gói dữ liệu hook cần — không expose toàn bộ state."""

    browser: Any                    # module browser_adapter
    spec: Any                       # ScenarioSpec
    context: dict                   # context từ request (email, password...)
    session_id: str


@dataclass
class HookResult:
    """Hook trả kết quả điều khiển flow của generic runner."""

    terminate: bool = False         # True → runner dừng và yield record (nếu có)
    record: Any = None              # StepRecord để yield cho client
    extra: dict = field(default_factory=dict)
