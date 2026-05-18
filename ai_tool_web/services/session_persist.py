"""
services/session_persist.py — Tạo result.json và session.jsonl khi session kết thúc.

Lưu local vào ARTIFACTS_ROOT/sessions/{session_id}/
Sau đó upload lên CDN (nếu UPLOAD_ENABLED), ghi CDN URL vào artifacts section.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from config import ARTIFACTS_ROOT

_log = logging.getLogger(__name__)


def get_session_artifact_dir(session_id: str) -> Path:
    """Thư mục lưu artifact của session: ARTIFACTS_ROOT/sessions/{session_id}/"""
    return ARTIFACTS_ROOT / "sessions" / session_id


def write_session_jsonl(session_id: str, events: list[dict], artifact_dir: Path) -> Path:
    """
    Ghi danh sách events ra file session.jsonl.
    Mỗi dòng là 1 JSON object: {type, ts, step, payload}.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / "session.jsonl"
    try:
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        _log.info(f"[{session_id}] session.jsonl written: {len(events)} events → {path}")
    except Exception as e:
        _log.error(f"[{session_id}] Failed to write session.jsonl: {e}")
    return path


def write_result_json(
    session_id: str,
    status: str,
    scenario: str,
    summary: str,
    url_after: str,
    total_steps: int,
    duration_seconds: float,
    finished_at: str,
    artifact_dir: Path,
    error_msg: str = "",
    uploader=None,
    extracted_data: dict | None = None,
    session_json_url: str = "",
    task_id: str = "",
) -> tuple[Path, str]:
    """
    Ghi result.json tổng kết session.

    Fields:
      session_id, status, scenario, summary, url_after,
      total_steps, duration_seconds, finished_at,
      error_msg (nếu failed),
      artifacts.log_path, artifacts.log_url (events log session.jsonl CDN),
      artifacts.session_json_url (rich diagnostic session.json CDN —
        chứa llm_prompt, llm_raw_response, action chosen từng step),
      data (nếu spec có output_schema + action extract_data đã chạy)

    uploader: ArtifactUploader instance hoặc None.
    extracted_data: dict từ action `extract_data` (nếu có) — merge vào
                    result["data"] để Sup Agent đọc theo output_schema.
    session_json_url: CDN URL của session.json đã upload bởi job_handler
                     `_copy_runner_session_json`. Rỗng nếu fail/skip.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = artifact_dir / "session.jsonl"
    result_path = artifact_dir / "result.json"

    # Upload session.jsonl trước để có URL cho result.json
    log_url = ""
    if uploader and jsonl_path.exists():
        from services.artifact_uploader import build_artifact_remote_path
        remote = build_artifact_remote_path(session_id, "session.jsonl", task_id=task_id)
        log_url = uploader.upload_artifact(str(jsonl_path), remote) or ""

    result = {
        "session_id": session_id,
        "status": status,
        "scenario": scenario,
        "summary": summary,
        "url_after": url_after,
        "total_steps": total_steps,
        "duration_seconds": round(duration_seconds, 1),
        "finished_at": finished_at,
        "artifacts": {
            "log_path": str(jsonl_path),
            "log_url": log_url,
            # CDN URL của session.json (rich diagnostic) — Sup Agent fetch
            # llm_prompt, llm_raw_response, action chosen từng step.
            # Rỗng nếu session.json không tìm thấy hoặc uploader=None.
            "session_json_url": session_json_url,
        },
    }
    if error_msg:
        result["error_msg"] = error_msg
    if extracted_data:
        result["data"] = extracted_data

    try:
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        _log.info(f"[{session_id}] result.json written → {result_path}")
    except Exception as e:
        _log.error(f"[{session_id}] Failed to write result.json: {e}")
        return result_path, ""

    # Upload result.json lên CDN — capture URL để caller lưu Redis.
    # K8s: API và Worker là 2 pod khác nhau, không share local FS → API phải
    # fetch result.json từ CDN (BUG-result-endpoint-404-k8s).
    result_cdn_url = ""
    if uploader:
        from services.artifact_uploader import build_artifact_remote_path
        remote = build_artifact_remote_path(session_id, "result.json", task_id=task_id)
        result_cdn_url = uploader.upload_artifact(str(result_path), remote) or ""

    return result_path, result_cdn_url
