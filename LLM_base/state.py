"""
state.py - Quản lý trạng thái session và lưu log.
"""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path


ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
ARTIFACTS_DIR.mkdir(exist_ok=True)


@dataclass
class StepRecord:
    step: int
    goal: str
    snapshot: str
    screenshot_path: str
    screenshot_b64: str
    annotated_screenshot_b64: str       # Screenshot có đánh số label element
    action: dict
    llm_prompt: str = ""                 # Prompt text gửi lên GPT (không gồm base64 ảnh)
    llm_raw_response: str = ""          # Raw JSON string GPT trả về
    url_before: str = ""
    url_after: str = ""
    page_title: str = ""                # Tiêu đề trang tại thời điểm chụp snapshot
    annotated_screenshot_path: str = "" # Đường dẫn screenshot có đánh số label element
    post_snapshot: str = ""             # Snapshot sau khi thực thi action
    post_screenshot_path: str = ""      # Đường dẫn screenshot sau action
    post_page_title: str = ""           # Tiêu đề trang sau khi thực thi action
    error: str = ""
    visual_fallback_used: bool = False  # True khi element không có mô tả → gửi ảnh lên GPT
    is_blocked: bool = False            # True khi LLM cần hỏi thêm thông tin từ user (action=ask)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def action_type(self) -> str:
        return self.action.get("action", "unknown")

    @property
    def reason(self) -> str:
        return self.action.get("reason", "")

    @property
    def ref(self) -> str:
        return self.action.get("ref", "")

    @property
    def is_done(self) -> bool:
        return self.action_type == "done"

    @property
    def done_message(self) -> str:
        return self.action.get("message", "Hoàn thành")


class SessionState:
    """Quản lý toàn bộ lịch sử các bước trong một session."""

    def __init__(self, goal: str, session_id: str = ""):
        self.goal = goal
        self.session_id = session_id
        self.steps: list[StepRecord] = []
        self.started_at = datetime.now().isoformat()

    def add_step(self, record: StepRecord):
        self.steps.append(record)

    def save_visual_fallback_log(self, entries: list[dict], log_dir: Path | None = None) -> str:
        """Lưu riêng log các lần gửi ảnh lên GPT (visual fallback) để dễ kiểm tra."""
        if not entries:
            return ""
        target = log_dir or ARTIFACTS_DIR
        target.mkdir(parents=True, exist_ok=True)
        log_path = target / "vfb.json"
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "goal": self.goal,
                    "saved_at": datetime.now().isoformat(),
                    "total_vfb_calls": len(entries),
                    "entries": entries,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        return str(log_path)

    def save_log(self, log_dir: Path | None = None) -> str:
        """Lưu log đầy đủ ra file JSON, trả về đường dẫn."""
        target = log_dir or ARTIFACTS_DIR
        target.mkdir(parents=True, exist_ok=True)
        log_path = target / "session.json"

        log_data = {
            "session_id": self.session_id,
            "goal": self.goal,
            "started_at": self.started_at,
            "finished_at": datetime.now().isoformat(),
            "total_steps": len(self.steps),
            "steps": [
                {
                    k: v
                    for k, v in asdict(s).items()
                    if k not in ("screenshot_b64", "annotated_screenshot_b64")
                }
                for s in self.steps
            ],
        }

        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(log_data, f, ensure_ascii=False, indent=2)

        return str(log_path)

    def save_chat_history(self, messages: list[dict]) -> str:
        """Lưu toàn bộ lịch sử chat Streamlit ra file JSON."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        chat_path = ARTIFACTS_DIR / f"chat_{timestamp}.json"

        # Bỏ base64 ảnh để file không quá nặng
        clean_messages = []
        for msg in messages:
            clean = {k: v for k, v in msg.items() if k != "step_data"}
            if "step_data" in msg:
                step = msg["step_data"]
                clean["step_data"] = {
                    k: v
                    for k, v in step.items()
                    if k not in ("screenshot_b64", "annotated_screenshot_b64")
                }
            clean_messages.append(clean)

        with open(chat_path, "w", encoding="utf-8") as f:
            json.dump(
                {"saved_at": datetime.now().isoformat(), "messages": clean_messages},
                f,
                ensure_ascii=False,
                indent=2,
            )

        return str(chat_path)

    @property
    def last_step(self) -> StepRecord | None:
        return self.steps[-1] if self.steps else None

    @property
    def is_finished(self) -> bool:
        return bool(self.last_step and self.last_step.is_done)
