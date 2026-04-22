"""Action: fill — điền text vào input khớp target.

Value lấy từ `value` (literal) hoặc `value_from` (tên InputField).
Nếu InputField có type=secret, log sẽ mask thành `***`.
"""

from __future__ import annotations

from ..action_registry import ActionResult, action
from ..snapshot_query import describe_target, find_ref


@action("fill")
def run_fill(rt, step) -> ActionResult:
    if step.target is None:
        return ActionResult(ok=False, action_type="fill",
                            error="step fill thiếu 'target'")

    # Resolve value
    value = ""
    field_name = ""
    if step.value is not None:
        value = str(step.value)
    elif step.value_from:
        field_name = step.value_from
        if field_name not in rt.context:
            return ActionResult(
                ok=False, action_type="fill",
                error=f"Context không có field '{field_name}' cho value_from",
            )
        value = str(rt.context[field_name] or "")
    else:
        return ActionResult(
            ok=False, action_type="fill",
            error="step fill cần 'value' hoặc 'value_from'",
        )

    # Snapshot + tìm ref
    snapshot = _ensure_snapshot(rt)
    ref = find_ref(snapshot, step.target)
    if ref is None:
        return ActionResult(
            ok=False, action_type="fill",
            error=f"Không tìm thấy element theo target=[{describe_target(step.target)}]",
        )

    # Điền
    try:
        rt.browser.type_text(ref, value)
    except Exception as e:
        return ActionResult(
            ok=False, action_type="fill", ref_used=ref,
            error=f"type_text fail: {e}",
        )

    # Mask nếu secret
    is_secret = rt.is_secret_field(field_name) if field_name else False
    shown = "***" if is_secret else value
    return ActionResult(
        ok=True, action_type="fill", ref_used=ref,
        text_typed=shown,
        reason=(
            step.note
            or f"Điền {'(secret) ' if is_secret else ''}vào {describe_target(step.target)}"
        ),
    )


def _ensure_snapshot(rt) -> str:
    """Reuse snapshot nếu wait_for vừa lấy, nếu không chụp mới."""
    if getattr(rt, "last_snapshot", ""):
        return rt.last_snapshot
    try:
        snap = rt.browser.take_snapshot()
    except Exception:
        snap = ""
    rt.last_snapshot = snap
    return snap
