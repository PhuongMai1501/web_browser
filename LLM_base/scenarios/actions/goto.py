"""Action: goto — mở URL trong browser."""

from __future__ import annotations

from ..action_registry import ActionResult, action


@action("goto")
def run_goto(rt, step) -> ActionResult:
    url = step.url or ""
    if not url:
        return ActionResult(ok=False, error="step goto thiếu field 'url'",
                            action_type="goto")
    url_before = _safe_url(rt.browser)
    try:
        rt.browser.open_url(url)
    except Exception as e:
        return ActionResult(ok=False, error=f"open_url fail: {e}",
                            action_type="goto", url_before=url_before)
    # Cho trang load 1 nhịp — admin có thể thêm wait_for ngay sau.
    try:
        rt.browser.wait_ms(1500)
    except Exception:
        pass
    url_after = _safe_url(rt.browser)
    return ActionResult(
        ok=True, action_type="goto",
        url_before=url_before, url_after=url_after,
        reason=step.note or f"Mở {url}",
    )


def _safe_url(browser) -> str:
    try:
        return browser.get_current_url()
    except Exception:
        return ""
