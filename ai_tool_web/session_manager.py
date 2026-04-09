"""
session_manager.py - Session lifecycle management.

# DEPRECATED — Phase 1b replacement: state/session_store.py + state/event_store.py
# This file is kept for compatibility with api.py (Phase 1a).
# Do NOT use in new code. Will be removed after Phase 1b migration is complete.

Dùng asyncio.Queue để bridge giữa agent thread (sync) và SSE generator (async).
threading.Event cho ask/resume signaling trong agent thread.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional


SessionStatus = Literal["running", "blocked", "completed", "cancelled", "error"]

SESSION_TTL_SECONDS = 600       # cleanup sau 10 phút hoàn thành
ASK_TIMEOUT_SECONDS = 300       # tự cancel nếu 5 phút không resume
MAX_SESSION_SECONDS = 600       # hard cap toàn session


@dataclass
class SessionData:
    id: str
    status: SessionStatus
    scenario: str
    max_steps: int
    created_at: datetime = field(default_factory=datetime.now)
    blocked_at: Optional[datetime] = None
    blocked_message: Optional[str] = None
    completed_at: Optional[datetime] = None
    current_step: int = 0
    error_message: Optional[str] = None

    # Screenshot paths: step_number → file_path
    screenshot_paths: dict[int, str] = field(default_factory=dict)
    annotated_paths: dict[int, str] = field(default_factory=dict)

    # Event buffer cho SSE reconnect (Last-Event-ID)
    event_buffer: list[dict] = field(default_factory=list)
    MAX_BUFFER = 50

    # Asyncio coordination
    step_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: Optional[asyncio.Task] = None

    # Ask/resume: threading.Event vì agent chạy trong to_thread
    blocked_event: threading.Event = field(default_factory=threading.Event)
    answer_value: Optional[str] = None

    def add_to_buffer(self, event: dict) -> None:
        self.event_buffer.append(event)
        if len(self.event_buffer) > self.MAX_BUFFER:
            self.event_buffer.pop(0)

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.completed_at:
            return (self.completed_at - self.created_at).total_seconds()
        return None

    @property
    def is_expired(self) -> bool:
        if self.status in ("completed", "cancelled", "error") and self.completed_at:
            return (datetime.now() - self.completed_at).total_seconds() > SESSION_TTL_SECONDS
        return False


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionData] = {}
        self._lock = asyncio.Lock()

    def create(self, scenario: str, max_steps: int) -> SessionData:
        session_id = str(uuid.uuid4())
        sess = SessionData(
            id=session_id,
            status="running",
            scenario=scenario,
            max_steps=max_steps,
        )
        self._sessions[session_id] = sess
        return sess

    def get(self, session_id: str) -> Optional[SessionData]:
        return self._sessions.get(session_id)

    def get_running(self) -> Optional[SessionData]:
        """Trả về session đang running/blocked nếu có."""
        for sess in self._sessions.values():
            if sess.status in ("running", "blocked"):
                return sess
        return None

    def mark_blocked(self, session_id: str, message: str) -> None:
        sess = self._sessions.get(session_id)
        if sess:
            sess.status = "blocked"
            sess.blocked_at = datetime.now()
            sess.blocked_message = message

    def mark_completed(self, session_id: str) -> None:
        sess = self._sessions.get(session_id)
        if sess:
            sess.status = "completed"
            sess.completed_at = datetime.now()

    def mark_error(self, session_id: str, message: str) -> None:
        sess = self._sessions.get(session_id)
        if sess:
            sess.status = "error"
            sess.completed_at = datetime.now()
            sess.error_message = message

    def mark_cancelled(self, session_id: str) -> None:
        sess = self._sessions.get(session_id)
        if sess:
            sess.status = "cancelled"
            sess.completed_at = datetime.now()

    def cleanup_expired(self) -> int:
        """Xoá sessions đã hết TTL. Trả về số session đã xoá."""
        expired = [sid for sid, s in self._sessions.items() if s.is_expired]
        for sid in expired:
            del self._sessions[sid]
        return len(expired)

    def all_sessions(self) -> list[SessionData]:
        return list(self._sessions.values())


# Singleton instance — dùng trong api.py
session_manager = SessionManager()
