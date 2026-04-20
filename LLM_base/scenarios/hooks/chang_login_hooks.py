"""
scenarios/hooks/chang_login_hooks.py — Port logic Python của scenario chang_login
từ scenarios/chang_login.py sang 3 hook chạy qua generic runner.

- chang_login.pre_check: phát hiện "đã đăng nhập sẵn" ngay khi vào trang →
  kết thúc sớm với done record.
- chang_login.post_step:   sau mỗi step, kiểm tra DOM/snapshot có dấu hiệu
  đăng nhập thành công → inject done record và dừng agent.
- chang_login.final_capture: không dùng riêng (đã gộp vào post_step), nhưng
  export để tương lai reuse cho scenario khác.
"""

from __future__ import annotations

import json
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

import browser_adapter as browser
from scenarios.hooks_registry import HookContext, HookResult, hook
from state import StepRecord


# Các chuỗi CHỈ xuất hiện sau khi đăng nhập thành công
_LOGIN_SUCCESS_INDICATORS = (
    "hi! anh",
    "hi anh",
)

# Các indicator URL vẫn ở trang đăng nhập (dùng để loại trừ)
_LOGIN_URL_INDICATORS = (
    "/auth/", "/login", "microsoftonline.com",
    "chang.fpt.net/home", "chang.fpt.net/#", "chang.fpt.net/\n",
)

_ARTIFACTS = Path(__file__).resolve().parent.parent.parent / "artifacts"


def _strip_diacritics(s: str) -> str:
    nfd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()


def _is_login_success(text: str) -> bool:
    lower = text.lower()
    stripped = _strip_diacritics(text)
    for indicator in _LOGIN_SUCCESS_INDICATORS:
        ind_stripped = _strip_diacritics(indicator)
        if ind_stripped in stripped or indicator.lower() in lower:
            return True
    return False


def _check_dom_success() -> bool:
    return browser.page_contains_any(_LOGIN_SUCCESS_INDICATORS)


def _capture_final_state(step_num: int, outcome: str) -> dict:
    """Chụp snapshot + screenshot ở trạng thái hiện tại, ghi login_result.json."""
    now = datetime.now()
    save_dir = (
        _ARTIFACTS
        / now.strftime("%Y")
        / now.strftime("%m")
        / now.strftime("%d")
        / now.strftime("%H_%M_%S")
    )
    save_dir.mkdir(parents=True, exist_ok=True)

    url = page_title = snapshot = screenshot_b64 = screenshot_path = annotated_path = ""
    try: url = browser.get_current_url()
    except Exception: pass
    try: page_title = browser.get_page_title()
    except Exception: pass
    try: snapshot = browser.take_snapshot()
    except Exception: pass
    try:
        screenshot_b64, screenshot_path = browser.take_screenshot(
            save_path=str(save_dir / f"step_{step_num:02d}.png")
        )
    except Exception: pass
    try:
        _, annotated_path = browser.take_annotated_screenshot(
            save_path=str(save_dir / f"step_{step_num:02d}_annotated.png")
        )
    except Exception: pass

    try:
        (save_dir / "login_result.json").write_text(
            json.dumps(
                {
                    "outcome": outcome,
                    "captured_at": now.isoformat(),
                    "step": step_num,
                    "url": url,
                    "page_title": page_title,
                    "screenshot_path": screenshot_path,
                    "annotated_path": annotated_path,
                    "snapshot_chars": len(snapshot),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass

    return {
        "snapshot": snapshot,
        "screenshot_path": screenshot_path,
        "annotated_path": annotated_path,
        "screenshot_b64": screenshot_b64,
        "url": url,
        "page_title": page_title,
    }


@hook("chang_login.pre_check")
def pre_check(ctx: HookContext) -> Optional[HookResult]:
    """Nếu user đã đăng nhập sẵn (URL đã redirect khỏi trang login, hoặc DOM
    có indicator thành công), yield luôn done record và bỏ qua agent loop."""
    start_url = ctx.spec.start_url or ""
    try:
        current_url = browser.get_current_url()
    except Exception:
        current_url = start_url

    already_logged_in_url = (
        current_url
        and current_url.rstrip("/") != start_url.rstrip("/")
        and not any(ind in current_url for ind in _LOGIN_URL_INDICATORS)
    )
    try:
        pre_snapshot = browser.take_snapshot()
    except Exception:
        pre_snapshot = ""
    dom_success = _check_dom_success()
    already_logged_in = already_logged_in_url or dom_success or _is_login_success(pre_snapshot)
    if not already_logged_in:
        return None

    outcome = (
        "login_success_dom"
        if (dom_success or _is_login_success(pre_snapshot))
        else "already_logged_in"
    )
    final = _capture_final_state(step_num=1, outcome=outcome)
    msg = (
        "Đăng nhập thành công — phát hiện DOM thành công trên trang."
        if outcome == "login_success_dom"
        else f"Đã đăng nhập sẵn. URL: {final['url'] or current_url}"
    )
    record = StepRecord(
        step=1,
        goal=ctx.spec.goal,
        snapshot=final["snapshot"] or pre_snapshot or f"URL hiện tại: {current_url}",
        screenshot_path=final["screenshot_path"],
        screenshot_b64=final["screenshot_b64"],
        annotated_screenshot_b64="",
        annotated_screenshot_path=final["annotated_path"],
        action={"action": "done", "message": msg},
        url_before=start_url,
        url_after=final["url"] or current_url,
        page_title=final["page_title"],
    )
    return HookResult(terminate=True, record=record)


@hook("chang_login.post_step")
def post_step(ctx: HookContext, record: StepRecord) -> Optional[HookResult]:
    """Sau mỗi step, kiểm tra DOM/snapshot có dấu hiệu đăng nhập thành công.
    Nếu có, inject 1 done record và báo runner dừng."""
    if record.is_done:
        return None
    check_text = (record.post_snapshot or "") + (record.snapshot or "")
    dom_ok = _check_dom_success()
    snap_ok = (not dom_ok) and _is_login_success(check_text)
    if not (dom_ok or snap_ok):
        return None

    next_step = record.step + 1
    outcome = "login_success_dom" if dom_ok else "login_success_snapshot"
    final = _capture_final_state(step_num=next_step, outcome=outcome)
    msg = (
        "Đăng nhập thành công — phát hiện DOM thành công trên trang."
        if dom_ok
        else "Đăng nhập thành công — phát hiện snapshot thành công trên trang."
    )
    done_record = StepRecord(
        step=next_step,
        goal=ctx.spec.goal,
        snapshot=final["snapshot"] or check_text[:300],
        screenshot_path=final["screenshot_path"],
        screenshot_b64=final["screenshot_b64"],
        annotated_screenshot_b64="",
        annotated_screenshot_path=final["annotated_path"],
        action={"action": "done", "message": msg},
        url_before=record.url_after or "",
        url_after=final["url"] or record.url_after or "",
        page_title=final["page_title"],
    )
    return HookResult(terminate=True, record=done_record)
