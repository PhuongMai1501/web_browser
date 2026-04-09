"""
app.py - Streamlit chatbot UI tích hợp agent-browser và GPT-4o mini.
API key đọc từ file .env (OPENAI_API_KEY).
"""

import sys
import os
import base64
import json
from dataclasses import asdict
from pathlib import Path
from io import BytesIO

import streamlit as st
from PIL import Image
from dotenv import load_dotenv
from openai import RateLimitError

# Đọc .env từ cùng thư mục với app.py
load_dotenv(Path(__file__).parent / ".env")

sys.path.insert(0, str(Path(__file__).parent))

import browser_adapter as browser
from runner import run_agent
from scenarios.chang_login import (
    run_chang_login_autonomous,
    CHANG_AUTONOMOUS_GOAL, CHANG_URL,
)
from state import StepRecord, SessionState, ARTIFACTS_DIR

# ─── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Chang AI Browser Agent",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ─── Load API key từ .env ─────────────────────────────────────────────────────
def get_api_key() -> str:
    return os.getenv("OPENAI_API_KEY", "")


def api_key_valid() -> bool:
    k = get_api_key()
    return bool(k) and k != "sk-your-api-key-here"


def _friendly_error(e: Exception) -> str:
    """Map exception → thông báo thân thiện cho user."""
    if isinstance(e, RateLimitError):
        return "API key đã đạt giới hạn rate limit. Vui lòng thử lại sau ít phút."
    if isinstance(e, TimeoutError):
        return "Browser không phản hồi (timeout). Kiểm tra agent-browser còn chạy không."
    if isinstance(e, json.JSONDecodeError):
        return "LLM trả về response không hợp lệ (không phải JSON). Thử lại."
    if isinstance(e, ConnectionError):
        return "Mất kết nối. Kiểm tra mạng và agent-browser."
    if isinstance(e, ValueError) and "Domain" in str(e):
        return f"URL bị chặn bởi domain allowlist: {e}"
    return f"Lỗi: {e}"


