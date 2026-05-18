"""Action: extract_data — LLM extract JSON từ DOM hiện tại theo output_schema.

Use case: cuối flow scenario, sau khi đã navigate đến trang đích, cần lấy ra
thông tin structured (vd giá sản phẩm, tên, link...). Action này:

1. Lấy DOM snapshot hiện tại (text-only, prune script/style/SVG)
2. Gọi OpenAI structured-output API với schema từ spec.output_schema
3. Lưu kết quả vào rt.context["__extracted_data"]
4. Worker cuối session merge data này vào result.json["data"]

YAML schema:

```yaml
output_schema:                  # Top-level — JSON Schema
  type: object
  properties:
    name: { type: string, description: "Tên sản phẩm" }
    price: { type: string, description: "Giá tiền" }
    seller: { type: string, description: "Website bán" }
  required: [name, price]
  additionalProperties: false   # OpenAI strict mode yêu cầu

steps:
  # ... navigate steps ...
  - action: extract_data
    prompt: "Đọc trang sản phẩm và trả về thông tin"
```

`prompt` (optional): instruction cụ thể cho LLM. Nếu rỗng → dùng default.
"""

from __future__ import annotations

import json
import logging
import os

from openai import OpenAI

from ..action_registry import ActionResult, action

_log = logging.getLogger(__name__)

# Limit DOM snapshot truyền lên LLM để giảm token cost. Page lớn có thể >100KB.
_MAX_SNAPSHOT_CHARS = 24000
# Storage key trong rt.context — worker đọc khi persist artifacts.
_CONTEXT_KEY = "__extracted_data"
_DEFAULT_PROMPT = (
    "Đọc nội dung trang web bên dưới và extract data theo JSON schema "
    "đã cung cấp. Nếu thiếu thông tin, để chuỗi rỗng cho field optional. "
    "Trả về CHỈ JSON, không kèm giải thích."
)


@action("extract_data")
def run_extract_data(rt, step) -> ActionResult:
    """Lấy snapshot DOM, gọi LLM extract JSON theo spec.output_schema."""
    schema = getattr(rt.spec, "output_schema", None) or {}
    if not schema:
        return ActionResult(
            ok=False, action_type="extract_data",
            error=(
                "Spec thiếu top-level `output_schema`. Khai báo JSON Schema "
                "ở scenario root rồi mới dùng action extract_data."
            ),
        )

    api_key = rt.api_key or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return ActionResult(
            ok=False, action_type="extract_data",
            error="OPENAI_API_KEY chưa cấu hình trong worker",
        )

    # Snapshot từ rt.last_snapshot (cached) hoặc fresh từ browser. Snapshot
    # đã được flow_runner prune script/style trước khi cache.
    snapshot = rt.last_snapshot or ""
    if not snapshot:
        try:
            snapshot = rt.browser.snapshot() or ""
            rt.last_snapshot = snapshot
        except Exception as e:
            return ActionResult(
                ok=False, action_type="extract_data",
                error=f"snapshot fail: {e}",
            )

    # Truncate nếu quá dài để tránh token explosion. LLM thường cần text đầu
    # tiên nhiều hơn — keep prefix.
    if len(snapshot) > _MAX_SNAPSHOT_CHARS:
        snapshot = snapshot[:_MAX_SNAPSHOT_CHARS] + "\n...(truncated)"

    user_prompt = (step.prompt or "").strip() or _DEFAULT_PROMPT
    url_before = _safe_url(rt.browser)

    try:
        client = OpenAI(api_key=api_key, timeout=30)
        # Dùng response_format=json_schema để OpenAI enforce schema. Strict
        # mode yêu cầu schema phải có `additionalProperties: false` và mọi
        # property phải nằm trong `required`. Wrap để tự fill nếu user gen
        # schema thiếu.
        strict_schema = _ensure_strict_schema(schema)
        resp = client.chat.completions.create(
            model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Bạn là chuyên gia extract data từ HTML/text. "
                        "Trả về JSON ĐÚNG SCHEMA. Không suy đoán field không có "
                        "trong nội dung — để chuỗi rỗng."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"{user_prompt}\n\n"
                        f"URL: {url_before}\n\n"
                        f"Nội dung trang:\n{snapshot}"
                    ),
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "extracted_data",
                    "schema": strict_schema,
                    "strict": True,
                },
            },
            temperature=0.1,
            max_tokens=1500,
        )
    except Exception as e:
        _log.warning(
            "[%s] extract_data OpenAI fail (%s): %s",
            rt.session_id, type(e).__name__, e,
        )
        return ActionResult(
            ok=False, action_type="extract_data",
            error=f"OpenAI {type(e).__name__}: {e}",
            url_before=url_before,
        )

    raw = (resp.choices[0].message.content or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return ActionResult(
            ok=False, action_type="extract_data",
            error=f"LLM trả JSON không hợp lệ: {e}. Raw: {raw[:200]}",
            url_before=url_before,
        )

    # Merge vào context (debug) + output_holder (worker đọc sau khi gen close).
    # output_holder None = caller không truyền → action vẫn ok, chỉ log warning.
    rt.context[_CONTEXT_KEY] = data
    if rt.output_holder is not None:
        rt.output_holder["data"] = data
    else:
        _log.warning(
            "[%s] extract_data: rt.output_holder=None → data sẽ KHÔNG được "
            "ghi vào result.json. Caller cần truyền output_holder vào run_flow.",
            rt.session_id,
        )
    _log.info(
        "[%s] extract_data ok — %d fields, ~%d tokens out",
        rt.session_id, len(data) if isinstance(data, dict) else 0,
        getattr(resp.usage, "completion_tokens", 0),
    )

    return ActionResult(
        ok=True, action_type="extract_data",
        url_before=url_before, url_after=url_before,
        reason=step.note or f"Extracted {len(data) if isinstance(data, dict) else 1} fields",
    )


def _ensure_strict_schema(schema: dict) -> dict:
    """OpenAI strict mode yêu cầu schema chuẩn hoá. Auto-fill nếu thiếu.

    - Top-level `type: object` (set nếu chưa có)
    - `additionalProperties: false` ở mọi object
    - Tất cả properties phải trong `required`

    LLM tự gen schema thường thiếu các field này → strict call fail. Wrap để
    tránh phải tune prompt phía API.
    """
    if not isinstance(schema, dict):
        return schema

    if "type" not in schema:
        schema = {**schema, "type": "object"}

    if schema.get("type") == "object":
        props = schema.get("properties") or {}
        schema = {
            **schema,
            "additionalProperties": False,
            "required": list(props.keys()),
        }
        # Recurse vào nested object properties
        new_props = {}
        for k, v in props.items():
            if isinstance(v, dict) and v.get("type") == "object":
                new_props[k] = _ensure_strict_schema(v)
            elif isinstance(v, dict) and v.get("type") == "array":
                items = v.get("items")
                if isinstance(items, dict) and items.get("type") == "object":
                    new_props[k] = {**v, "items": _ensure_strict_schema(items)}
                else:
                    new_props[k] = v
            else:
                new_props[k] = v
        schema["properties"] = new_props

    return schema


def _safe_url(browser) -> str:
    try:
        return browser.get_current_url()
    except Exception:
        return ""
