"""
services/scenario_generator.py — Sinh YAML scenario từ NL description.

Workflow:
  1. Nhận NL description tiếng Việt từ Sup Agent
  2. Build prompt với system + few-shot QCVN
  3. Gọi OpenAI gpt-4o-mini (config LLM_MODEL)
  4. Strip markdown wrap nếu có, return raw YAML
  5. Caller validate qua yaml_normalizer

Config qua env (reuse `config.py`):
  - OPENAI_API_KEY
  - LLM_MODEL (default "gpt-4o-mini")
  - LLM_TIMEOUT_S (default 60)
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from openai import OpenAI

from config import LLM_MODEL, LLM_TIMEOUT_S

_log = logging.getLogger(__name__)

_FEW_SHOT_FILENAME = "check_law_version_qcvn.yaml"
_MAX_OUTPUT_TOKENS = 4000
_MARKDOWN_FENCE_RE = re.compile(r"^```(?:ya?ml)?\s*\n(.*?)\n```\s*$", re.DOTALL)


@dataclass(frozen=True)
class GenerateResult:
    """Kết quả 1 lần sinh YAML."""

    ok: bool
    yaml: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    error: str = ""


def _load_few_shot() -> str:
    """Đọc scenario QCVN làm few-shot example.

    Search order:
      1. ENV SCENARIO_GEN_FEW_SHOT_PATH (override path tuỳ chỉnh)
      2. services/few_shot/check_law_version_qcvn.yaml — bundle kèm code
         (K8s deploy)
      3. modify_scenarios/thuvienphapluat.vn/check_law_version_qcvn.yaml —
         repo root (chỉ có khi chạy dev local)
      4. fallback: empty (LLM vẫn chạy nhưng kém chất lượng)
    """
    override = os.getenv("SCENARIO_GEN_FEW_SHOT_PATH", "").strip()
    if override:
        p = Path(override)
        if p.exists():
            return p.read_text(encoding="utf-8")
        _log.warning("Few-shot override path missing: %s", override)

    here = Path(__file__).resolve()
    candidates: list[Path] = [here.parent / "few_shot" / _FEW_SHOT_FILENAME]
    # Dev fallback: repo root có folder `modify_scenarios/`. Depth tới repo root
    # khác nhau giữa dev (sâu) và K8s (`/app/agent_browser/...` chỉ 3 parents).
    # Wrap IndexError vì K8s không có depth đủ — chỉ cần few_shot bundle ở trên.
    for depth in (3, 4, 5):
        try:
            candidates.append(
                here.parents[depth] / "modify_scenarios" / "thuvienphapluat.vn" / _FEW_SHOT_FILENAME
            )
        except IndexError:
            break
    for c in candidates:
        if c.exists():
            return c.read_text(encoding="utf-8")

    _log.warning("Few-shot %s không tìm thấy. LLM chạy không ví dụ.", _FEW_SHOT_FILENAME)
    return ""


_SYSTEM_PROMPT_HEADER = """Bạn là chuyên gia sinh YAML scenario cho hệ thống automation browser (tool-web).

OUTPUT FORMAT (BẮT BUỘC):
- Trả về THUẦN YAML, không markdown wrap, không triple-backtick, không kèm giải thích.
- Output phải parse được trực tiếp qua PyYAML.

SCHEMA BẮT BUỘC:
id: <slug ascii_lower, gạch dưới>
display_name: <tên hiển thị>
description: |
  <mô tả ngắn>
enabled: true
mode: flow
start_url: <URL điểm bắt đầu>
allowed_domains:
  - <domain>
max_steps_default: <int ≤ 30>
inputs:
  - name: <field_name>
    type: string | secret
    required: true | false
    source: context
    description: <mô tả>
steps:
  - action: <action_type>
    ...

CÁC ACTION HỢP LỆ:
- click: click element. Cần `target: {role, text_any}`.
- fill: nhập text vào textbox. Cần `target` và `value_from: <input_name>`.
- wait_for: chờ element xuất hiện. Cần `target`, optional `timeout_ms` (default 30000).
- open_link: mở link same-tab. Như click nhưng đảm bảo navigation.
- if_visible: branch theo target có visible không. Có `then: [...]` và `else: [...]`.
- eval_js: chạy JS script. Cần `script: <code>`. Workaround cho inline onclick handler.
- upload_download: poll file download xong, upload lên CDN, xóa local. Optional `extensions: [...]`, `timeout_ms`.

