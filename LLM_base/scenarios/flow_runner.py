"""
scenarios/flow_runner.py — Runner cho mode=flow.

Thực thi tuần tự `spec.steps`, emit StepRecord (tương thích với job_handler
hiện tại), xử lý ask_user pause/resume, kiểm tra success/failure sau mỗi step.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Generator, Iterable, Optional

import browser_adapter as browser_module
import llm_planner as planner_module
from state import ARTIFACTS_DIR, StepRecord

from .action_registry import ActionResult, get_action
from .actions import *  # noqa: F401,F403  — trigger action registration
from .flow_models import Condition, FlowStep, SuccessRule, TargetSpec
from .snapshot_query import find_ref


def _resolve_placeholders(value, ctx: dict):
    """Expand `{var}` / `{var.subkey}` trong string/list string bằng ctx.
    Không throw — placeholder thiếu để nguyên (giúp debug dễ hơn)."""
    if value is None:
        return None
    if isinstance(value, str):
        if "{" not in value or "}" not in value:
            return value
        try:
            return value.format_map(_SafeCtx(ctx))
        except Exception:
            return value
    if isinstance(value, list):
        return [_resolve_placeholders(v, ctx) for v in value]
    return value


class _SafeCtx(dict):
    """format_map helper: missing key → giữ nguyên `{key}`."""

    def __missing__(self, key):
        return "{" + key + "}"


def _resolve_target(target: Optional[TargetSpec], ctx: dict) -> Optional[TargetSpec]:
    if target is None:
        return None
    data = target.model_dump()
    for k in ("text_any", "text_all", "label_any", "placeholder_any"):
        data[k] = _resolve_placeholders(data.get(k), ctx)
    data["css"] = _resolve_placeholders(data.get("css"), ctx)
    return TargetSpec(**{k: v for k, v in data.items() if v is not None})


def _resolve_step(step: FlowStep, ctx: dict) -> FlowStep:
    """Tạo FlowStep copy với target/value/url/prompt đã thay `{var}`."""
    data = step.model_dump(by_alias=False)
    data["target"] = None  # set lại bên dưới
    data["value"] = _resolve_placeholders(step.value, ctx)
    data["url"] = _resolve_placeholders(step.url, ctx)
    data["prompt"] = _resolve_placeholders(step.prompt, ctx)
    # `then`/`else_` giữ nguyên — sẽ resolve khi execute
    resolved_target = _resolve_target(step.target, ctx)
    step_copy = FlowStep.model_validate({**data, "else": data.pop("else_", [])})
    step_copy.target = resolved_target
    return step_copy


_log = logging.getLogger(__name__)


def _make_run_dir() -> Path:
    """Tạo thư mục artifacts/YYYY/MM/DD/HH_MM_SS/ cho screenshot flow mode."""
    now = datetime.now()
    d = (ARTIFACTS_DIR
         / now.strftime("%Y") / now.strftime("%m")
         / now.strftime("%d") / now.strftime("%H_%M_%S"))
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Runtime ────────────────────────────────────────────────────────────────────

@dataclass
class FlowRuntime:
    browser: Any
    spec: Any
    context: dict
    session_id: str = ""
    last_snapshot: str = ""
    step_count: int = 0
    run_dir: Optional[Path] = None
    _secret_fields: set[str] = field(default_factory=set)
    _pending_nested: Optional[list] = None   # set bởi if_visible

    def is_secret_field(self, name: str) -> bool:
        return name in self._secret_fields


def _capture_step_artifacts(rt: FlowRuntime, step_num: int) -> tuple[str, str, str]:
    """Chụp screenshot + annotated + snapshot cho 1 step (flow mode).

    Returns (screenshot_path, annotated_path, snapshot_text). Mọi lỗi được
    swallow — record vẫn emit được với field rỗng.
    """
    if rt.run_dir is None:
        return "", "", rt.last_snapshot

    screenshot_path = ""
    annotated_path = ""
    snapshot_text = rt.last_snapshot or ""

    try:
        snapshot_text = rt.browser.take_snapshot() or snapshot_text
        rt.last_snapshot = snapshot_text
    except Exception as e:
        _log.debug("flow: take_snapshot failed step %d: %s", step_num, e)

    # full_page=True để thấy toàn bộ danh sách kết quả / trang dài,
    # không chỉ viewport.
    try:
        _, screenshot_path = rt.browser.take_screenshot(
            save_path=str(rt.run_dir / f"step_{step_num:02d}.png"),
            full_page=True,
        )
    except TypeError:
        # Fallback cho fake browser trong unit test không có param full_page
        try:
            _, screenshot_path = rt.browser.take_screenshot(
                save_path=str(rt.run_dir / f"step_{step_num:02d}.png")
            )
        except Exception as e:
            _log.debug("flow: take_screenshot failed step %d: %s", step_num, e)
    except Exception as e:
        _log.debug("flow: take_screenshot failed step %d: %s", step_num, e)

    try:
        _, annotated_path = rt.browser.take_annotated_screenshot(
            save_path=str(rt.run_dir / f"step_{step_num:02d}_annotated.png"),
            full_page=True,
        )
    except TypeError:
        try:
            _, annotated_path = rt.browser.take_annotated_screenshot(
                save_path=str(rt.run_dir / f"step_{step_num:02d}_annotated.png")
            )
        except Exception as e:
            _log.debug("flow: take_annotated_screenshot failed step %d: %s", step_num, e)
    except Exception as e:
        _log.debug("flow: take_annotated_screenshot failed step %d: %s", step_num, e)

    return screenshot_path, annotated_path, snapshot_text


def _build_secret_set(spec) -> set[str]:
    """Các field có type='secret' trong spec.inputs."""
    out = set()
    for inp in getattr(spec, "inputs", []) or []:
        if inp.type == "secret":
            out.add(inp.name)
    return out


# ── Condition check ───────────────────────────────────────────────────────────

def _eval_condition(cond: Condition, rt: FlowRuntime) -> bool:
    if cond.url_contains:
        try:
            url = rt.browser.get_current_url() or ""
        except Exception:
            url = ""
        if cond.url_contains not in url:
            return False
    if cond.text_any:
        try:
            if not rt.browser.page_contains_any(tuple(cond.text_any)):
                return False
        except Exception:
            return False
    if cond.element_visible:
        try:
            snap = rt.browser.take_snapshot()
        except Exception:
            return False
        rt.last_snapshot = snap
        if find_ref(snap, cond.element_visible) is None:
            return False
    return True


def _check_rule(rule, rt: FlowRuntime) -> bool:
    """Return True nếu rule đạt. None rule → False."""
    if rule is None:
        return False
    if rule.all_of and not all(_eval_condition(c, rt) for c in rule.all_of):
        return False
    if rule.any_of and not any(_eval_condition(c, rt) for c in rule.any_of):
        return False
    # Nếu cả 2 list đều rỗng → rule không ý nghĩa, trả False
    if not rule.all_of and not rule.any_of:
        return False
    return True


# ── Record helpers ────────────────────────────────────────────────────────────

def _make_record(
    step_num: int, goal: str, result: ActionResult,
    snapshot: str, screenshot_path: str = "", annotated_path: str = "",
) -> StepRecord:
    action_payload = {
        "action": _translate_action(result),
        "ref": result.ref_used,
        "text": result.text_typed,
        "reason": result.reason,
        "flow_action": result.action_type,   # giữ nguyên để debug
    }
    if result.ask_user:
        action_payload["ask_type"] = "question"
        action_payload["message"] = result.ask_prompt
    return StepRecord(
        step=step_num,
        goal=goal,
        snapshot=snapshot,
        screenshot_path=screenshot_path,
        screenshot_b64="",                       # base64 không cần — path đủ để upload
        annotated_screenshot_b64="",
        annotated_screenshot_path=annotated_path,
        action=action_payload,
        url_before=result.url_before,
        url_after=result.url_after,
        error=result.error if not result.ok else "",
        is_blocked=result.ask_user,
    )


def _translate_action(result: ActionResult) -> str:
    """Map tên action v2 → action name cũ (click/type/wait/ask/done)
    để event SSE giữ shape hiện tại."""
    a = result.action_type
    if result.ask_user:
        return "ask"
    if a in ("click", "if_visible"):
        return "click" if a == "click" else "wait"  # if_visible log như wait
    if a == "fill":
        return "type"
    if a == "wait_for":
        return "wait"
    if a == "goto":
        return "wait"   # goto không có action cũ tương đương — dùng wait
    return a or "wait"


def _make_done_record(step_num: int, goal: str, message: str, rt: FlowRuntime) -> StepRecord:
    url_after = ""
    try:
        url_after = rt.browser.get_current_url()
    except Exception:
        pass
    ss_path, ann_path, snap = _capture_step_artifacts(rt, step_num)
    return StepRecord(
        step=step_num, goal=goal,
        snapshot=snap,
        screenshot_path=ss_path, screenshot_b64="",
        annotated_screenshot_b64="", annotated_screenshot_path=ann_path,
        action={"action": "done", "message": message},
        url_after=url_after,
    )


def _make_failed_record(step_num: int, goal: str, error: str, rt: Optional[FlowRuntime] = None) -> StepRecord:
    ss_path = ann_path = ""
    snap = ""
    if rt is not None:
        ss_path, ann_path, snap = _capture_step_artifacts(rt, step_num)
    return StepRecord(
        step=step_num, goal=goal, snapshot=snap,
        screenshot_path=ss_path, screenshot_b64="",
        annotated_screenshot_b64="", annotated_screenshot_path=ann_path,
        action={"action": "done", "message": error},
        error=error,
    )


# ── Main generator ────────────────────────────────────────────────────────────

def run_flow(
    spec,
    context: Optional[dict],
    session_id: str = "",
    browser=None,
) -> Generator[StepRecord, Optional[str], None]:
    """Generator thực thi flow. Tương thích protocol với `run_agent_autonomous`:
    caller làm `gen.send(answer)` khi nhận record `is_blocked=True`.
    """
    rt = FlowRuntime(
        browser=browser or browser_module,
        spec=spec,
        context=dict(context or {}),
        session_id=session_id,
        run_dir=_make_run_dir(),
        _secret_fields=_build_secret_set(spec),
    )

    step_num = 0
    yielded_terminal = False
    all_records: list[StepRecord] = []   # để ghi session.json cuối run

    # Bật trace — flow mode chủ yếu log browser CLI (LLM trace sẽ rỗng
    # trừ khi có hook LLM trong tương lai, vẫn ghi file cho đồng nhất).
    # hasattr() guard để fake browser trong unit test không cần implement.
    if hasattr(rt.browser, "start_trace"):
        rt.browser.start_trace()
        rt.browser.set_trace_step(0)
    planner_module.start_trace()
    planner_module.set_trace_step(0)

    def _flush_artifacts():
        # Trace
        try:
            b_trace = rt.browser.stop_trace() if hasattr(rt.browser, "stop_trace") else []
            (rt.run_dir / "browser_trace.json").write_text(
                json.dumps(b_trace, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            _log.warning("flow: flush browser_trace fail: %s", e)
        try:
            l_trace = planner_module.stop_trace()
            (rt.run_dir / "llm_trace.json").write_text(
                json.dumps(l_trace, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            _log.warning("flow: flush llm_trace fail: %s", e)
        # session.json cho flow mode — structure giống agent mode để tool đọc chung
        try:
            payload = {
                "session_id": rt.session_id,
                "scenario_id": getattr(spec, "id", ""),
                "mode": getattr(spec, "mode", "flow"),
                "goal": spec.goal or spec.display_name,
                "finished_at": datetime.now().isoformat(),
                "total_steps": len(all_records),
                "steps": [
                    {k: v for k, v in asdict(r).items()
                     if k not in ("screenshot_b64", "annotated_screenshot_b64")}
                    for r in all_records
                ],
            }
            (rt.run_dir / "session.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            _log.warning("flow: flush session.json fail: %s", e)

    def _run_steps(steps: Iterable[FlowStep]):
        nonlocal step_num, yielded_terminal
        for step in steps:
            if yielded_terminal:
                return
            step_num += 1
            rt.browser.set_trace_step(step_num) if hasattr(rt.browser, "set_trace_step") else None
            planner_module.set_trace_step(step_num)
            attempts = max(1, 1 + (step.retry or 0))
            last_result: Optional[ActionResult] = None
            # Resolve `{var}` placeholders trong step theo context runtime
            # (context có thể thay đổi sau ask_user).
            resolved_step = _resolve_step(step, rt.context)
            for attempt in range(attempts):
                fn = get_action(resolved_step.action)
                try:
                    result = fn(rt, resolved_step)
                except Exception as e:
                    result = ActionResult(
                        ok=False, action_type=resolved_step.action,
                        error=f"exception: {e}",
                    )
                last_result = result
                if result.ok or result.ask_user:
                    break
                # Retry — invalidate snapshot để lần sau chụp mới
                rt.last_snapshot = ""

            assert last_result is not None
            # Chụp screenshot + snapshot SAU khi action xong, cho mọi step
            # (trừ ask_user — chưa có thay đổi DOM, nhưng vẫn chụp để UI có ảnh).
            ss_path, ann_path, snap = _capture_step_artifacts(rt, step_num)
            record = _make_record(
                step_num, spec.goal or spec.display_name, last_result,
                snap, screenshot_path=ss_path, annotated_path=ann_path,
            )

            all_records.append(record)
            if last_result.ask_user:
                answer = yield record
                rt.context[last_result.ask_field] = answer or ""
            else:
                yield record

            if not last_result.ok and not last_result.ask_user:
                # Flow fail
                failed_rec = _make_failed_record(step_num, spec.goal or spec.display_name, last_result.error, rt)
                all_records.append(failed_rec)
                yield failed_rec
                yielded_terminal = True
                return

            # Nested (if_visible đã set _pending_nested)
            if rt._pending_nested is not None:
                nested_steps = rt._pending_nested
                rt._pending_nested = None
                yield from _run_steps(nested_steps)
                if yielded_terminal:
                    return

            # Check success/failure sau mỗi step
            if _check_rule(spec.failure, rt):
                code = spec.failure.code if spec.failure else "FLOW_FAILED"
                msg = spec.failure.message if spec.failure else "Flow failed."
                failed_rec = _make_failed_record(step_num, spec.goal or spec.display_name, f"[{code}] {msg}", rt)
                all_records.append(failed_rec)
                yield failed_rec
                yielded_terminal = True
                return

            if _check_rule(spec.success, rt):
                step_num += 1
                done_rec = _make_done_record(step_num, spec.goal or spec.display_name,
                                             "Flow hoàn thành — success rule đạt.", rt)
                all_records.append(done_rec)
                yield done_rec
                yielded_terminal = True
                return

    try:
        yield from _run_steps(spec.steps or [])

        if not yielded_terminal:
            # Hết steps nhưng không rule success → fallback done.
            step_num += 1
            done_rec = _make_done_record(step_num, spec.goal or spec.display_name,
                                         "Flow hoàn thành — hết steps.", rt)
            all_records.append(done_rec)
            yield done_rec
    finally:
        _flush_artifacts()
