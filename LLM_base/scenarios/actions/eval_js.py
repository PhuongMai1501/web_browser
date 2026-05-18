"""Action: eval_js — chạy JavaScript code trong page context.

Use case chính: site dùng inline `onclick` handler hoặc framework JS-only
(không có URL nav) → CDP click không trigger handler → cần force JS exec
trực tiếp.

Ví dụ scenario YAML:

```yaml
- action: eval_js
  script: "document.getElementById('aTieuChuanVN').click()"
  note: "Force trigger inline onclick S_TCVN()"
```

Hoặc gọi function trong page scope:

```yaml
- action: eval_js
  script: "S_TCVN(MemberGA)"
```

Trả về stdout của agent-browser (thường là `undefined` cho code không return,
hoặc JSON-encoded value). Caller chỉ cần kiểm tra `ok=True`.
"""

from __future__ import annotations

from ..action_registry import ActionResult, action


@action("eval_js")
def run_eval_js(rt, step) -> ActionResult:
    script = (step.script or "").strip()
    if not script:
        return ActionResult(
            ok=False, action_type="eval_js",
            error="step eval_js thiếu field 'script'",
        )

    url_before = _safe_url(rt.browser)
    try:
        rt.browser.eval_js(script)
    except Exception as e:
        return ActionResult(
            ok=False, action_type="eval_js",
            error=f"eval_js fail: {e}",
            url_before=url_before,
        )

    # Cho JS handler chạy + có thể trigger AJAX/DOM update.
    try:
        rt.browser.wait_ms(800)
    except Exception:
        pass

    url_after = _safe_url(rt.browser)
    rt.last_snapshot = ""  # invalidate snapshot — DOM có thể đã đổi
    return ActionResult(
        ok=True, action_type="eval_js",
        url_before=url_before, url_after=url_after,
        reason=step.note or f"Run JS: {script[:60]}",
    )


def _safe_url(browser) -> str:
    try:
        return browser.get_current_url()
    except Exception:
        return ""
