"""Action: upload_html_source — lấy HTML source trang hiện tại, upload CDN.

Use case: user query "lấy mã nguồn HTML trang X" → flow navigate đến trang
+ action này → CDN URL nằm trong result.artifacts.downloaded_files[].

Flow:
  1. eval_js(`document.documentElement.outerHTML`) — lấy HTML toàn trang
  2. JSON unwrap (agent-browser bọc string trong JSON)
  3. Sanity check size (< MAX_HTML_BYTES, default 10MB)
  4. Build filename — auto-gen nếu step.filename rỗng
  5. Write tmp file UTF-8 vào downloads dir (cùng dir với upload_download)
  6. Upload qua ArtifactUploader giữ tên file
  7. Delete local sau upload thành công
  8. Return ActionResult với downloaded_filename + downloaded_cdn_url
     (reuse field có sẵn — job_handler tự collect vào result.artifacts)

YAML usage:

    - action: upload_html_source
      format: outer_html           # outer_html (default) | inner_html
      filename: "page.html"        # optional, auto-gen nếu None
      remote_dir: "..."            # optional override CDN path
      note: "Lấy source HTML trang chi tiết"

Config qua env:
- AGENT_BROWSER_DOWNLOADS_DIR — dir tmp ghi file trước upload (cùng upload_download)
- UPLOAD_HTML_MAX_BYTES — limit size HTML (default 10485760 = 10MB)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from ..action_registry import ActionResult, action

_log = logging.getLogger(__name__)

_MAX_HTML_BYTES = int(os.getenv("UPLOAD_HTML_MAX_BYTES", str(10 * 1024 * 1024)))
_DEFAULT_FORMAT = "outer_html"
_JS_OUTER_HTML = "document.documentElement.outerHTML"
_JS_INNER_HTML = "document.body && document.body.innerHTML"

# Smart wait: poll document.readyState until "complete" hoặc timeout, rồi sleep buffer.
# Đảm bảo HTML capture được sau khi trang load xong, không cần wait_for selector.
_DEFAULT_READY_TIMEOUT_MS = int(os.getenv("UPLOAD_HTML_READY_TIMEOUT_MS", "20000"))
_DEFAULT_EXTRA_WAIT_MS = int(os.getenv("UPLOAD_HTML_EXTRA_WAIT_MS", "1500"))
_READY_POLL_INTERVAL_S = 0.5

# Reuse downloads dir của upload_download để giữ behavior nhất quán
_CANDIDATE_DIRS = (
    "/root/Downloads",
    "/home/chromium/Downloads",
    "/home/chrome/Downloads",
    "/home/user/Downloads",
    "/app/downloads",
    "/workspace/downloads",
    "/tmp/downloads",
    "/tmp",
)


@action("upload_html_source")
def run_upload_html_source(rt, step) -> ActionResult:
    fmt = (step.format or _DEFAULT_FORMAT).strip().lower()
    if fmt not in ("outer_html", "inner_html"):
        return ActionResult(
            ok=False, action_type="upload_html_source",
            error=f"format='{step.format}' không hợp lệ (chỉ 'outer_html' | 'inner_html')",
        )
    js = _JS_OUTER_HTML if fmt == "outer_html" else _JS_INNER_HTML

    url_before = _safe_url(rt.browser)

    # 0. Smart wait — đợi document.readyState === "complete" (max step.timeout_ms
    # hoặc env hoặc 20s), rồi sleep buffer cho SPA mount component
    ready_timeout_ms = step.timeout_ms or _DEFAULT_READY_TIMEOUT_MS
    ready = _wait_for_page_ready(rt, ready_timeout_ms)
    if not ready:
        _log.warning(
            "[%s] upload_html_source: readyState timeout sau %dms — capture anyway",
            getattr(rt, "session_id", "?"), ready_timeout_ms,
        )
    if _DEFAULT_EXTRA_WAIT_MS > 0:
        time.sleep(_DEFAULT_EXTRA_WAIT_MS / 1000.0)

    # 1. eval_js lấy HTML — agent-browser bọc string trong JSON, cần unwrap
    try:
        raw = rt.browser.eval_js(js, timeout=15)
    except Exception as e:
        return ActionResult(
            ok=False, action_type="upload_html_source",
            error=f"eval_js fail: {e}",
            url_before=url_before,
        )

    html = _unwrap_json_string(raw)
    if not html:
        return ActionResult(
            ok=False, action_type="upload_html_source",
            error=f"eval_js return rỗng/non-string (raw={raw[:200]!r})",
            url_before=url_before,
        )

    # 2. Size check
    size = len(html.encode("utf-8"))
    if size > _MAX_HTML_BYTES:
        return ActionResult(
            ok=False, action_type="upload_html_source",
            error=(
                f"HTML quá lớn: {size} bytes > limit {_MAX_HTML_BYTES} "
                f"(env UPLOAD_HTML_MAX_BYTES override nếu cần)"
            ),
            url_before=url_before,
        )

    # 3. Build filename + tmp local path
    filename = _resolve_filename(step.filename, url_before, rt)
    tmp_dir = _resolve_downloads_dir()
    if tmp_dir is None:
        return ActionResult(
            ok=False, action_type="upload_html_source",
            error="Không tìm thấy downloads dir nào tồn tại để ghi file tmp",
            url_before=url_before,
        )
    local_path = tmp_dir / filename

    try:
        local_path.write_text(html, encoding="utf-8")
    except OSError as e:
        return ActionResult(
            ok=False, action_type="upload_html_source",
            error=f"Ghi tmp file {local_path} fail: {e}",
            url_before=url_before,
        )

    # 4. Upload via ArtifactUploader (lazy import như upload_download)
    cdn_url = _upload_to_cdn(rt, local_path, step.remote_dir)
    if not cdn_url:
        # Giữ file local để debug
        return ActionResult(
            ok=False, action_type="upload_html_source",
            error=f"Upload {filename} → CDN thất bại (xem worker log)",
            url_before=url_before,
        )

    # 5. Delete local file sau upload thành công
    try:
        local_path.unlink()
    except OSError as e:
        _log.warning("Không xóa được tmp file %s: %s", local_path, e)

    _log.info(
        "[%s] upload_html_source ok — %s (%d bytes) → %s",
        getattr(rt, "session_id", "?"), filename, size, cdn_url,
    )

    return ActionResult(
        ok=True, action_type="upload_html_source",
        downloaded_filename=filename,
        downloaded_cdn_url=cdn_url,
        url_before=url_before,
        url_after=url_before,
        reason=step.note or f"Captured HTML ({size} bytes) → {cdn_url}",
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _wait_for_page_ready(rt, timeout_ms: int) -> bool:
    """Poll document.readyState === 'complete' cho tới khi ready hoặc timeout.

    Return True nếu trang đã ready trong thời gian timeout, False nếu timeout.
    Mỗi 500ms = 1 CDP roundtrip; với default 20s = max 40 calls.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            raw = rt.browser.eval_js("document.readyState", timeout=3)
            state = _unwrap_json_string(raw) if isinstance(raw, str) else str(raw or "")
            if state == "complete":
                return True
        except Exception as e:
            _log.debug("readyState poll fail (sẽ retry): %s", e)
        time.sleep(_READY_POLL_INTERVAL_S)
    return False


