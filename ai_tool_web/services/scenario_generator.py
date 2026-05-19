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
- fill: nhập text vào textbox. Cần `target` + CHỌN 1 TRONG 2:
    + value: <literal string>      — text gõ trực tiếp (search keyword, URL,
                                      số hiệu cố định, label hardcoded).
                                      VD: value: "Giá iPhone 15"
    + value_from: <input_name>     — tên 1 input đã declare trong `inputs:`.
                                      VD: value_from: username
  ⚠️ QUAN TRỌNG — sai cú pháp value_from là LỖI THƯỜNG GẶP:
     - value_from CHỈ NHẬN identifier (a-z, _, 0-9) match đúng tên 1 input.
     - KHÔNG dùng value_from cho literal text có space/dấu/tiếng Việt.
     - SAI: `value_from: "Giá iPhone 15"` → worker fail "Context không có field".
     - ĐÚNG: `value: "Giá iPhone 15"` (literal) HOẶC khai báo
       `inputs: [{name: keyword, default: "Giá iPhone 15"}]` rồi `value_from: keyword`.
- wait_for: chờ element xuất hiện. Cần `target`, optional `timeout_ms` (default 30000).
- open_link: mở link same-tab. Như click nhưng đảm bảo navigation.
- if_visible: branch theo target có visible không. Có `then: [...]` và `else: [...]`.
- ask_user: hỏi user runtime. Cần `field: <input_name>` + `prompt: <câu hỏi>`. Worker phát SSE event ask, đợi user POST /resume.
- eval_js: chạy JS script. Cần `script: <code>`. Workaround cho inline onclick handler.
- upload_download: poll file download xong, upload lên CDN, xóa local. Optional `extensions: [...]`, `timeout_ms`.
- extract_data: LLM extract data từ DOM theo top-level `output_schema`. Cần đặt CUỐI flow. Optional `prompt: <instruction>`.

ROLE HỢP LỆ TRONG target:
- button, link, textbox, combobox, searchbox, textarea, image, heading

⚠️ CHỌN ROLE CHO INPUT — nhiều site dùng ARIA role khác nhau cho cùng "ô gõ chữ":
- Google / Bing / YouTube / Facebook search box → `combobox` (vì có autocomplete dropdown)
- HTML5 search input chuẩn (`<input type="search">`) → `searchbox`
- Form input thường (login, register, payment) → `textbox`
- Rich editor (Gmail compose body, Twitter tweet box, Notion block) → `textbox`
  (vì là `<div contenteditable>` nên ARIA tree expose là textbox)
- `<textarea>` HTML → `textarea`

KHI KHÔNG CHẮC: dùng `textbox` mặc định — worker có fallback tự động thử
combobox / searchbox / textarea nếu textbox không match. Tuy nhiên đoán
đúng role ngay từ đầu giúp giảm latency 1 vòng fallback.

CHEAT SHEET site phổ biến:
| Site/Loại                          | Role chuẩn  |
|------------------------------------|-------------|
| Google.com / Bing.com search       | combobox    |
| YouTube search                     | combobox    |
| Facebook / Twitter search          | combobox    |
| HTML5 `<input type="search">`      | searchbox   |
| Login form (email, password)       | textbox     |
| Form đăng ký, thanh toán           | textbox     |
| Gmail compose body, chat editor    | textbox     |
| `<textarea>` comment box           | textarea    |

TEXT_ANY:
- List các text NHÌN THẤY được trên UI (tiếng Việt).
- Matcher case-insensitive, partial match.
- Bao gồm nhiều variant nếu site đôi khi đổi text (vd ["Đăng nhập", "Login"]).

⚠️ KHÔNG ĐOÁN TEXT GENERIC KHÔNG TỒN TẠI TRÊN DOM:
- Báo điện tử (vnexpress, dantri, tuoitre...) KHÔNG có element với label
  "Tin nổi bật" / "Bài viết nổi bật" / "Bài hot". Card bài chỉ chứa title
  thật của bài (vd "Hà Nội mở rộng quốc lộ 1 lên 16 làn xe").