# ─── Render step card ─────────────────────────────────────────────────────────
def render_step_card(step_data: dict):
    """Render một bước với đầy đủ thông tin debug."""
    action = step_data.get("action", {})
    action_type = action.get("action", "unknown")
    reason = action.get("reason", "")
    ref = action.get("ref", "")
    screenshot_b64 = step_data.get("screenshot_b64", "")
    annotated_b64 = step_data.get("annotated_screenshot_b64", "") or screenshot_b64
    snapshot = step_data.get("snapshot", "")
    step_num = step_data.get("step", "?")
    url_before = step_data.get("url_before", "")
    url_after = step_data.get("url_after", "")
    post_snapshot = step_data.get("post_snapshot", "")
    post_screenshot_path = step_data.get("post_screenshot_path", "")
    error = step_data.get("error", "")
    llm_raw = step_data.get("llm_raw_response", "")
    llm_prompt = step_data.get("llm_prompt", "")
    visual_fallback = step_data.get("visual_fallback_used", False)

    action_emoji = {"click": "👆", "type": "⌨️", "wait": "⏳", "done": "✅"}.get(action_type, "🔹")
    mode_badge = "📷 Visual Fallback" if visual_fallback else "📝 Text-only"
    mode_color = "orange" if visual_fallback else "gray"

    # ── Header ────────────────────────────────────────────────────────────────
    st.markdown(
        f"<h4 style='margin:0'>Bước {step_num} &nbsp; {action_emoji} "
        f"<code>{action_type.upper()}</code> &nbsp;"
        f"<span style='font-size:0.75em;color:{mode_color};font-weight:normal'>{mode_badge}</span></h4>",
        unsafe_allow_html=True,
    )
    if url_before:
        if url_after and url_after != url_before:
            st.caption(f"🌐 `{url_before}` → `{url_after}`")
        else:
            st.caption(f"🌐 `{url_before}`")

    st.divider()

    # ── Cột trái: ảnh | Cột phải: LLM debug ──────────────────────────────────
    col_img, col_llm = st.columns([1, 1])

    with col_img:
        # Ảnh gửi lên GPT — chỉ khi visual fallback
        if visual_fallback and screenshot_b64:
            st.markdown("**📤 Ảnh gửi lên GPT** _(Visual Fallback)_")
            try:
                st.image(Image.open(BytesIO(base64.b64decode(screenshot_b64))), use_container_width=True)
            except Exception:
                st.warning("Không thể hiển thị ảnh")
        else:
            st.markdown("**📝 GPT quyết định từ text** _(không gửi ảnh)_")
            st.caption("Ảnh bên dưới chỉ để xem — không gửi lên GPT.")

        # Ảnh hiện tại của trang (annotated, luôn hiển thị)
        if annotated_b64:
            with st.expander("📸 Trạng thái trang (annotated)", expanded=not visual_fallback):
                try:
                    st.image(Image.open(BytesIO(base64.b64decode(annotated_b64))), use_container_width=True)
                except Exception:
                    st.warning("Không thể hiển thị ảnh annotated")
        else:
            st.warning("📷 Ảnh không khả dụng (screenshot fail)")

    with col_llm:
        st.markdown("**🤖 LLM điều khiển browser**")

        # Badge mode
        if visual_fallback:
            st.warning(f"⚠️ Element `{ref}` không có mô tả → đã gửi ảnh để xác nhận")
        else:
            st.caption(f"Phân tích từ snapshot (text only)")

        # Lý do LLM chọn
        if reason:
            st.info(f"💬 **Lý do:** {reason}")

        # Action thực thi
        if action_type == "click":
            st.success(f"**→ CLICK** element `{ref}`")
        elif action_type == "type":
            st.success(f"**→ TYPE** `{action.get('text', '')}` vào `{ref}`")
        elif action_type == "wait":
            st.success(f"**→ WAIT** `{action.get('ms', 1000)}ms`")
        elif action_type == "done":
            msg = action.get("message", "Hoàn thành")
            st.success(f"**✅ DONE:** {msg}")

        if action_type == "ask":
            ask_type = action.get("ask_type", "question")
            if ask_type == "error":
                st.error(f"❌ **Lỗi cần xử lý:**\n\n{action.get('message', '')}")
            else:
                st.warning(f"🙋 **Agent cần thêm thông tin:**\n\n{action.get('message', '')}")

        if error:
            st.error(f"⚠️ **Lỗi thực thi:** {error}")

        # Snapshot gửi LLM — hiển thị ngay để debug
        st.markdown("**📋 Snapshot (text LLM thấy):**")
        st.code(
            snapshot[:2000] + ("\n… [cắt bớt]" if len(snapshot) > 2000 else ""),
            language="text",
        )

        # Prompt gửi lên GPT — để kiểm tra thiếu dữ liệu không
        with st.expander("📤 Prompt gửi lên GPT", expanded=bool(error)):
            if llm_prompt:
                st.code(llm_prompt, language="text")
            else:
                st.caption("_(Chưa có prompt)_")

        # Raw GPT response
        with st.expander("📄 Raw JSON response từ GPT"):
            if llm_raw:
                try:
                    st.json(json.loads(llm_raw))
                except Exception:
                    st.code(llm_raw, language="json")
            else:
                st.caption("_(Không có response)_")

    # ── Trạng thái sau action (full width) ───────────────────────────────────
    if post_screenshot_path or post_snapshot:
        with st.expander("🔍 Trạng thái sau action (post-click)", expanded=bool(error)):
            if post_screenshot_path:
                try:
                    with open(post_screenshot_path, "rb") as f:
                        img_post = Image.open(BytesIO(f.read()))
                        img_post.load()
                    st.image(img_post, use_container_width=True, caption=post_screenshot_path)
                except Exception:
                    st.caption(f"_(Ảnh: `{post_screenshot_path}`)_")
            if post_snapshot:
                st.code(
                    post_snapshot[:2000] + ("\n… [cắt bớt]" if len(post_snapshot) > 2000 else ""),
                    language="text",
                )


# ─── Helper: lưu lịch sử chat ra file ────────────────────────────────────────
def save_chat_to_file():
    """Lưu toàn bộ messages trong session ra JSON trong artifacts/."""
    session = SessionState(goal="chat_export")
    try:
        path = session.save_chat_history(st.session_state.get("messages", []))
        return path
    except Exception:
        return None


# ─── Add message ──────────────────────────────────────────────────────────────
def add_message(
    role: str,
    content: str = "",
    msg_type: str = "text",
    step_data: dict | None = None,
):
    msg = {"role": role, "type": msg_type, "content": content}
    if step_data is not None:
        msg["step_data"] = step_data
    st.session_state["messages"].append(msg)


