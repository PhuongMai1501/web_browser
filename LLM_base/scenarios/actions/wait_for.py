"""Action: wait_for — đợi cho đến khi element (theo target) xuất hiện trong snapshot,
hoặc timeout. Hỗ trợ `target=None` → chỉ sleep `timeout_ms`.
"""

from __future__ import annotations

import time

from ..action_registry import ActionResult, action
from ..snapshot_query import describe_target, find_ref


DEFAULT_WAIT_MS = 5000
POLL_MS = 500


@action("wait_for")
def run_wait_for(rt, step) -> ActionResult:
    timeout_ms = step.timeout_ms or DEFAULT_WAIT_MS

    # Trường hợp không có target: chỉ sleep
    if step.target is None:
        try:
            rt.browser.wait_ms(timeout_ms)
        except Exception:
            pass
        return ActionResult(ok=True, action_type="wait_for",
                            reason=f"Wait {timeout_ms}ms")

    deadline = time.time() + (timeout_ms / 1000.0)
    last_snapshot = ""
    while time.time() < deadline:
        try:
            last_snapshot = rt.browser.take_snapshot()
        except Exception:
            last_snapshot = ""
        ref = find_ref(last_snapshot, step.target)
        if ref:
            rt.last_snapshot = last_snapshot
            return ActionResult(
                ok=True, action_type="wait_for", ref_used=ref,
                reason=step.note or f"Đợi thấy {describe_target(step.target)}",
            )
        try:
            rt.browser.wait_ms(POLL_MS)
        except Exception:
            time.sleep(POLL_MS / 1000.0)

    rt.last_snapshot = last_snapshot
    return ActionResult(
        ok=False, action_type="wait_for",
        error=(
            f"Hết {timeout_ms}ms chưa thấy element theo "
            f"target=[{describe_target(step.target)}]"
        ),
    )
