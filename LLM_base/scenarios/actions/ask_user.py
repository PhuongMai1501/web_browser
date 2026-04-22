"""Action: ask_user — dừng flow, hỏi user, ghi câu trả lời vào context[field].

Flow runner xử lý logic resume: khi gặp ActionResult(ask_user=True), nó
yield AskRecord, đợi caller gọi gen.send(answer), rồi gán vào context.
"""

from __future__ import annotations

from ..action_registry import ActionResult, action


@action("ask_user")
def run_ask_user(rt, step) -> ActionResult:
    field_name = step.field or ""
    if not field_name:
        return ActionResult(
            ok=False, action_type="ask_user",
            error="step ask_user thiếu 'field'",
        )
    prompt = step.prompt or f"Vui lòng nhập {field_name}"
    return ActionResult(
        ok=True, action_type="ask_user",
        ask_user=True, ask_field=field_name, ask_prompt=prompt,
        reason=prompt,
    )
