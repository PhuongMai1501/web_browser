"""Action: click — click vào element khớp target."""

from __future__ import annotations

from ..action_registry import ActionResult, action
from ..snapshot_query import describe_target, find_ref


@action("click")
def run_click(rt, step) -> ActionResult:
    if step.target is None:
        return ActionResult(ok=False, action_type="click",
                            error="step click thiếu 'target'")
    snapshot = _ensure_snapshot(rt)
    ref = find_ref(snapshot, step.target)
    if ref is None:
        return ActionResult(
            ok=False, action_type="click",
            error=f"Không tìm thấy element theo target=[{describe_target(step.target)}]",
        )

    url_before = _safe_url(rt.browser)
    try:
        rt.browser.click_element(ref)
    except Exception as e:
        return ActionResult(
            ok=False, action_type="click", ref_used=ref,
            error=f"click_element fail: {e}",
        )
    # Cho page điều hướng nếu có
    try:
        rt.browser.wait_ms(800)
    except Exception:
        pass
    url_after = _safe_url(rt.browser)
    rt.last_snapshot = ""  # invalidate — action tiếp theo phải lấy snapshot mới
    return ActionResult(
        ok=True, action_type="click", ref_used=ref,
        url_before=url_before, url_after=url_after,
        reason=step.note or f"Click {describe_target(step.target)}",
    )


def _ensure_snapshot(rt) -> str:
    if getattr(rt, "last_snapshot", ""):
        return rt.last_snapshot
    try:
        snap = rt.browser.take_snapshot()
    except Exception:
        snap = ""
    rt.last_snapshot = snap
    return snap


def _safe_url(browser) -> str:
    try:
        return browser.get_current_url()
    except Exception:
        return ""