- Khi user nói "bài top" / "bài nổi bật" / "bài đầu tiên" → DÙNG vị trí
  (nth=0 trên role=heading level=3) thay vì text_any đoán mò.
- Khi user nói "click vào bài X cụ thể" → mới dùng text_any=[<title bài X>].

SITE CHEAT SHEET — selector pattern thực tế (Vietnamese news sites):

| Site                | "Bài top trên home"          | "Title bài chi tiết" | "Sapo/tóm tắt" |
|---------------------|------------------------------|----------------------|----------------|
| vnexpress.net       | role=heading level=3 nth=0   | heading level=1      | paragraph đầu  |
| dantri.com.vn       | role=heading level=3 nth=0   | heading level=1      | paragraph đầu  |
| tuoitre.vn          | role=heading level=3 nth=0   | heading level=1      | paragraph đầu  |
| thanhnien.vn        | role=heading level=3 nth=0   | heading level=1      | paragraph đầu  |
| zingnews.vn         | role=heading level=2 nth=0   | heading level=1      | paragraph đầu  |
| vietnamnet.vn       | role=heading level=3 nth=0   | heading level=1      | paragraph đầu  |

⚠️ CHỌN `click` HAY `open_link` (CỰC KỲ QUAN TRỌNG):
- `click`: support đầy đủ target (role, level, nth, text_any). Dùng khi
  selector dựa trên VỊ TRÍ/ROLE (vd "heading h3 đầu tiên").
- `open_link`: BẮT BUỘC phải có target.text_any/text_all (logic internal
  tìm <a> theo innerText qua JS eval). Dùng khi user chỉ định TEXT CỤ THỂ
  của link (vd "Tải bản tiếng Việt", "Đăng nhập").
- ❌ KHÔNG dùng open_link với role+nth không kèm text_any → action raise
  "open_link hiện chỉ support target.text_any / text_all".

VÍ DỤ ĐÚNG cho "lấy bài top vnexpress":
```yaml
- action: wait_for
  target: { role: heading, level: 3 }
  timeout_ms: 30000
- action: click              # ← click (không phải open_link) vì dùng role+nth
  target: { role: heading, level: 3, nth: 0 }
- action: wait_for
  target: { role: heading, level: 1 }   # trang chi tiết
- action: extract_data
  prompt: "Đọc bài và trả về title (h1), summary (sapo), published_time"
```

VÍ DỤ SAI 1 (đoán mò text không có trên DOM):
```yaml
- action: wait_for
  target: { role: link, text_any: ["Tin nổi bật", "Bài hot"] }  # ❌ KHÔNG tồn tại
```

VÍ DỤ SAI 2 (open_link thiếu text_any):
```yaml
- action: open_link
  target: { role: heading, level: 3, nth: 0 }  # ❌ open_link bắt buộc text_any
```

INPUTS RULES:
- Field name KHÔNG được match regex `password|pwd|secret|token|api[_-]?key` nếu type=string.
- Nếu type=secret → tool-web tự mask trong log.
- Có thể đặt `default: <value>` để bypass require khi missing context (chỉ test mode).

OUTPUT JSON ĐỘNG (output_schema + action extract_data):

Khi user yêu cầu output có FORMAT cụ thể (vd "trả về JSON với tên, giá, web bán"),
PHẢI:

1. Thêm top-level field `output_schema` (JSON Schema chuẩn) NGAY SAU `max_steps_default`:

   ```yaml
   output_schema:
     type: object
     properties:
       name:    { type: string, description: "Tên sản phẩm" }
       price:   { type: string, description: "Giá kèm đơn vị tiền tệ" }
       seller:  { type: string, description: "Tên trang web bán" }
       url:     { type: string, description: "Link trang sản phẩm" }
     required: [name, price, seller, url]
     additionalProperties: false
   ```

   QUAN TRỌNG:
   - `required` phải liệt kê TẤT CẢ property keys (OpenAI strict mode).
   - `additionalProperties: false` (OpenAI strict mode).
   - Mỗi property phải có `description` rõ ràng — LLM extract sẽ dùng làm hint.
   - Type cơ bản: string, number, integer, boolean, array, object.
     Nested object phải có `additionalProperties: false` + `required` đầy đủ.

