"""
scenarios/spec.py — Pydantic models cho scenario declarative config.

ScenarioSpec là source of truth cho 1 kịch bản: URL, goal, context schema,
prompt tuning, và tên các hook Python (tra cứu từ HOOK_REGISTRY).
Serialize được thành JSON để lưu Redis hoặc YAML để seed.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .flow_models import FailureRule, FlowStep, InputField, SuccessRule


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

    # v2: chế độ chạy. Default 'agent' để spec v1 không đổi hành vi.
    #   - flow:   chạy `steps` tuần tự qua flow_runner (không cần LLM quyết).
    #   - agent:  chạy LLM autonomous với `goal` (v1 behavior).
    #   - hybrid: Sprint 3 — flow trước, agent fallback sau.
    mode: Literal["flow", "agent", "hybrid"] = "agent"

    # Run config
    start_url: Optional[str] = None
    goal: str = ""                        # có thể chứa {placeholder} từ context
    max_steps_default: int = 20
    allowed_domains: list[str] = Field(default_factory=list)

    # Per-scenario proxy (2026-05-28) — agent-browser launch-time option.
    # None → dùng proxy mặc định từ K8s Deployment env (HTTP_PROXY).
    # Set khi scenario truy cập site local riêng cần proxy khác.
    # Proxy áp dụng lúc open browser (đầu scenario); worker close → reopen
    # giữa các scenario nên proxy mới session sau auto refresh.
    proxy: Optional[str] = None            # vd "http://10.20.30.40:8080"
    proxy_bypass: Optional[str] = None     # vd "localhost,*.internal" (NO_PROXY)

    # v2 input declaration (ưu tiên). context_schema giữ cho back-compat;
    # validator ưu tiên `inputs` nếu có, fallback về context_schema.
    inputs: list[InputField] = Field(default_factory=list)

    # v2 flow fields — chỉ cần set khi mode='flow' hoặc 'hybrid'.
    steps: list[FlowStep] = Field(default_factory=list)
    success: Optional[SuccessRule] = None
    failure: Optional[FailureRule] = None

    # Legacy — giữ để các spec cũ không vỡ.
    context_schema: dict = Field(default_factory=dict)

    # Prompt tuning (dùng ở mode=agent / hybrid)
    system_prompt_extra: str = ""

    # JSON Schema mô tả output mà step `action: extract_data` sẽ điền.
    # Khi flow chạy xong, worker merge data đã extract vào result.json["data"].
    # None hoặc {} = không có extract data → result.json không có field "data".
    output_schema: dict = Field(default_factory=dict)

    hooks: ScenarioHooks = Field(default_factory=ScenarioHooks)
