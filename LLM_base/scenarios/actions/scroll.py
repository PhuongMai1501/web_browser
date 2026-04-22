"""Action: scroll — cuộn trang để lazy-content render / xem kết quả dài.

Ví dụ YAML:
    - action: scroll
      direction: down        # up|down|left|right|top|bottom
      amount: 500            # optional, px (bỏ qua với top/bottom)

    - action: scroll
      direction: bottom      # cuộn đến cuối trang
"""

from __future__ import annotations

from ..action_registry import ActionResult, action


@action("scroll")
def run_scroll(rt, step) -> ActionResult:
    direction = (step.direction or "down").lower()
    amount = step.amount

    # Gọi browser.scroll_page nếu adapter có support; fallback return error
    if not hasattr(rt.browser, "scroll_page"):
        return ActionResult(
            ok=False, action_type="scroll",
            error="browser adapter không có scroll_page(); cập nhật browser_adapter.py",
        )

    try:
        rt.browser.scroll_page(direction, amount)
    except Exception as e:
        return ActionResult(
            ok=False, action_type="scroll",
            error=f"scroll fail: {e}",
        )

    # Scroll làm content lazy-load → invalidate snapshot cache
    rt.last_snapshot = ""

    amt_text = f" {amount}px" if amount and direction not in ("top", "bottom") else ""
    return ActionResult(
        ok=True, action_type="scroll",
        reason=step.note or f"Scroll {direction}{amt_text}",
    )