ROLE HỢP LỆ TRONG target:
- button, link, textbox, image, heading

TEXT_ANY:
- List các text NHÌN THẤY được trên UI (tiếng Việt).
- Matcher case-insensitive, partial match.
- Bao gồm nhiều variant nếu site đôi khi đổi text (vd ["Đăng nhập", "Login"]).

INPUTS RULES:
- Field name KHÔNG được match regex `password|pwd|secret|token|api[_-]?key` nếu type=string.
- Nếu type=secret → tool-web tự mask trong log.
- Có thể đặt `default: <value>` để bypass require khi missing context (chỉ test mode).

COMMON PATTERNS:

1. Login form (skip nếu đã login):
   - if_visible với target check link "Trang cá nhân"/"Thoát" (success marker)
   - then: [] (đã login → skip)
   - else: wait_for textbox → fill user → fill password → click submit
            → if_visible popup "Đồng ý" → click nếu có → wait_for success marker

2. Search:
   - fill keyword vào textbox
   - click button submit
   - eval_js force inline onclick nếu site dùng onclick="" handler
   - wait_for kết quả render

3. Download document:
   - click tab "Tải về"
   - eval_js force inline onclick (nhiều site cần)
   - wait_for menu variant render
   - eval_js find link by text rồi .click() (CDP click không trigger inline onclick)
   - upload_download cuối flow để collect file

NHIỆM VỤ:
Đọc mô tả tiếng Việt của user, sinh YAML scenario hoàn chỉnh tuân thủ schema trên.
Học từ ví dụ dưới đây.

VÍ DỤ THAM KHẢO (scenario thuvienphapluat.vn TCVN — KHÔNG copy nguyên xi, chỉ tham khảo pattern):

"""


def _build_system_prompt() -> str:
    few_shot = _load_few_shot()
    if not few_shot:
        return _SYSTEM_PROMPT_HEADER
    return f"{_SYSTEM_PROMPT_HEADER}\n```yaml\n{few_shot}\n```\n"


def _strip_markdown_fence(text: str) -> str:
    """Bỏ ```yaml ... ``` wrap nếu LLM trả markdown thay vì pure YAML."""
    m = _MARKDOWN_FENCE_RE.match(text.strip())
    if m:
        return m.group(1).strip()
    return text.strip()


def generate_yaml(
    description: str,
    site_hint: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> GenerateResult:
    """Sinh YAML scenario từ NL description.

    Returns GenerateResult với ok=True nếu LLM trả response, ok=False nếu lỗi
    API call. Caller (endpoint) chịu trách nhiệm validate YAML qua yaml_normalizer.
    """
    key = api_key or os.getenv("OPENAI_API_KEY", "")
    if not key:
        return GenerateResult(ok=False, error="OPENAI_API_KEY chưa cấu hình")

    desc = (description or "").strip()
    if not desc:
        return GenerateResult(ok=False, error="description rỗng")

    mdl = (model or LLM_MODEL).strip()
    system_prompt = _build_system_prompt()
    user_msg_parts = [f"Mô tả từ user: {desc}"]
    if site_hint:
        user_msg_parts.append(f"Site gợi ý: {site_hint}")
    user_msg = "\n".join(user_msg_parts)

    try:
        client = OpenAI(api_key=key, timeout=LLM_TIMEOUT_S)
        resp = client.chat.completions.create(
            model=mdl,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=_MAX_OUTPUT_TOKENS,
            temperature=0.2,
        )
    except Exception as e:
        _log.warning("scenario_generator OpenAI call failed (%s): %s",
                     type(e).__name__, e)
        return GenerateResult(ok=False, model=mdl,
                              error=f"OpenAI {type(e).__name__}: {e}")

    raw = (resp.choices[0].message.content or "").strip()
    yaml_text = _strip_markdown_fence(raw)
    usage = resp.usage
    return GenerateResult(
        ok=True,
        yaml=yaml_text,
        model=mdl,
        tokens_in=getattr(usage, "prompt_tokens", 0),
        tokens_out=getattr(usage, "completion_tokens", 0),
    )
