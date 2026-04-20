"""
scenarios/spec.py — Pydantic models cho scenario declarative config.

ScenarioSpec là source of truth cho 1 kịch bản: URL, goal, context schema,
prompt tuning, và tên các hook Python (tra cứu từ HOOK_REGISTRY).
Serialize được thành JSON để lưu Redis hoặc YAML để seed.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ScenarioHooks(BaseModel):
    """Các điểm hook Python mà scenario muốn chạy.

    Giá trị là TÊN hook đã register trong HOOK_REGISTRY (không phải code).
    None ở tất cả = scenario thuần declarative, chạy generic runner.
    """

    pre_check: Optional[str] = None       # trước agent loop; có thể kết thúc sớm
    post_step: Optional[str] = None       # sau mỗi step; có thể kết thúc sớm
    final_capture: Optional[str] = None   # khi terminate; chụp artifact đặc thù


class ScenarioSpec(BaseModel):
    id: str = Field(..., description="Unique id, ví dụ 'chang_login'")
    display_name: str
    description: str = ""
    enabled: bool = True
    builtin: bool = False                 # True nếu seed từ YAML (chặn delete cứng)
    version: int = 1                      # bump khi update qua API

    # Run config
    start_url: Optional[str] = None
    goal: str = ""                        # có thể chứa {placeholder} từ context
    max_steps_default: int = 20
    allowed_domains: list[str] = Field(default_factory=list)

    # Input schema — JSON Schema-lite: {"required": ["email"], "optional": [...]}
    context_schema: dict = Field(default_factory=dict)

    # Prompt tuning
    system_prompt_extra: str = ""

    hooks: ScenarioHooks = Field(default_factory=ScenarioHooks)
