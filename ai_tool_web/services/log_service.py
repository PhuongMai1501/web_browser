"""
services/log_service.py — Structured JSONL logger.

3 loại log:
  1. System log: startup, healthcheck, worker lifecycle, infra error
  2. Session log: step, planner output, browser action, ask/done/fail
  3. Error log: exception, retry fail, upload fail (ghi vào system log, level=ERROR)

Local write real-time + batch upload lên DSC.
"""

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

# ── Secret masking ────────────────────────────────────────────────────────────

_MASK_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "***API_KEY***"),
    (re.compile(r"Bearer\s+[a-zA-Z0-9._\-]+"), "Bearer ***"),
    (re.compile(r'(password["\s:=]+)[^\s",}]+', re.I), r"\1***"),
    (re.compile(r'(token["\s:=]+)[^\s",}]+', re.I), r"\1***"),
    (re.compile(r'(cookie["\s:=]+)[^\s",}]+', re.I), r"\1***"),
    (re.compile(r'(authorization["\s:=]+)[^\s",}]+', re.I), r"\1***"),
    (re.compile(r'(secret["\s:=]+)[^\s",}]+', re.I), r"\1***"),
]


def mask_secrets(text: str) -> str:
    """Mask API keys, passwords, tokens, cookies trong text."""
    for pattern, replacement in _MASK_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _safe_json(obj: dict) -> str:
    """JSON serialize với fallback cho non-serializable values."""
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps({"error": "serialize_failed", "ts": _now()})


# ── LogService ────────────────────────────────────────────────────────────────

class LogService:
    """
    Structured JSONL logger — ghi local + batch upload DSC.

    Usage:
        log_svc = LogService("worker-1", artifacts_root, uploader)
        log_svc.log_system("INFO", "startup", version="1.0")
        log_svc.log_session(session_id, "step", step=1, action="click")
        log_svc.log_error("planner_error", "timeout", session_id="abc")
        log_svc.flush_upload()
    """

    def __init__(
        self,
        worker_id: str,
        artifacts_root: Path,
        uploader=None,
        max_buffer: int = 100,
    ) -> None:
        self._worker_id = worker_id
        self._artifacts = artifacts_root
        self._uploader = uploader
        self._max_buffer = max_buffer
        self._lock = threading.Lock()

        # System log file
        self._system_dir = self._artifacts / "logs" / "system"
        self._system_dir.mkdir(parents=True, exist_ok=True)
        self._system_file = self._system_dir / "current.jsonl"

        # Session log dir
        self._session_dir = self._artifacts / "logs" / "session"
        self._session_dir.mkdir(parents=True, exist_ok=True)

        # Buffer for batch upload
        self._system_buffer: list[dict] = []

    # ── Public API ────────────────────────────────────────────────────────

    def log_system(self, level: str, log_type: str, **extra) -> None:
        """System log: startup, healthcheck, worker lifecycle, infra error."""
        entry = {
            "ts": _now(),
            "level": level,
            "type": log_type,
            "worker_id": self._worker_id,
            **extra,
        }
        self._write_local(self._system_file, entry)
        with self._lock:
            self._system_buffer.append(entry)
            if len(self._system_buffer) >= self._max_buffer:
                self._flush_upload_locked()

    def log_session(
        self,
        session_id: str,
        log_type: str,
        step: int | None = None,
        **extra,
    ) -> None:
        """Session log: step, planner output, browser action, ask/done/fail."""
        entry = {
            "ts": _now(),
            "level": "INFO",
            "type": log_type,
            "session_id": session_id,
            "worker_id": self._worker_id,
        }
        if step is not None:
            entry["step"] = step
        entry.update(extra)
        path = self._session_dir / f"{session_id}.jsonl"
        self._write_local(path, entry)

    def log_error(
        self,
        error_type: str,
        error_msg: str,
        session_id: str | None = None,
        **extra,
    ) -> None:
        """Error log: exception, retry fail, upload fail. Upload ngay."""
        entry = {
            "ts": _now(),
            "level": "ERROR",
            "type": error_type,
            "error": mask_secrets(str(error_msg)[:500]),
            "worker_id": self._worker_id,
        }
        if session_id:
            entry["session_id"] = session_id
        entry.update(extra)
        self._write_local(self._system_file, entry)
        # Error → flush upload ngay
        with self._lock:
            self._system_buffer.append(entry)
            self._flush_upload_locked()

    def flush_upload(self) -> None:
        """Upload buffered system logs to DSC (thread-safe)."""
        with self._lock:
            self._flush_upload_locked()

    def upload_session_log(self, session_id: str) -> str | None:
        """Upload session log file lên DSC. Trả về CDN URL hoặc None."""
        if not self._uploader:
            return None
        path = self._session_dir / f"{session_id}.jsonl"
        if not path.exists():
            return None
        from services.artifact_uploader import build_artifact_remote_path
        remote = build_artifact_remote_path(session_id, "log_session.jsonl")
        return self._uploader.upload_artifact(str(path), remote)

    # ── Internal ──────────────────────────────────────────────────────────

    def _write_local(self, path: Path, entry: dict) -> None:
        """Append 1 JSONL line to file."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(_safe_json(entry) + "\n")
        except Exception as e:
            _log.warning("Log write failed %s: %s", path.name, e)

    def _flush_upload_locked(self) -> None:
        """Upload system log buffer. Must be called with self._lock held."""
        if not self._system_buffer or not self._uploader:
            self._system_buffer.clear()
            return
        try:
            # Upload current system log file
            date = _date_str()
            remote_dir = f"public/logs/system/{date}/{self._worker_id}"
            remote_path = f"{remote_dir}/log_system.jsonl"
            self._uploader.upload_artifact(
                str(self._system_file), remote_path,
            )
            self._system_buffer.clear()
        except Exception as e:
            _log.warning("System log upload failed: %s", e)


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: LogService | None = None


def get_log_service(
    worker_id: str = "",
    artifacts_root: Path | None = None,
    uploader=None,
) -> LogService:
    """Get or create LogService singleton."""
    global _instance
    if _instance is None:
        if not worker_id or artifacts_root is None:
            raise RuntimeError("LogService not initialized — call with worker_id + artifacts_root first")
        from config import LOG_MAX_BUFFER_SIZE
        _instance = LogService(
            worker_id=worker_id,
            artifacts_root=artifacts_root,
            uploader=uploader,
            max_buffer=LOG_MAX_BUFFER_SIZE,
        )
    return _instance
