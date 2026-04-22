"""Action: if_visible — rẽ nhánh theo element có xuất hiện không.

  - target visible → chạy step.then
  - không visible → chạy step.else_ (có thể rỗng)

Runner thực thi các nested step bằng cách tự gọi lại action_registry.
Để giữ file gọn, runner đọc `nested` trong ActionResult và inline.
"""

from __future__ import annotations

from ..action_registry import ActionResult, action
from ..snapshot_query import find_ref


@action("if_visible")
def run_if_visible(rt, step) -> ActionResult:
    if step.target is None:
        return ActionResult(ok=False, action_type="if_visible",
                            error="step if_visible thiếu 'target'")
    try:
        snapshot = rt.browser.take_snapshot()
    except Exception:
        snapshot = ""
    rt.last_snapshot = snapshot
    ref = find_ref(snapshot, step.target)
    branch = step.then if ref else step.else_
    rt._pending_nested = branch
    return ActionResult(
        ok=True, action_type="if_visible",
        ref_used=ref or "",
        reason=(
            f"{'Thấy' if ref else 'Không thấy'} element "
            f"→ chạy {'then' if ref else 'else'} ({len(branch)} step)"
        ),
    )