2. Thêm step `action: extract_data` ở CUỐI flow (sau khi đã navigate tới trang
   có data cần lấy):

   ```yaml
   - action: extract_data
     prompt: "Đọc trang sản phẩm này và trả về thông tin theo schema"
     note: "Extract product info"
   ```

   `prompt` (optional): hướng dẫn cụ thể cho LLM extract. Nếu thiếu,
   dùng default "Đọc nội dung trang và extract theo schema".

KHI NÀO BỎ output_schema + extract_data:
- User KHÔNG yêu cầu output JSON cụ thể.
- Task chỉ là thao tác (click, fill, login) — không cần lấy data về.

COMMON PATTERNS:

⚠️ KHI NÀO CẦN LOGIN — KHÔNG MẶC ĐỊNH THÊM LOGIN FLOW:
- User nói "đăng nhập <site>": chỉ thêm login nếu trang đó BẮT BUỘC login để
  truy cập tính năng (vd: thuvienphapluat đọc full text, chang.fpt.net dashboard).
- Search engine (Google, Bing, DuckDuckGo, Cốc Cốc): KHÔNG cần login.
- Trang công khai (Wikipedia, báo điện tử, e-commerce product page): KHÔNG cần login.
- Câu "đăng nhập google" trong NL của user thường nghĩa "truy cập google" —
  KHÔNG phải đăng nhập Google Account. Tránh tự ý thêm form login Gmail.
- Khi không chắc → BỎ login flow + KHÔNG khai báo inputs username/password.
  Để user explicit yêu cầu login mới thêm.

1. Login form (CHỈ khi user yêu cầu rõ HOẶC site bắt buộc):
   - if_visible với target check link "Trang cá nhân"/"Thoát" (success marker — đã login)
   - then: [] (đã login → skip)
   - else: wait_for textbox → fill user → fill password → click submit
            → if_visible popup "Đồng ý" → click nếu có → wait_for success marker
   - inputs: khai báo username (string), password (secret), source=context.

2. Search engine (Google, Bing, ...) — KHÔNG login, KHÔNG inputs credentials:
   - fill ô search với `value: "<keyword>"` (literal, KHÔNG value_from).
     Hoặc khai báo `inputs: [{name: keyword, default: "<keyword>"}]` rồi `value_from: keyword`.
   - click button submit ("Tìm kiếm", "Google Search", "Tìm trên Google").
     Hoặc gửi Enter (chỉ cần fill rồi wait_for kết quả).
   - wait_for kết quả render (heading "Kết quả tìm kiếm" / link tới site).
   - open_link nth=0 (link đầu tiên, organic result).
   - wait_for trang đích render (heading sản phẩm/bài viết).
   - extract_data nếu user yêu cầu JSON output.

   VÍ DỤ:
     ```yaml
     id: search_iphone_price
     mode: flow
     start_url: https://www.google.com
     allowed_domains: [google.com]
     # KHÔNG khai báo inputs username/password ở đây
     output_schema: {...}
     steps:
       - action: fill
         target: { role: combobox, text_any: ["Tìm kiếm"] }
         value: "Giá iPhone 15"             # ← literal, dùng `value`
       - action: click
         target: { role: button, text_any: ["Tìm trên Google", "Google Search"] }
       - action: wait_for
         target: { role: heading, text_any: ["Kết quả"] }
       - action: open_link
         target: { role: link, nth: 0 }
       - action: wait_for
         target: { role: heading }
       - action: extract_data
         prompt: "Đọc trang sản phẩm và trả về thông tin"
     ```

3. Form site có login bắt buộc + search:
   - Login flow (xem pattern 1)
   - Sau wait_for success marker mới fill keyword search.

4. Download document:
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
