"""
runner.py - Orchestrator: vòng lặp agent điều khiển browser bằng LLM.
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Generator

import browser_adapter as browser
import llm_planner as planner
from state import StepRecord, SessionState

_BASE_ARTIFACTS = Path(__file__).parent / "artifacts"
_MAX_SCREENSHOTS = int(os.getenv("MAX_SCREENSHOTS_RETAIN", "10"))
_logger = logging.getLogger(__name__)

# Secret keys cần mask khi lưu log
_SECRET_KEYS = frozenset({"password", "pass", "secret", "token", "otp", "pin", "passwd"})

# Placeholder chính xác (exact match) — tránh bắt nhầm credentials thật
_EXACT_PLACEHOLDERS = frozenset({
    "your_password_here", "your_email_here", "placeholder",
    "<password>", "<email>", "enter_password", "enter_email",
    "your_username_here", "username_here", "password_here",
    "email_here", "[password]", "[email]", "yourpassword",
    "youremail", "test@example.com", "user@example.com",
    "your_", "_here",  # partial patterns
})


def _mask_prompt_secrets(prompt: str, context: dict | None) -> str:
    """Thay thế giá trị secret trong prompt text trước khi lưu log."""
    if not context:
        return prompt
    masked = prompt
    for k, v in context.items():
        if k.lower() in _SECRET_KEYS and v and len(str(v)) > 2:
            masked = masked.replace(str(v), "***")
    return masked


def _cleanup_old_screenshots(run_dir: Path) -> None:
    """Xóa screenshot PNG cũ, giữ max _MAX_SCREENSHOTS ảnh gần nhất."""
    try:
        pngs = sorted(run_dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
        if len(pngs) > _MAX_SCREENSHOTS:
            for f in pngs[:-_MAX_SCREENSHOTS]:
                f.unlink(missing_ok=True)
    except Exception as e:
        _logger.debug("Screenshot cleanup failed: %s", e)


def _make_run_dir() -> Path:
    """Tạo thư mục run theo cấu trúc artifacts/YYYY/MM/DD/HH_MM_SS/."""
    now = datetime.now()
    run_dir = _BASE_ARTIFACTS / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d") / now.strftime("%H_%M_%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir

def run_agent(
    goal: str,
    api_key: str,
    max_steps: int = 10,
    max_retries: int = 3,  # Số lần retry khi ref không hợp lệ trong 1 step
    session_id: str = "",
) -> Generator[StepRecord, None, SessionState]:
    """
    Generator chạy vòng lặp agent. Yield từng StepRecord sau mỗi bước.
    Browser đã được mở trước khi gọi hàm này.

    Yields:
        StepRecord cho mỗi bước thực thi

    Returns:
        SessionState cuối cùng (qua StopIteration.value)
    """
    session = SessionState(goal, session_id=session_id)
    run_dir = _make_run_dir()
    step_num = 0
    vfb_log: list[dict] = []

    while step_num < max_steps:
        step_num += 1

        # Lấy URL + title trước khi action
        try:
            url_before = browser.get_current_url()
        except Exception:
            url_before = ""
        try:
            page_title = browser.get_page_title()
        except Exception:
            page_title = ""

        # Snapshot
        try:
            snapshot = browser.take_snapshot()
        except Exception as e:
            record = StepRecord(
                step=step_num,
                goal=goal,
                snapshot="",
                screenshot_path="",
                screenshot_b64="",
                annotated_screenshot_b64="",
                action={"action": "done", "message": f"Lỗi snapshot: {e}"},
                url_before=url_before,
                page_title=page_title,
                error=str(e),
            )
            session.add_step(record)
            yield record
            break

        # Hỏi LLM quyết định action — text only, không gửi ảnh
        action = None
        llm_raw = ""
        llm_prompt = ""
        error_msg = ""
        screenshot_b64 = ""
        screenshot_path = ""
        annotated_b64 = ""
        annotated_screenshot_path = ""
        visual_fallback_used = False

        for attempt in range(max_retries):
            try:
                if attempt == 0:
                    action, llm_raw, llm_prompt = planner.decide_action(
                        goal=goal,
                        snapshot=snapshot,
                        api_key=api_key,
                        step=step_num,
                    )
                else:
                    action, llm_raw, llm_prompt = planner.decide_action_retry(
                        goal=goal,
                        snapshot=snapshot,
                        invalid_ref=action.get("ref", ""),
                        api_key=api_key,
                        step=step_num,
                    )

                # Validate ref nếu action cần ref
                if action.get("action") in ("click", "type"):
                    ref = action.get("ref", "")
                    if not browser.ref_exists(ref, snapshot):
                        continue  # ref không tồn tại → retry text-only

                    # Visual fallback: element tồn tại nhưng không có mô tả → mới gửi ảnh
                    if not browser.element_has_description(ref, snapshot):
                        try:
                            fresh_snapshot = browser.take_snapshot()
                            fresh_b64, _ = browser.take_screenshot(
                                save_path=str(run_dir / f"step_{step_num:02d}_vfb.png")
                            )
                            fresh_annotated_b64 = fresh_b64
                            try:
                                fresh_annotated_b64, _ = browser.take_annotated_screenshot()
                            except Exception:
                                pass

                            action, llm_raw, llm_prompt = planner.decide_action_visual_fallback(
                                goal=goal,
                                snapshot=fresh_snapshot,
                                screenshot_b64=fresh_b64,
                                undescribed_ref=ref,
                                api_key=api_key,
                                step=step_num,
                                annotated_b64=fresh_annotated_b64,
                            )
                            # Validate ref mới từ visual fallback
                            if action.get("action") in ("click", "type"):
                                if not browser.ref_exists(action.get("ref", ""), fresh_snapshot):
                                    continue  # vẫn invalid → retry tiếp
                                snapshot = fresh_snapshot
                            # Lưu ảnh fallback cho display + log
                            screenshot_b64 = fresh_b64
                            visual_fallback_used = True
                            vfb_log.append({
                                "step": step_num,
                                "triggered_by_ref": ref,
                                "screenshot_path": str(run_dir / f"step_{step_num:02d}_vfb.png"),
                                "prompt_sent": llm_prompt,
                                "llm_response": llm_raw,
                                "action_decided": action,
                            })
                        except Exception as e:
                            error_msg = str(e)

                break  # ref hợp lệ hoặc action không cần ref

            except Exception as e:
                error_msg = str(e)
                action = {"action": "done", "message": f"Lỗi LLM: {e}", "reason": str(e)}
                llm_raw = str(e)
                break
        else:
            # for...else: vòng lặp hết max_retries mà không break → ref luôn không hợp lệ
            action = {
                "action": "done",
                "message": f"Không tìm thấy element phù hợp sau {max_retries} lần thử",
                "reason": "Ref không hợp lệ",
            }

        # Chụp ảnh để hiển thị trên UI (chỉ khi chưa có từ visual fallback)
        if not screenshot_b64:
            try:
                screenshot_b64, screenshot_path = browser.take_screenshot(
                    save_path=str(run_dir / f"step_{step_num:02d}.png")
                )
            except Exception as e:
                screenshot_b64 = ""
                screenshot_path = ""
                error_msg = error_msg or str(e)
                _logger.warning("Screenshot fail tại step %d: %s", step_num, e)
        try:
            annotated_b64, annotated_screenshot_path = browser.take_annotated_screenshot(
                save_path=str(run_dir / f"step_{step_num:02d}_annotated.png")
            )
        except Exception as e:
            annotated_b64 = screenshot_b64
            annotated_screenshot_path = ""
            _logger.warning("Annotated screenshot fail tại step %d: %s", step_num, e)

        # Thực thi action
        action_type = action.get("action", "done")
        try:
            if action_type == "click":
                browser.click_element(action.get("ref", ""))
            elif action_type == "type":
                browser.type_text(action.get("ref", ""), action.get("text", ""))
            elif action_type == "wait":
                browser.wait_ms(action.get("ms", 1000))

        except Exception as e:
            error_msg = str(e)

        # Lấy URL + title sau action
        try:
            url_after = browser.get_current_url()
        except Exception:
            url_after = ""

        # Chụp snapshot + screenshot sau action để có thể kiểm tra trạng thái mới
        post_snapshot = ""
        post_screenshot_path = ""
        post_page_title = ""
        if action_type != "done":
            try:
                # Đợi ngắn để trang kịp render (đặc biệt khi mở tab mới)
                browser.wait_ms(500)
            except Exception:
                pass
            try:
                post_snapshot = browser.take_snapshot()
            except Exception:
                pass
            try:
                _, post_screenshot_path = browser.take_screenshot(
                    save_path=str(run_dir / f"step_{step_num:02d}_post.png")
                )
            except Exception:
                pass
            try:
                post_page_title = browser.get_page_title()
            except Exception:
                pass

        record = StepRecord(
            step=step_num,
            goal=goal,
            snapshot=snapshot,
            screenshot_path=screenshot_path,
            screenshot_b64=screenshot_b64,
            annotated_screenshot_b64=annotated_b64,
            action=action,
            llm_prompt=llm_prompt,
            llm_raw_response=llm_raw,
            url_before=url_before,
            url_after=url_after,
            page_title=page_title,
            annotated_screenshot_path=annotated_screenshot_path,
            post_snapshot=post_snapshot,
            post_screenshot_path=post_screenshot_path,
            post_page_title=post_page_title,
            error=error_msg,
            visual_fallback_used=visual_fallback_used,
        )
        session.add_step(record)
        try:
            session.save_log(run_dir)  # L1: incremental save sau mỗi step
        except Exception:
            pass
        yield record

        if action_type == "done":
            break

    # Lưu session log cuối (đảm bảo flush)
    try:
        session.save_log(run_dir)
    except Exception:
        pass
    try:
        if vfb_log:
            session.save_visual_fallback_log(vfb_log, run_dir)
    except Exception:
        pass
    _cleanup_old_screenshots(run_dir)

    return session


def run_agent_autonomous(
    goal: str,
    api_key: str,
    context: dict | None = None,
    max_steps: int = 20,
    max_retries: int = 3,
    session_id: str = "",
    system_prompt_extra: str = "",
    allowed_domains: list[str] | None = None,
) -> Generator[StepRecord, None, SessionState]:
    """
    Autonomous agent với memory — LLM tự suy luận đa bước từ lịch sử.
    Không cần kịch bản hardcode. Hỗ trợ action 'ask' khi LLM cần thêm thông tin.

    Args:
        goal: Mục tiêu tổng thể (có thể gồm nhiều bước ngầm)
        api_key: OpenAI API key
        context: Thông tin bổ sung gửi kèm (email, password, ...)
        max_steps: Số bước tối đa
        max_retries: Số lần retry khi ref không hợp lệ trong 1 step
        session_id: ID của session để ghi vào log
        system_prompt_extra: Text append vào SYSTEM_PROMPT cho scenario này
        allowed_domains: Override domain allowlist của browser_adapter (None → mặc định)
    """
    # Override allowlist nếu scenario khai báo; reset khi kết thúc.
    _domain_override_set = False
    if allowed_domains:
        browser.set_allowed_domains(allowed_domains)
        _domain_override_set = True

    session = SessionState(goal, session_id=session_id)
    run_dir = _make_run_dir()
    step_num = 0
    history: list[dict] = []
    vfb_log: list[dict] = []

    while step_num < max_steps:
        step_num += 1

        try:
            url_before = browser.get_current_url()
        except Exception:
            url_before = ""
        try:
            page_title = browser.get_page_title()
        except Exception:
            page_title = ""

        try:
            snapshot = browser.take_snapshot()
        except Exception as e:
            record = StepRecord(
                step=step_num, goal=goal, snapshot="",
                screenshot_path="", screenshot_b64="", annotated_screenshot_b64="",
                action={"action": "done", "message": f"Lỗi snapshot: {e}"},
                url_before=url_before, page_title=page_title, error=str(e),
            )
            session.add_step(record)
            yield record
            break

        action = None
        llm_raw = ""
        llm_prompt = ""
        error_msg = ""
        screenshot_b64 = ""
        screenshot_path = ""
        annotated_b64 = ""
        annotated_screenshot_path = ""
        visual_fallback_used = False

        for attempt in range(max_retries):
            try:
                if attempt == 0:
                    action, llm_raw, llm_prompt = planner.decide_action_autonomous(
                        goal=goal,
                        history=history,
                        snapshot=snapshot,
                        api_key=api_key,
                        step=step_num,
                        context=context,
                        system_prompt_extra=system_prompt_extra,
                    )
                else:
                    action, llm_raw, llm_prompt = planner.decide_action_retry(
                        goal=goal,
                        snapshot=snapshot,
                        invalid_ref=action.get("ref", ""),
                        api_key=api_key,
                        step=step_num,
                        system_prompt_extra=system_prompt_extra,
                    )

                # ask / done không cần validate ref
                if action.get("action") in ("ask", "done", "wait"):
                    break

                # Chặn placeholder: LLM đôi khi tự bịa giá trị khi thiếu thông tin
                # Dùng exact match để tránh bắt nhầm credentials hợp lệ
                if action.get("action") == "type":
                    text_val = action.get("text", "") or ""
                    if text_val.lower().strip() in _EXACT_PLACEHOLDERS:
                        action = {
                            "action": "ask",
                            "ask_type": "question",
                            "message": "Cần thông tin thực để điền vào field này. Vui lòng cung cấp giá trị cần điền.",
                            "reason": f"LLM trả về placeholder '{text_val}' thay vì giá trị thực — chuyển sang hỏi user.",
                        }
                        break

                if action.get("action") in ("click", "type"):
                    ref = action.get("ref", "")
                    if not browser.ref_exists(ref, snapshot):
                        continue  # retry

                    if not browser.element_has_description(ref, snapshot):
                        try:
                            fresh_snapshot = browser.take_snapshot()
                            fresh_b64, _ = browser.take_screenshot(
                                save_path=str(run_dir / f"step_{step_num:02d}_vfb.png")
                            )
                            fresh_annotated_b64 = fresh_b64
                            try:
                                fresh_annotated_b64, _ = browser.take_annotated_screenshot()
                            except Exception:
                                pass

                            action, llm_raw, llm_prompt = planner.decide_action_visual_fallback(
                                goal=goal,
                                snapshot=fresh_snapshot,
                                screenshot_b64=fresh_b64,
                                undescribed_ref=ref,
                                api_key=api_key,
                                step=step_num,
                                annotated_b64=fresh_annotated_b64,
                                system_prompt_extra=system_prompt_extra,
                            )
                            if action.get("action") in ("click", "type"):
                                if not browser.ref_exists(action.get("ref", ""), fresh_snapshot):
                                    continue
                                snapshot = fresh_snapshot
                            screenshot_b64 = fresh_b64
                            visual_fallback_used = True
                            vfb_log.append({
                                "step": step_num,
                                "triggered_by_ref": ref,
                                "screenshot_path": str(run_dir / f"step_{step_num:02d}_vfb.png"),
                                "prompt_sent": llm_prompt,
                                "llm_response": llm_raw,
                                "action_decided": action,
                            })
                        except Exception as e:
                            error_msg = str(e)

                break

            except Exception as e:
                error_msg = str(e)
                action = {"action": "done", "message": f"Lỗi LLM: {e}", "reason": str(e)}
                llm_raw = str(e)
                break
        else:
            action = {
                "action": "ask",
                "message": f"Không tìm thấy element phù hợp sau {max_retries} lần thử. Bạn có thể hướng dẫn thêm?",
                "reason": "Ref không hợp lệ",
            }

        # Guardrails: phát hiện loop — cùng action lặp ≥3 lần liên tiếp
        if len(history) >= 2 and action.get("action") in ("click", "type"):
            current_key = (action.get("action"), action.get("ref", ""))
            past_keys = [(h["action_type"], h.get("ref", "")) for h in history[-2:]]
            if all(k == current_key for k in past_keys):
                action = {
                    "action": "ask",
                    "ask_type": "error",
                    "message": (
                        f"Agent đang lặp lại '{current_key[0]}' trên '{current_key[1]}' "
                        f"{len(past_keys) + 1} lần liên tiếp. Trang có thể không phản hồi. "
                        "Bạn muốn tiếp tục như thế nào?"
                    ),
                    "reason": "Loop guard: repeated action detected",
                }

        # Chụp ảnh hiển thị UI
        if not screenshot_b64:
            try:
                screenshot_b64, screenshot_path = browser.take_screenshot(
                    save_path=str(run_dir / f"step_{step_num:02d}.png")
                )
            except Exception as e:
                screenshot_b64 = ""
                screenshot_path = ""
                error_msg = error_msg or str(e)
                _logger.warning("Screenshot fail tại step %d: %s", step_num, e)
        try:
            annotated_b64, annotated_screenshot_path = browser.take_annotated_screenshot(
                save_path=str(run_dir / f"step_{step_num:02d}_annotated.png")
            )
        except Exception as e:
            annotated_b64 = screenshot_b64
            annotated_screenshot_path = ""
            _logger.warning("Annotated screenshot fail tại step %d: %s", step_num, e)

        action_type = action.get("action", "done")
        is_blocked = action_type == "ask"

        # Thực thi action (không thực thi ask/done)
        try:
            if action_type == "click":
                browser.click_element(action.get("ref", ""))
            elif action_type == "type":
                browser.type_text(action.get("ref", ""), action.get("text", ""))
            elif action_type == "wait":
                browser.wait_ms(action.get("ms", 1000))
        except Exception as e:
            error_msg = str(e)

        try:
            url_after = browser.get_current_url()
        except Exception:
            url_after = ""

        post_snapshot = ""
        post_screenshot_path = ""
        post_page_title = ""
        if action_type not in ("done", "ask"):
            try:
                browser.wait_ms(500)
            except Exception:
                pass
            try:
                post_snapshot = browser.take_snapshot()
            except Exception:
                pass
            try:
                _, post_screenshot_path = browser.take_screenshot(
                    save_path=str(run_dir / f"step_{step_num:02d}_post.png")
                )
            except Exception:
                pass
            try:
                post_page_title = browser.get_page_title()
            except Exception:
                pass

        # Tích lũy history cho bước tiếp theo
        result_hint = ""
        if url_after and url_after != url_before:
            result_hint = f"(URL → {url_after})"
        elif post_snapshot and post_snapshot != snapshot:
            result_hint = "(trang thay đổi)"
        elif error_msg:
            result_hint = f"(lỗi: {error_msg})"

        history.append({
            "step": step_num,
            "action_type": action_type,
            "ask_type": action.get("ask_type", "question") if action_type == "ask" else "",
            "ref": action.get("ref", ""),
            "text": action.get("text", ""),
            "ms": action.get("ms", 0),
            "question": action.get("message", "") if action_type == "ask" else "",
            "answer": "",
            "url_before": url_before,
            "url_after": url_after,
            "page_title": page_title,
            "post_page_title": post_page_title,
            "result_hint": result_hint,
        })

        record = StepRecord(
            step=step_num,
            goal=goal,
            snapshot=snapshot,
            screenshot_path=screenshot_path,
            screenshot_b64=screenshot_b64,
            annotated_screenshot_b64=annotated_b64,
            action=action,
            llm_prompt=_mask_prompt_secrets(llm_prompt, context),  # Secret: mask trước khi log
            llm_raw_response=llm_raw,
            url_before=url_before,
            url_after=url_after,
            page_title=page_title,
            annotated_screenshot_path=annotated_screenshot_path,
            post_snapshot=post_snapshot,
            post_screenshot_path=post_screenshot_path,
            post_page_title=post_page_title,
            error=error_msg,
            visual_fallback_used=visual_fallback_used,
            is_blocked=is_blocked,
        )
        session.add_step(record)
        try:
            session.save_log(run_dir)
        except Exception:
            pass

        if action_type == "ask":
            # Dừng tại đây và chờ caller gọi gen.send(answer) để tiếp tục
            answer = yield record
            history[-1]["answer"] = answer or ""
            continue  # vào vòng lặp tiếp theo với answer đã được ghi vào history

        yield record

        if action_type == "done":
            break
    try:
        if vfb_log:
            session.save_visual_fallback_log(vfb_log, run_dir)
    except Exception:
        pass
    _cleanup_old_screenshots(run_dir)

    # Reset allowlist về default để worker kế tiếp không bị ảnh hưởng
    if _domain_override_set:
        try:
            browser.reset_allowed_domains()
        except Exception:
            pass

    return session