# ─── Helpers stream steps ────────────────────────────────────────────────────
def _stream_steps(gen, scenario: str, status) -> bool:
    """
    Iterate generator, render từng step.
    Trả về True nếu agent bị block (cần user trả lời), False nếu xong.
    Khi block: lưu gen vào session_state["blocked_gen"] và dừng.
    """
    step_count = 0
    for step in gen:
        step_count += 1
        step_dict = asdict(step)
        status.info(
            f"⚙️ **Bước {step.step}** — `{step.action_type.upper()}`"
            + (f" `{step.ref}`" if step.ref else "")
        )
        add_message("assistant", msg_type="step", step_data=step_dict)
        with st.chat_message("assistant"):
            render_step_card(step_dict)

        if step.is_blocked:
            # Agent cần thêm thông tin — lưu generator để resume sau
            st.session_state["blocked_gen"] = gen
            st.session_state["blocked_scenario"] = scenario
            status.warning("⏸️ Agent đang chờ phản hồi từ bạn...")
            return True  # blocked

        if step.is_done and scenario != "chang_login_autonomous":
            break

    # Kết thúc bình thường
    status.success(f"✅ Hoàn thành sau {step_count} bước!")
    add_message("assistant", f"✅ Hoàn thành sau **{step_count} bước**.")
    chat_path = save_chat_to_file()
    if chat_path:
        add_message("assistant", f"💾 Lịch sử đã lưu tại `{chat_path}`")
    return False  # done


# ─── Run agent và stream live ─────────────────────────────────────────────────
def run_and_stream(goal: str, scenario: str = "custom"):
    """Chạy agent, stream từng bước trực tiếp lên UI, lưu log sau khi xong."""
    api_key = get_api_key()
    add_message("user", f"**🎯 Goal:** {goal}")
    status = st.empty()
    status.info("🚀 Đang khởi động agent và mở browser...")

    blocked = False
    try:
        if scenario == "chang_login_autonomous":
            ctx = st.session_state.get("login_context") or None
            gen = run_chang_login_autonomous(
                api_key=api_key,
                context=ctx,
                max_steps=st.session_state.get("max_steps", 20),
            )
        else:
            gen = run_agent(
                goal=goal,
                api_key=api_key,
                max_steps=st.session_state.get("max_steps", 10),
            )
        blocked = _stream_steps(gen, scenario, status)

    except Exception as e:
        msg = _friendly_error(e)
        status.error(f"❌ {msg}")
        add_message("assistant", f"❌ {msg}")
    finally:
        if not blocked:
            st.session_state["agent_running"] = False


# ─── Resume agent sau khi user trả lời ───────────────────────────────────────
def resume_from_ask(answer: str):
    """Gọi gen.send(answer) để tiếp tục agent đang bị block, rồi stream tiếp."""
    gen = st.session_state.pop("blocked_gen")
    scenario = st.session_state.pop("blocked_scenario", "chang_login_autonomous")

    add_message("user", f"💬 {answer}")
    status = st.empty()
    status.info("▶️ Tiếp tục agent với thông tin mới...")

    blocked = False
    try:
        # Gửi câu trả lời vào generator đang pause
        try:
            first_step = gen.send(answer)
        except StopIteration:
            status.warning("⚠️ Agent đã đạt giới hạn số bước mà chưa hoàn thành.")
            add_message("assistant", "⚠️ Agent đã đạt giới hạn số bước. Hãy tăng **Số bước tối đa** ở sidebar và chạy lại.")
            save_chat_to_file()
            return

        # Stream bước đầu tiên sau send(), rồi tiếp tục bình thường
        def _gen_from_first(first, rest):
            yield first
            yield from rest

        blocked = _stream_steps(_gen_from_first(first_step, gen), scenario, status)

    except Exception as e:
        msg = _friendly_error(e)
        status.error(f"❌ {msg}")
        add_message("assistant", f"❌ {msg}")
    finally:
        if not blocked:
            st.session_state["agent_running"] = False


# ─── Session state init ───────────────────────────────────────────────────────
if "messages" not in st.session_state:
    st.session_state["messages"] = []
if "agent_running" not in st.session_state:
    st.session_state["agent_running"] = False
if "max_steps" not in st.session_state:
    st.session_state["max_steps"] = 20