def _unwrap_json_string(raw: str) -> str:
    """Agent-browser eval_js return JSON-encoded string (đôi khi 2 lớp).

    Logic giống browser_adapter._parse_json_output:
    - Nếu raw bắt đầu bằng " hoặc ' → json.loads(raw) → str
    - Nếu raw có thêm 1 lớp JSON nữa → loads lần 2
    - Nếu raw parse fail → trả raw nguyên.
    """
    if not isinstance(raw, str) or not raw:
        return ""
    s = raw.strip()
    # 1 lớp
    if s.startswith(('"', "'")):
        try:
            v = json.loads(s)
            if isinstance(v, str):
                # check 2 lớp
                if v.startswith(('"', "'")):
                    try:
                        v2 = json.loads(v)
                        if isinstance(v2, str):
                            return v2
                    except json.JSONDecodeError:
                        pass
                return v
        except json.JSONDecodeError:
            pass
    return s


def _resolve_filename(explicit: str | None, url: str, rt) -> str:
    """Build filename .html. Ưu tiên explicit; fallback auto-gen từ URL + ts."""
    if explicit:
        name = explicit.strip()
        if not name.lower().endswith(".html"):
            name += ".html"
        return _sanitize_filename(name)

    # Auto-gen: <host>_<path-slug>_<session>_<ts>.html
    host = "page"
    slug = ""
    if url:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            host = (parsed.hostname or "page").replace(".", "_")
            slug = parsed.path.strip("/").replace("/", "_")[:40]
        except Exception:
            pass

    session_id = (getattr(rt, "session_id", "") or "x")[:12]
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    parts = [host]
    if slug:
        parts.append(slug)
    parts.append(session_id)
    parts.append(ts)
    name = "_".join(parts) + ".html"
    return _sanitize_filename(name)


def _sanitize_filename(name: str) -> str:
    """Strip ký tự nguy hiểm; giới hạn length 200."""
    name = re.sub(r"[^\w\-.]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    if len(name) > 200:
        # Giữ đuôi .html nếu có
        ext = ".html" if name.lower().endswith(".html") else ""
        name = name[: 200 - len(ext)] + ext
    return name or "page.html"


def _resolve_downloads_dir() -> Path | None:
    """Tìm dir đầu tiên tồn tại trong _CANDIDATE_DIRS hoặc env override."""
    env_override = os.getenv("AGENT_BROWSER_DOWNLOADS_DIR", "").strip()
    if env_override:
        p = Path(env_override)
        if p.exists():
            return p
    for c in _CANDIDATE_DIRS:
        p = Path(c)
        if p.exists():
            return p
    return None


def _upload_to_cdn(rt, local_path: Path, remote_dir_override) -> str | None:
    """Upload file qua ArtifactUploader. Path mặc định cùng upload_download
    nhưng vào subdir 'source/' để phân biệt với file download thường."""
    try:
        from services.artifact_uploader import ArtifactUploader
    except ImportError as e:
        _log.error("ArtifactUploader import fail: %s", e)
        return None

    uploader = ArtifactUploader()
    session_id = getattr(rt, "session_id", "") or "unknown"
    date_str = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    task_id = getattr(rt, "task_id", "") or ""
    task_seg = f"{task_id}/" if task_id else ""
    default_remote_dir = (
        f"public/tool-web/prod/sessions/{date_str}/{task_seg}{session_id}/source"
    )
    remote_dir = remote_dir_override or default_remote_dir
    remote_path = f"{remote_dir.rstrip('/')}/{local_path.name}"
    return uploader.upload_artifact(str(local_path), remote_path)


def _safe_url(browser) -> str:
    try:
        return browser.get_current_url()
    except Exception:
        return ""
