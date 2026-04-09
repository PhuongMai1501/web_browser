"""
scenarios/chang_login.py - Kịch bản autonomous: Đăng nhập vào chang.fpt.net
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Generator

sys.path.insert(0, str(Path(__file__).parent.parent))

import browser_adapter as browser
from runner import run_agent_autonomous
from state import StepRecord

_ARTIFACTS = Path(__file__).parent.parent / "artifacts"


CHANG_URL = "https://chang.fpt.net"

# Các chuỗi text CHỈ xuất hiện sau khi đăng nhập thành công (không có trên landing page)
# "Chang Biết Tuốt" là tên app → xuất hiện TRƯỚC login → KHÔNG dùng làm indicator
# "Hi! anh" là greeting cá nhân → CHỈ xuất hiện sau login
_LOGIN_SUCCESS_INDICATORS = (
    "hi! anh",   # DOM text: "Hi! anh Nguyễn..." (có dấu !)
    "hi anh",    # accessibility tree thường bỏ dấu ! → "Hi anh..."
)


def _strip_diacritics(s: str) -> str:
    """Bỏ dấu tiếng Việt để so sánh không phân biệt dấu."""
    import unicodedata
    nfd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()


def _is_login_success(text: str) -> bool:
    """Trả True nếu text chứa bất kỳ dấu hiệu đăng nhập thành công nào.
    So sánh cả 2 dạng: có dấu và không dấu."""
    lower = text.lower()
    stripped = _strip_diacritics(text)
    for indicator in _LOGIN_SUCCESS_INDICATORS:
        ind_stripped = _strip_diacritics(indicator)
        if ind_stripped in stripped or indicator.lower() in lower:
            return True
    return False


def _check_dom_success() -> bool:
    """
    Kiểm tra DOM thực tế qua JS (document.body.innerText).
    Đáng tin hơn accessibility snapshot vì capture mọi text kể cả non-accessible elements.
    """
    return browser.page_contains_any(_LOGIN_SUCCESS_INDICATORS)


def _capture_final_state(step_num: int, outcome: str) -> dict:
    """
    Chụp snapshot + screenshot + annotated screenshot ở trạng thái hiện tại.
    Lưu vào artifacts/YYYY/MM/DD/HH_MM_SS/ và ghi login_result.json.
    Trả dict với các keys: snapshot, screenshot_path, annotated_path, screenshot_b64, url, page_title, save_dir.
    """
    now = datetime.now()
    save_dir = (
        _ARTIFACTS
        / now.strftime("%Y")
        / now.strftime("%m")
        / now.strftime("%d")
        / now.strftime("%H_%M_%S")
    )
    save_dir.mkdir(parents=True, exist_ok=True)

    url = ""
    page_title = ""
    snapshot = ""
    screenshot_b64 = ""
    screenshot_path = ""
    annotated_path = ""

    try:
        url = browser.get_current_url()
    except Exception:
        pass
    try:
        page_title = browser.get_page_title()
    except Exception:
        pass
    try:
        snapshot = browser.take_snapshot()
    except Exception:
        pass
    try:
        screenshot_b64, screenshot_path = browser.take_screenshot(
            save_path=str(save_dir / f"step_{step_num:02d}.png")
        )
    except Exception:
        pass
    try:
        _, annotated_path = browser.take_annotated_screenshot(
            save_path=str(save_dir / f"step_{step_num:02d}_annotated.png")
        )
    except Exception:
        pass

    # Ghi log JSON tóm tắt kết quả
    log_entry = {
        "outcome": outcome,
        "captured_at": now.isoformat(),
        "step": step_num,
        "url": url,
        "page_title": page_title,
        "screenshot_path": screenshot_path,
        "annotated_path": annotated_path,
        "snapshot_chars": len(snapshot),
    }
    try:
        (save_dir / "login_result.json").write_text(
            json.dumps(log_entry, ensure_ascii=False, indent=2), encoding="utf-8"
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
        "save_dir": str(save_dir),
    }


# Goal cụ thể: hướng dẫn từng bước cho flow Microsoft Azure (ổn định, ít lạc hướng)
CHANG_AUTONOMOUS_GOAL_SPECIFIC = (
    "Đăng nhập vào trang chang.fpt.net bằng tài khoản Microsoft Azure. "
    "Thực hiện theo thứ tự các bước sau:\n"
    "1. Tìm và click nút/icon đăng nhập (nhận ra qua icon=key, icon=lock, "
    "href chứa '/login' hoặc '/auth', text/aria-label liên quan đến đăng nhập)\n"
    "2. Khi popup hoặc trang đăng nhập xuất hiện, click nút 'Đăng nhập Microsoft Azure'\n"
    "3. Khi trang Microsoft Sign in hiện ra, điền email vào ô email input "
    "(dùng thông tin 'email' trong THÔNG TIN BỔ SUNG nếu có, nếu không có thì hỏi)\n"
    "4. Click nút Next\n"
    "5. Khi trang Enter password hiện ra, điền password vào ô password input "
    "(dùng thông tin 'password' trong THÔNG TIN BỔ SUNG nếu có, nếu không có thì hỏi)\n"
    "6. Click nút Sign in\n"
    "7. Sau khi click Sign in, Microsoft có thể yêu cầu xác thực qua ứng dụng (Authenticator app). "
    "Khi nhận ra trang đang chờ xác thực (có thể thấy: 'Approve sign-in request', 'Verifying', "
    "'Open your Authenticator app'...) → dùng action=ask (ask_type=question) để hỏi user: "
    "'Vui lòng xác thực qua ứng dụng Microsoft Authenticator trên điện thoại, rồi nhập OK để tiếp tục.'\n"
    "8. Sau khi user xác nhận đã xác thực, chờ trang chuyển sang 'Stay signed in?' → click nút 'Yes'\n"
    "9. Sau khi click Yes, chụp màn hình và hoàn thành (action=done).\n"
    "QUAN TRỌNG: email field → chỉ điền email; password field → chỉ điền password. "
    "Nếu thiếu thông tin cho field nào, dùng action=ask (ask_type=question) để hỏi đúng loại thông tin đó.\n"
    "NHẬN BIẾT ĐĂNG NHẬP THÀNH CÔNG (ưu tiên cao nhất): Nếu snapshot có BẤT KỲ dấu hiệu nào sau:\n"
    "  - URL chứa '/dashboard'\n"
    "  - textbox 'Bạn muốn biết điều gì?'\n"
    "  - navigation link 'Chang Biết Tuốt' hoặc 'Trợ lý của tôi'\n"
    "  - text chào cá nhân 'Hi! anh' hoặc 'Hi anh'\n"
    "→ Đây là trang dashboard sau đăng nhập THÀNH CÔNG. Lập tức action=done. "
    "KHÔNG hỏi thêm, KHÔNG báo lỗi, BỎ QUA mọi thông báo trong === LỖI TRÊN TRANG ===."
)

# Goal generic: LLM tự suy luận toàn bộ flow từ snapshot (linh hoạt hơn cho các trang mới)
CHANG_AUTONOMOUS_GOAL_GENERIC = (
    "Đăng nhập vào trang chang.fpt.net bằng thông tin trong THÔNG TIN BỔ SUNG. "
    "Tự quan sát trang, tìm cách đăng nhập phù hợp, và hoàn thành khi đã đăng nhập thành công. "
    "Nếu thiếu thông tin cần thiết, dùng action=ask để hỏi."
)

# Đang dùng goal nào — đổi dòng này để chuyển đổi
CHANG_AUTONOMOUS_GOAL = CHANG_AUTONOMOUS_GOAL_SPECIFIC


def run_chang_login_autonomous(
    api_key: str,
    context: dict | None = None,
    max_steps: int = 20,
    session_id: str = "",
) -> Generator[StepRecord, None, None]:
    """
    Autonomous agent — LLM tự suy luận toàn bộ flow đăng nhập
    dựa trên history + snapshot, không cần kịch bản Python hardcode.
    """
    browser.open_url(CHANG_URL)
    try:
        browser.wait_ms(2000)
    except Exception:
        pass

    # ── Pre-check: đã đăng nhập chưa? ─────────────────────────────────────
    # Kiểm tra URL redirect và DOM indicator sau khi trang load
    try:
        current_url = browser.get_current_url()
    except Exception:
        current_url = CHANG_URL

    _LOGIN_INDICATORS = ("/auth/", "/login", "microsoftonline.com", "chang.fpt.net/home",
                         "chang.fpt.net/#", "chang.fpt.net/\n")
    already_logged_in_url = (
        current_url
        and current_url.rstrip("/") != CHANG_URL.rstrip("/")
        and not any(ind in current_url for ind in _LOGIN_INDICATORS)
    )

    # Kiểm tra thêm DOM indicator qua JS (đáng tin hơn snapshot)
    try:
        pre_snapshot = browser.take_snapshot()
    except Exception:
        pre_snapshot = ""

    dom_success = _check_dom_success()
    already_logged_in = already_logged_in_url or dom_success or _is_login_success(pre_snapshot)

    if already_logged_in:
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
            goal=CHANG_AUTONOMOUS_GOAL,
            snapshot=final["snapshot"] or pre_snapshot or f"URL hiện tại: {current_url}",
            screenshot_path=final["screenshot_path"],
            screenshot_b64=final["screenshot_b64"],
            annotated_screenshot_b64="",
            annotated_screenshot_path=final["annotated_path"],
            action={"action": "done", "message": msg},
            url_before=CHANG_URL,
            url_after=final["url"] or current_url,
            page_title=final["page_title"],
        )
        yield record
        return
    # ───────────────────────────────────────────────────────────────────────

    # ── Main agent loop với DOM success detection ──────────────────────────
    # Không dùng yield from để có thể intercept từng record và kiểm tra DOM
    gen = run_agent_autonomous(
        goal=CHANG_AUTONOMOUS_GOAL,
        api_key=api_key,
        context=context,
        max_steps=max_steps,
        session_id=session_id,
    )
    answer = None
    while True:
        try:
            record = gen.send(answer)
            answer = None
        except StopIteration:
            break

        # Kiểm tra DOM thực tế qua JS — kể cả khi LLM đã quyết định ask
        # (LLM có thể nhầm vì snapshot không capture đủ text)
        check_text = (record.post_snapshot or "") + (record.snapshot or "")
        dom_ok = not record.is_done and _check_dom_success()
        snap_ok = not record.is_done and not dom_ok and _is_login_success(check_text)

        if dom_ok or snap_ok:
            yield record  # yield step hiện tại để UI hiển thị

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
                goal=CHANG_AUTONOMOUS_GOAL,
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
            gen.close()
            yield done_record
            return

        if record.is_blocked:
            answer = yield record  # chờ answer từ caller (resume)
        else:
            yield record

        if record.is_done:
            break
