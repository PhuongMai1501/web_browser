"""
scenarios/flow_models.py — Pydantic models cho mode=flow (declarative steps).

Phân biệt rõ với ScenarioSpec:
- ScenarioSpec chứa metadata + list FlowStep.
- FlowStep chỉ mô tả 1 hành động + target, không biết runner.
- Runner đọc FlowStep, tra ACTION_REGISTRY, thi hành.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


# ── Input field ────────────────────────────────────────────────────────────────

class InputField(BaseModel):
    """1 biến admin khai báo để scenario sử dụng.

    - `source=context`: lấy từ `session.context` (request body gửi lên).
    - `source=ask_user`: hỏi user runtime qua action `ask_user` trong steps.
    """

    name: str
    type: Literal["string", "secret", "number", "bool"] = "string"
    required: bool = False
    source: Literal["context", "ask_user"] = "context"
    default: Optional[Any] = None
    description: str = ""


# ── Target ────────────────────────────────────────────────────────────────────

class TargetSpec(BaseModel):
    """Mô tả cách tìm element trên trang. Ít nhất 1 field phải set.

    Semantics:
    - `*_any`: OR — element phải chứa **BẤT KỲ** string nào trong list.
    - `*_all`: AND — element phải chứa **TẤT CẢ** string trong list.

    Visual hint (optional):
    - `image_hint`: CDN URL ảnh user khoanh đỏ element cần click.
      Vision matcher fallback khi text matcher fail. Source of truth runtime.
    - `image_hint_desc`: Mô tả phụ trợ cho vision LLM (vd: "tab download
      chính, KHÔNG phải link Tải về của related document").
    """

    text_any: Optional[list[str]] = None       # OR — match text/label
    text_all: Optional[list[str]] = None       # AND — phải có cả N string
    label_any: Optional[list[str]] = None
    placeholder_any: Optional[list[str]] = None
    role: Optional[str] = None                 # 'button' | 'textbox' | 'link' | ...
    css: Optional[str] = None                  # escape hatch
    nth: int = 0                               # nếu nhiều match, lấy phần tử thứ n

    # Visual hint (Phase 1 Visual Hints — optional)
    image_hint: Optional[str] = None           # CDN URL ảnh khoanh đỏ
    image_hint_desc: Optional[str] = None      # mô tả phụ trợ cho vision LLM

    @model_validator(mode="after")
    def _ensure_any_field(self) -> "TargetSpec":
        # image_hint một mình KHÔNG đủ — vẫn cần text/role để text matcher thử
        # trước khi escalate vision. Tránh trường hợp scenario chỉ có ảnh →
        # mọi step đều gọi vision, đốt cost vô tội vạ.
        if not any([self.text_any, self.text_all, self.label_any,
                    self.placeholder_any, self.role, self.css]):
            raise ValueError(
                "TargetSpec phải có ít nhất 1 trong: "
                "text_any, text_all, label_any, placeholder_any, role, css. "
                "image_hint là optional fallback, không thay thế các field này."
            )
        return self


# ── Condition & rules ─────────────────────────────────────────────────────────

class Condition(BaseModel):
    """Điều kiện — dùng cho success/failure rule và if_visible."""

    url_contains: Optional[str] = None
    text_any: Optional[list[str]] = None
    element_visible: Optional[TargetSpec] = None


class SuccessRule(BaseModel):
    any_of: list[Condition] = Field(default_factory=list)
    all_of: list[Condition] = Field(default_factory=list)


class FailureRule(BaseModel):
    any_of: list[Condition] = Field(default_factory=list)
    all_of: list[Condition] = Field(default_factory=list)
    code: str = "FLOW_FAILED"
    message: str = "Flow failed."


# ── Flow step ──────────────────────────────────────────────────────────────────

class FlowStep(BaseModel):
    """1 bước trong flow. Không phải action nào cũng cần đủ tất cả field.

    Action           | Field cần
    -----------------|--------------------------------------------
    goto             | url
    wait_for         | target (hoặc timeout_ms)
    fill             | target + (value_from hoặc value)
    click            | target
    ask_user         | field + prompt
    if_visible       | target + then (+ else)
    eval_js          | script
    upload_download  | (optional: extensions, remote_dir, timeout_ms)
    """

    action: str
    target: Optional[TargetSpec] = None

    # fill / type
    value_from: Optional[str] = None
    value: Optional[str] = None

    # goto
    url: Optional[str] = None

    # ask_user
    field: Optional[str] = None
    prompt: Optional[str] = None

    # wait_for / generic timeout
    timeout_ms: Optional[int] = None

    # scroll
    direction: Optional[str] = None      # up|down|left|right|top|bottom
    amount: Optional[int] = None         # pixels (None = agent-browser default 300)

    # eval_js — JS code chạy trực tiếp trong page context
    script: Optional[str] = None

    # upload_download — filter file extension + override remote_dir trên CDN
    extensions: Optional[list[str]] = None  # [".doc", ".pdf"] — filter file mới
    remote_dir: Optional[str] = None        # subdir trên CDN bucket

    # upload_html_source — capture HTML source trang hiện tại
    format: Optional[str] = None      # "outer_html" (default) | "inner_html"
    filename: Optional[str] = None    # tên file .html; auto-gen nếu None

    # solve_captcha — đọc CAPTCHA bằng vision LLM (OCR) + điền + verify + retry.
    # `target` (ở trên) không dùng cho action này; các target riêng bên dưới.
    fill_target: Optional[TargetSpec] = None      # ô nhập mã xác minh (BẮT BUỘC)
    submit_target: Optional[TargetSpec] = None    # nút submit để verify captcha
    submit_eval_js: Optional[str] = None          # JS submit (ưu tiên hơn submit_target;
                                                  # dùng khi nút không target được/onclick-only)
    reload_target: Optional[TargetSpec] = None    # link "Thay đổi" để reroll
    reload_eval_js: Optional[str] = None          # JS reroll captcha (ưu tiên hơn reload_target)
    verify_fail_any: Optional[list[str]] = None   # text site hiện khi mã SAI
    verify_success_any: Optional[list[str]] = None  # text báo chắc chắn ĐÚNG (optional)
    max_attempts: int = 4                         # số lần OCR+verify tối đa
    # DEPRECATED (2026-06-25): model OCR do env CAPTCHA_MODEL quyết, YAML KHÔNG đè
    # nữa. 3 field dưới GIỮ để YAML cũ không vỡ khi parse, nhưng solve_captcha BỎ QUA.
    vision_model: Optional[str] = None            # [ignored] dùng env CAPTCHA_MODEL
    vision_model_cheap: Optional[str] = None      # [ignored]
    cheap_attempts: Optional[int] = None          # [ignored]
    crop_selector: Optional[str] = None           # CSS selector ảnh captcha để crop
    captcha_src_selector: Optional[str] = None     # CSS selector <img> captcha — lấy
                                                   # THẲNG src (data URI base64) → OCR ảnh
                                                   # gốc sạch, không cần screenshot/crop.
                                                   # Tốt nhất khi captcha là <img> base64.
    captcha_image_hint: Optional[str] = None      # URL ảnh khoanh đỏ vùng captcha —
                                                  # gửi kèm cho GPT-4o làm tham chiếu vị
                                                  # trí khi auto-crop không cắt được
    fail_code: Optional[str] = None               # solve_captcha: mã lỗi trả về khi
                                                  # OCR fail hết max_attempts (vd
                                                  # "WRONG_CAPTCHA"). None → chuỗi lỗi cũ.
    fail_message: Optional[str] = None            # message đi kèm fail_code
    manual_only: bool = False                     # solve_captcha: chỉ tiêu thụ mã user
                                                  # nhập (context[field]); rỗng → no-op.
                                                  # Dùng cho step sau ask_user.

    # http_login action — HTTP-based SSO chain (Plan B bypass Chrome JS redirect).
    # Worker dùng requests Python POST credentials, follow JS location.href chain
    # bằng regex, lấy cookies, set vào Chrome rồi navigate return_url.
    return_url: Optional[str] = None        # URL Chrome navigate sau khi cookies set
    username_from: Optional[str] = None     # context key (alt nếu khác value_from)
    password_from: Optional[str] = None     # context key cho password (type=secret)
    max_redirects: int = 5                  # số JS location.href chain max follow

    # condition branching (if_visible)
    then: list["FlowStep"] = Field(default_factory=list)
    else_: list["FlowStep"] = Field(default_factory=list, alias="else")

    # retry cho step flaky
    retry: int = 0

    # human-readable reason (hiện lên UI + log)
    note: str = ""

    model_config = {"populate_by_name": True}


FlowStep.model_rebuild()