# ─── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ Cài đặt")
    st.divider()

    # Trạng thái API key
    if api_key_valid():
        st.success("✅ API Key đã load từ `.env`")
    else:
        st.error("❌ Chưa có API Key")
        st.code("OPENAI_API_KEY=sk-...", language="bash")
        st.caption("Điền vào file `LLM_base/.env` rồi restart app")

    st.divider()
    st.subheader("🎯 Kịch bản sẵn")

    st.session_state["max_steps"] = st.slider(
        "Số bước tối đa",
        min_value=3,
        max_value=30,
        value=st.session_state["max_steps"],
    )

    with st.expander("🔐 Thông tin đăng nhập (Autonomous)"):
        login_email = st.text_input("Email", key="login_email")
        login_password = st.text_input("Password", type="password", key="login_password")
        if login_email or login_password:
            ctx: dict = {}
            if login_email:
                ctx["email"] = login_email
            if login_password:
                ctx["password"] = login_password
            st.session_state["login_context"] = ctx
        else:
            st.session_state["login_context"] = {}

    if st.button(
        "🤖 Autonomous: Đăng nhập Chang",
        use_container_width=True,
        type="primary",
        disabled=st.session_state["agent_running"] or not api_key_valid(),
    ):
        st.session_state["pending_run"] = {
            "goal": CHANG_AUTONOMOUS_GOAL,
            "scenario": "chang_login_autonomous",
        }

    st.divider()

    col_save, col_clear = st.columns(2)
    with col_save:
        if st.button(
            "💾 Lưu chat",
            use_container_width=True,
            disabled=not st.session_state["messages"],
        ):
            path = save_chat_to_file()
            if path:
                st.success(f"Đã lưu:\n`{Path(path).name}`")

    with col_clear:
        if st.button(
            "🗑️ Xóa",
            use_container_width=True,
            disabled=st.session_state["agent_running"],
        ):
            st.session_state["messages"] = []
            st.rerun()

    st.divider()
    st.caption("**Hướng dẫn:**")
    st.caption("1. Điền API key vào `LLM_base/.env`")
    st.caption("2. Nhấn nút kịch bản hoặc nhập goal trong chat")
    st.caption("3. Xem GPT phân tích từng bước live")
    st.caption(f"\n🔗 {CHANG_URL}")

    # Hiển thị file log gần nhất
    st.divider()
    st.caption("**Logs gần đây:**")
    try:
        logs = sorted(ARTIFACTS_DIR.rglob("session.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
        for log in logs:
            # Hiển thị đường dẫn tương đối từ artifacts/
            rel = log.relative_to(ARTIFACTS_DIR)
            st.caption(f"📄 `{rel}`")
    except Exception:
        pass

# ─── Main UI ──────────────────────────────────────────────────────────────────
st.title("🤖 Chang AI Browser Agent")
st.caption(
    "Điều khiển trình duyệt tự động · GPT-4o mini Vision · agent-browser · "
    "Mỗi bước hiển thị: ảnh annotated + lý do GPT + action thực thi"
)

# Hiển thị lịch sử messages
for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        if msg["type"] == "text":
            st.markdown(msg["content"])
        elif msg["type"] == "step":
            render_step_card(msg["step_data"])

# ─── Trigger kịch bản từ sidebar ─────────────────────────────────────────────
if "pending_run" in st.session_state and not st.session_state["agent_running"]:
    pending = st.session_state.pop("pending_run")
    st.session_state["agent_running"] = True
    run_and_stream(pending["goal"], scenario=pending["scenario"])
    st.rerun()

# ─── Chat input ───────────────────────────────────────────────────────────────
if "blocked_gen" in st.session_state:
    # Agent đang chờ — chat input dùng để trả lời câu hỏi
    st.info("🙋 **Agent đang chờ phản hồi.** Nhập câu trả lời bên dưới để tiếp tục.")
    if answer := st.chat_input("💬 Nhập câu trả lời cho agent..."):
        resume_from_ask(answer)
        st.rerun()
elif prompt := st.chat_input(
    "Nhập mục tiêu cho agent...",
    disabled=st.session_state["agent_running"],
):
    if not api_key_valid():
        st.error("Vui lòng điền OPENAI_API_KEY vào file `LLM_base/.env` trước!")
    else:
        st.session_state["agent_running"] = True
        run_and_stream(prompt, scenario="custom")
        st.rerun()

# ─── Footer ───────────────────────────────────────────────────────────────────
st.divider()
st.caption(f"Logs & screenshots → `{ARTIFACTS_DIR}`")
