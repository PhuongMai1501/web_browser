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
from scenarios.flow_runner import run_flow
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

    - Dispatch theo `spec.mode`:
        * flow   → scenarios.flow_runner.run_flow (declarative steps)
        * agent  → LLM autonomous (v1 behavior, dùng hooks)
        * hybrid → Sprint 3 (chưa ship → fallback về agent)
    - Bảo tồn protocol generator: caller dùng `gen.send(answer)` để resume
      sau record `is_blocked=True`.
    """
    mode = getattr(spec, "mode", "agent")

    if mode == "flow":
        # Set allowlist cho scenario (reset ở finally)
        domain_override_set = False
        if spec.allowed_domains:
            browser.set_allowed_domains(spec.allowed_domains)
            domain_override_set = True
        try:
            target_url = url_override or spec.start_url
            if target_url and not _steps_start_with_goto(spec.steps):
                try:
                    browser.open_url(target_url)
                    browser.wait_ms(1500)
                except Exception:
                    # Nếu fail, flow_runner vẫn chạy — step đầu có thể là goto
                    # hoặc wait_for sẽ báo lỗi rõ ràng hơn
                    pass
            yield from run_flow(
                spec=spec,
                context=context,
                session_id=session_id,
                browser=browser,
            )
        finally:
            if domain_override_set:
                try:
                    browser.reset_allowed_domains()
                except Exception:
                    pass
        return

    # mode == "agent" (hoặc "hybrid" fallback cho đến khi ship Sprint 3)
    # Set allowlist TRƯỚC khi open_url — nếu không open_url dùng default
    # allowlist và chặn domain của scenario này. try/finally đảm bảo reset
    # kể cả khi generator bị close giữa chừng.
    agent_domain_override_set = False
    if spec.allowed_domains:
        browser.set_allowed_domains(spec.allowed_domains)
        agent_domain_override_set = True

    try:
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
    finally:
        if agent_domain_override_set:
            try:
                browser.reset_allowed_domains()
            except Exception:
                pass


def _steps_start_with_goto(steps) -> bool:
    for s in steps or []:
        return getattr(s, "action", "") == "goto"
    return False
