"""
services/artifact_uploader.py — Upload screenshots và artifacts lên CDN.

Flow:
  Worker chụp PNG local → upload_screenshot() → CDN URL
  Sau khi done/failed  → upload_artifact()    → CDN URL cho result.json, session.jsonl

Config (từ env):
  UPLOAD_URL     = http://upload.dsc.net
  PUBLIC_CDN_URL = https://cdn.fstats.ai
  UPLOAD_BUCKET  = changchatbot
  UPLOAD_KEY     = <secret>
  UPLOAD_SECRET  = <secret>

Upload policy (tiết kiệm bandwidth):
  - LUÔN upload: step là ask / done / has_error
  - UPLOAD khi URL thay đổi (redirect xảy ra)
  - BỎ QUA: step wait thông thường, step click/type thành công không có redirect
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

_log = logging.getLogger(__name__)

# Timeout cho 1 request upload (giây)
_UPLOAD_TIMEOUT_S = 15


def _cfg(key: str, default: str = "") -> str:
    """Đọc config từ env — không phụ thuộc module config.py."""
    return os.getenv(key, default)


def _upload_enabled() -> bool:
    return bool(_cfg("UPLOAD_URL") and _cfg("UPLOAD_KEY") and _cfg("UPLOAD_SECRET"))


class ArtifactUploader:
    """
    Upload files lên internal upload server, trả về public CDN URL.

    Không raise exception — lỗi upload được log và trả về None.
    Worker vẫn tiếp tục bình thường nếu upload thất bại.
    """

    def __init__(self) -> None:
        self._upload_url = _cfg("UPLOAD_URL")
        self._cdn = _cfg("PUBLIC_CDN_URL").rstrip("/")
        self._bucket = _cfg("UPLOAD_BUCKET", "changchatbot")
        self._key = _cfg("UPLOAD_KEY")
        self._secret = _cfg("UPLOAD_SECRET")

    # ── Public API ─────────────────────────────────────────────────────────────

    def upload_screenshot(
        self,
        local_path: str,
        session_id: str,
        step: int,
        suffix: str = "",
    ) -> str | None:
        """
        Upload 1 screenshot PNG.

        dir:  tool-web/prod/sessions/{date}/{session_id}/screenshots
        file: step-{step:03d}{suffix}.png  (keepOriginalName=true)
        Trả về CDN URL hoặc None nếu fail.
        Xóa local file sau khi upload thành công.
        """
        date_str = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        remote_dir = f"public/tool-web/prod/sessions/{date_str}/{session_id}/screenshots"
        # Đổi tên file local thành step-{n}{suffix}.png trước khi upload
        target_name = f"step-{step:03d}{suffix}.png"
        local_path = _rename_for_upload(local_path, target_name)
        if local_path is None:
            return None
        return self._upload_file(local_path, remote_dir, content_type="image/png", cleanup=True)

    def upload_artifact(self, local_path: str, remote_path: str) -> str | None:
        """
        Upload 1 artifact file (result.json, session.jsonl, ...).
        remote_path: full path kể cả filename, dùng để tách dir và filename.
        KHÔNG xóa local file (cần giữ lại để serve fallback).
        Trả về CDN URL hoặc None nếu fail.
        """
        remote_dir = str(Path(remote_path).parent)
        content_type = "application/json" if local_path.endswith(".json") else "application/x-ndjson"
        return self._upload_file(local_path, remote_dir, content_type=content_type, cleanup=False)

    def should_upload(self, record) -> bool:
        """
        Upload policy.

        Upload khi:
          - Agent bị block (ask)
          - Agent hoàn thành (done)
          - Step có lỗi
          - URL thay đổi (redirect xảy ra — dấu hiệu action quan trọng)

        Bỏ qua:
          - Step wait
          - Step click/type thông thường không có redirect
        """
        action_type = ""
        if isinstance(record.action, dict):
            action_type = record.action.get("action", "")

        if record.is_blocked or record.is_done:
            return True
        if getattr(record, "error", ""):
            return True
        url_changed = (
            getattr(record, "url_before", "")
            and getattr(record, "url_after", "")
            and record.url_before != record.url_after
        )
        if url_changed:
            return True
        if action_type == "wait":
            return False
        return False

    # ── Internal ───────────────────────────────────────────────────────────────

    def _upload_file(
        self,
        local_path: str,
        remote_dir: str,
        content_type: str,
        cleanup: bool,
    ) -> str | None:
        """
        POST /api/v1/file/upload
        Auth qua HTTP headers (không phải form data).
        remote_dir: subdirectory trên bucket (không bao gồm filename).
        """
        if not Path(local_path).exists():
            _log.warning("Upload skipped — file not found: %s", local_path)
            return None

        filename = Path(local_path).name
        endpoint = self._upload_url.rstrip("/") + "/api/v1/file/upload"

        try:
            with open(local_path, "rb") as f:
                resp = requests.post(
                    endpoint,
                    headers={
                        "upload-bucket": self._bucket,
                        "upload-key": self._key,
                        "upload-secret": self._secret,
                    },
                    files={"file": (filename, f, content_type)},
                    data={
                        "dir": remote_dir,
                        "keepOriginalName": "true",
                    },
                    timeout=_UPLOAD_TIMEOUT_S,
                    proxies={"http": None, "https": None},
                )
            resp.raise_for_status()

            # Thử lấy URL từ response body, fallback build thủ công
            cdn_url = self._parse_cdn_url(resp, remote_dir, filename)
            _log.info("Uploaded %s → %s", filename, cdn_url)

            if cleanup:
                _cleanup(local_path)

            return cdn_url

        except requests.Timeout:
            _log.warning("Upload timeout (%ds): %s", _UPLOAD_TIMEOUT_S, local_path)
        except requests.HTTPError as e:
            _log.warning("Upload HTTP error %s: %s | body: %s",
                         e.response.status_code, local_path, e.response.text[:300])
        except Exception as e:
            _log.warning("Upload failed (%s): %s", type(e).__name__, local_path)

        return None

    def _parse_cdn_url(self, resp, remote_dir: str, filename: str) -> str:
        """Lấy CDN URL từ response body, fallback build từ cdn + bucket + dir + filename."""
        try:
            body = resp.json()
            for field in ("url", "cdnUrl", "cdn_url", "publicUrl", "fileUrl"):
                if val := body.get(field):
                    if val.startswith("http"):
                        return val
                    # Relative path (không có bucket) → thêm bucket vào
                    return f"{self._cdn}/{self._bucket}/{val.lstrip('/')}"
        except Exception:
            pass
        # Fallback: build thủ công
        return f"{self._cdn}/{self._bucket}/{remote_dir}/{filename}"


def _cleanup(local_path: str) -> None:
    """Xóa file local sau upload thành công."""
    try:
        os.remove(local_path)
    except Exception as e:
        _log.debug("Cleanup failed for %s: %s", local_path, e)


def _rename_for_upload(local_path: str, target_name: str) -> str | None:
    """
    Đổi tên file thành target_name trong cùng thư mục để keepOriginalName hoạt động đúng.
    Trả về đường dẫn mới, hoặc None nếu lỗi.
    """
    src = Path(local_path)
    if not src.exists():
        _log.warning("Rename skipped — file not found: %s", local_path)
        return None
    dst = src.parent / target_name
    try:
        src.rename(dst)
        return str(dst)
    except Exception as e:
        _log.warning("Rename failed %s → %s: %s", src.name, target_name, e)
        return local_path  # fallback: dùng tên gốc


def build_artifact_remote_path(session_id: str, filename: str) -> str:
    """Tạo remote path chuẩn cho artifact (result.json, session.jsonl)."""
    date_str = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    return f"public/tool-web/prod/sessions/{date_str}/{session_id}/{filename}"


# Singleton — dùng chung trong cùng process
_uploader: ArtifactUploader | None = None


def get_uploader() -> ArtifactUploader | None:
    """
    Trả về uploader singleton nếu env vars đủ, else None.
    Worker check None để biết upload bị tắt.
    """
    if not _upload_enabled():
        return None
    global _uploader
    if _uploader is None:
        _uploader = ArtifactUploader()
    return _uploader
