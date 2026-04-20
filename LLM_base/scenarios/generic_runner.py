"""
scenarios/generic_runner.py — Chạy 1 scenario khai báo (ScenarioSpec) bằng
cách kết hợp hook Python + vòng lặp LLM autonomous chung.

Đây là thay thế cho các hàm `run_chang_login_autonomous` / if-elif switch
theo tên scenario trong job_handler.py trước refactor.
"""

from __future__ import annotations

from typing import Generator, Optional

import browser_adapter as browser
from runner import run_agent_autonomous
from scenarios.hooks_registry import HookContext, HookResult, get_hook
from scenarios.spec import ScenarioSpec
from state import StepRecord


def _default_goal(target_url: Optional[str]) -> str:
    if target_url:
        return f"Thực hiện tác vụ trên {target_url}"
    return "Thực hiện tác vụ được yêu cầu."


def run_scenario(
    spec: ScenarioSpec,
    api_key: str,
    context: Optional[dict],
    max_steps: int,
    session_id: str,
    *,
    goal_override: Optional[str] = None,
    url_override: Optional[str] = None,
) -> Generator[StepRecord, Optional[str], None]:
    """Generator thống nhất cho mọi scenario.

    - Dùng HOOK_REGISTRY để resolve hook theo tên (pre_check, post_step,
      final_capture). Hook chưa register sẽ raise sớm.
    - Bảo tồn protocol generator cũ: caller dùng `gen.send(answer)` để resume
      sau action=ask, `record.is_blocked` để phát hiện cần hỏi user.
    """
    ctx = HookContext(
        browser=browser,
        spec=spec,
        context=context or {},
        session_id=session_id,
    )

    target_url = url_override or spec.start_url
    if target_url:
        browser.open_url(target_url)
        try:
            browser.wait_ms(2000)
        except Exception:
            pass

    pre_hook = get_hook(spec.hooks.pre_check)
    if pre_hook is not None:
        pre_result: Optional[HookResult] = pre_hook(ctx)
        if pre_result and pre_result.terminate:
            if pre_result.record is not None:
                yield pre_result.record
            return

    goal = goal_override or spec.goal or _default_goal(target_url)

    inner = run_agent_autonomous(
        goal=goal,
        api_key=api_key,
        context=context,
        max_steps=max_steps,
        session_id=session_id,
        system_prompt_extra=spec.system_prompt_extra,
        allowed_domains=spec.allowed_domains or None,
    )

    post_hook = get_hook(spec.hooks.post_step)
    final_hook = get_hook(spec.hooks.final_capture)

    answer: Optional[str] = None
    while True:
        try:
            record = inner.send(answer)
            answer = None
        except StopIteration:
            return

        # Chạy post_step TRƯỚC khi yield, để kịp quyết định có inject done record
        post_result: Optional[HookResult] = None
        if post_hook is not None and not record.is_done:
            post_result = post_hook(ctx, record)

        # Yield record hiện tại (và chờ answer nếu block)
        if record.is_blocked:
            answer = yield record
        else:
            yield record

        if post_result and post_result.terminate:
            # final_capture optional; ưu tiên record hook đã build
            if final_hook is not None:
                final_result = final_hook(ctx, record)
                if final_result and final_result.record is not None:
                    yield final_result.record
            if post_result.record is not None:
                yield post_result.record
            try:
                inner.close()
            except Exception:
                pass
            return

        if record.is_done:
            return
