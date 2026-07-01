"""Action: upload_download — wait file download xong, upload lên CDN, xóa local.

Flow:
  1. Snapshot file đã có trong downloads dir trước action
  2. Poll dir cho file MỚI (mtime > start) chưa có trong snapshot
  3. Filter theo extensions (nếu set)
  4. Verify file stable (size không đổi trong 1s) — tránh upload file đang ghi
  5. Upload lên CDN qua ArtifactUploader, giữ tên file gốc
  6. Delete local file sau upload thành công
  7. Trả về downloaded_filename + downloaded_cdn_url trong ActionResult →
     ghi vào step.action trong session.json

YAML usage:

    - action: upload_download
      extensions: [".doc", ".docx", ".pdf"]
      timeout_ms: 30000
      remote_dir: "public/tool-web/prod/sessions/{session_id}/downloads"
      note: "Upload file QCVN sau click tải"

Config qua env:
- AGENT_BROWSER_DOWNLOADS_DIR (default: "/root/Downloads")
- UPLOAD_URL / UPLOAD_KEY / UPLOAD_SECRET — cùng config với
  ArtifactUploader (artifact_uploader.py)
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from ..action_registry import ActionResult, action

_log = logging.getLogger(__name__)

_DEFAULT_DOWNLOAD_DIR = "/root/Downloads"
_DEFAULT_TIMEOUT_MS = 30000
_STABILITY_WAIT_S = 1.0
# Lookback window: file được coi là "mới" nếu mtime trong N giây qua. Đủ rộng
# để cover khoảng cách giữa step click và step upload_download (browser có thể
# download xong trước khi action upload_download bắt đầu poll).
_DEFAULT_LOOKBACK_S = 300.0
# Suffix cho file đang download (Chrome/Chromium)
_INCOMPLETE_SUFFIXES = (".crdownload", ".tmp", ".part", ".download")
# Candidate dirs khi default không tồn tại — agent-browser binary có thể save
# vào dir khác tùy K8s image config. Order: env override → home/Downloads →
# common Linux paths → tmp fallback.
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


@action("upload_download")
def run_upload_download(rt, step) -> ActionResult:
    download_dir, discovery_log = _resolve_download_dir()
    if download_dir is None:
        return ActionResult(
            ok=False, action_type="upload_download",
            error=(
                "Không tìm thấy downloads dir nào tồn tại. "
                f"Đã thử: {discovery_log}. "
                "Set env AGENT_BROWSER_DOWNLOADS_DIR trỏ đến dir thật."
            ),
        )

    timeout_s = (step.timeout_ms or _DEFAULT_TIMEOUT_MS) / 1000.0
    extensions_filter = _normalize_extensions(step.extensions)

    # File mới nhận diện qua mtime trong lookback window (default 300s),
    # không dùng "mtime > action_start" vì browser có thể download xong
    # TRƯỚC khi action upload_download bắt đầu poll (gap giữa step click
    # và step upload_download có thể vài chục giây).
    mtime_threshold = time.time() - _DEFAULT_LOOKBACK_S

    target = _wait_for_new_file(
        download_dir, extensions_filter, mtime_threshold, timeout_s,
    )
    if target is None:
        # Diagnostic: scan TẤT CẢ candidate dirs tìm file mới trong lookback
        # window — giúp xác định agent-browser thực tế save vào dir nào, kể
        # cả khi không phải dir đang dùng.
        diag = _diagnose_other_dirs(mtime_threshold, extensions_filter)
        return ActionResult(
            ok=False, action_type="upload_download",
            error=(
                f"Không tìm thấy file download mới trong {download_dir} "
                f"sau {timeout_s}s (filter: {extensions_filter or 'any'}). "
                f"Discovery: {discovery_log}. "
                f"Files mới ở dir khác: {diag or 'KHÔNG'}."
            ),
        )

    # Sanitize tên file trước khi upload:
    #  - Cắt hậu tố trùng " (N)" Chrome tự thêm khi file trùng tên đã tồn tại
    #    trong dir (vd "...324 (2).zip" → "...324.zip").
    #  - Đổi khoảng trắng còn lại → "-" (space thô làm CDN URL không truy cập được).
    target = _sanitize_download_name(target)

    cdn_url = _upload_to_cdn(rt, target, step.remote_dir)
    if not cdn_url:
        return ActionResult(
            ok=False, action_type="upload_download",
            error=f"Upload {target.name} → CDN thất bại (xem worker log)",
        )

    # Đưa link vào output chính (result.data) để Sup Agent đọc như "đầu ra" của
    # scenario (ngoài artifacts.downloaded_files). Merge để không đè data của
    # extract_data nếu scenario dùng cả hai.
    _write_output_holder(rt, target.name, cdn_url)

    # Delete local sau upload thành công
    try:
        target.unlink()
    except Exception as e:
        _log.warning("Không xóa được local file %s: %s", target, e)

    # Dọn file rác cùng loại còn sót trong dir (orphan từ session trước / bản
    # scenario click-only không qua upload_download). Tránh tích lũy khiến Chrome
    # thêm " (N)" cho lần tải SAU. Chỉ dọn khi có extensions_filter để không xóa
    # nhầm file khác loại (vd .html của upload_html_source).
    if extensions_filter:
        _cleanup_stale_files(download_dir, extensions_filter)

    return ActionResult(
        ok=True, action_type="upload_download",
        downloaded_filename=target.name,
        downloaded_cdn_url=cdn_url,
        reason=step.note or f"Upload {target.name} → CDN",
    )


def _cleanup_stale_files(download_dir: Path, extensions_filter: set[str]) -> None:
    """Xóa các file còn sót trong download_dir khớp extensions_filter (orphan).

    Worker đơn luồng → tại thời điểm này không có session khác đang tải, nên
    xóa file cùng loại còn lại là an toàn. Bỏ qua file đang download dở.
    """
    for p in download_dir.iterdir():
        try:
            if not p.is_file():
                continue
            low = p.name.lower()
            if any(low.endswith(s) for s in _INCOMPLETE_SUFFIXES):
                continue
            if p.suffix.lower() not in extensions_filter:
                continue
            p.unlink()
            _log.info("Dọn orphan download: %s", p.name)
        except OSError as e:
            _log.debug("Không xóa được orphan %s: %s", p, e)


def _write_output_holder(rt, filename: str, cdn_url: str) -> None:
    """Ghi link tải vào rt.output_holder["data"] → surface vào result.data.

    Merge vào dict data sẵn có (nếu có) thay vì đè. Nhiều download trong 1 flow:
    download_url = cái mới nhất, đồng thời gom vào list downloads[].
    """
    holder = getattr(rt, "output_holder", None)
    if holder is None:
        return
    data = holder.get("data")
    if not isinstance(data, dict):
        data = {}
    data["download_url"] = cdn_url
    data["filename"] = filename
    downloads = data.get("downloads")
    if not isinstance(downloads, list):
        downloads = []
    downloads.append({"filename": filename, "url": cdn_url})
    data["downloads"] = downloads
    holder["data"] = data


def _resolve_download_dir() -> tuple[Path | None, str]:
    """Resolve dir downloads agent-browser save vào.

    Order priority:
      1. env AGENT_BROWSER_DOWNLOADS_DIR (nếu set)
      2. _CANDIDATE_DIRS theo thứ tự, dir đầu tiên tồn tại
      3. None nếu không có dir nào tồn tại

    Returns:
        (Path resolved | None, log string mô tả các dir đã thử)
    """
    log_parts: list[str] = []
    env_override = os.getenv("AGENT_BROWSER_DOWNLOADS_DIR", "").strip()
    if env_override:
        p = Path(env_override)
        log_parts.append(f"env={env_override}({'OK' if p.exists() else 'MISSING'})")
        if p.exists():
            return p, " | ".join(log_parts)

    for candidate in _CANDIDATE_DIRS:
        p = Path(candidate)
        exists = p.exists()
        log_parts.append(f"{candidate}({'OK' if exists else 'no'})")
        if exists:
            return p, " | ".join(log_parts)

    return None, " | ".join(log_parts)


def _diagnose_other_dirs(start_ts: float, extensions_filter: set[str]) -> str:
    """Scan tất cả candidate dirs tìm file mới sau start_ts. Helper diagnostic
    khi action không tìm thấy file ở dir đang dùng — báo cáo dir khác có file
    mới không, để user biết agent-browser thực tế save ở đâu.

    Returns:
        String summary của file mới tìm thấy ở các dir khác, hoặc "" nếu không.
    """
    findings: list[str] = []
    seen: set[str] = set()

    # Scan cả env override + candidates + HOME/Downloads
    paths_to_scan: list[str] = []
    env_dir = os.getenv("AGENT_BROWSER_DOWNLOADS_DIR", "").strip()
    if env_dir:
        paths_to_scan.append(env_dir)
    paths_to_scan.extend(_CANDIDATE_DIRS)
    home_dir = os.getenv("HOME", "").strip()
    if home_dir:
        paths_to_scan.append(f"{home_dir}/Downloads")

    for path_str in paths_to_scan:
        if path_str in seen:
            continue
        seen.add(path_str)
        p = Path(path_str)
        if not p.exists() or not p.is_dir():
            continue
        try:
            new_files = []
            for f in p.iterdir():
                if not f.is_file():
                    continue
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                if mtime < start_ts:
                    continue
                lower = f.name.lower()
                if any(lower.endswith(s) for s in _INCOMPLETE_SUFFIXES):
                    new_files.append(f"{f.name}(downloading)")
                    continue
                if extensions_filter and f.suffix.lower() not in extensions_filter:
                    continue
                new_files.append(f.name)
            if new_files:
                findings.append(f"{path_str}: {new_files[:5]}")
        except OSError:
            continue

    # Scan rộng toàn filesystem qua `find` shell command — tìm file mới
    # ở dir nằm ngoài candidate list. Limit 5s timeout để không treo, skip
    # các pseudo-fs (/proc, /sys, /run) và log dirs.
    fs_findings = _find_new_files_global(start_ts, extensions_filter)
    if fs_findings:
        findings.append(f"find /: {fs_findings}")

    return " | ".join(findings) if findings else ""


def _find_new_files_global(start_ts: float, extensions_filter: set[str]) -> str:
    """Dùng `find` shell command scan toàn / tìm file mtime < 3 phút. Limit
    timeout 5s để không treo. Skip pseudo-fs.

    Returns:
        String list file path mới (max 10), hoặc "" nếu không tìm thấy/lỗi.
    """
    import subprocess

    # find / -mmin -3 -type f → tất cả regular file mtime < 3 phút
    # -size +1c để loại file rỗng. Limit -maxdepth 6 để tránh quét quá sâu.
    try:
        result = subprocess.run(
            [
                "find", "/",
                "-maxdepth", "6",
                "-mmin", "-3",
                "-type", "f",
                "-size", "+1c",
                "-not", "-path", "/proc/*",
                "-not", "-path", "/sys/*",
                "-not", "-path", "/run/*",
                "-not", "-path", "/var/log/*",
                "-not", "-path", "/var/lib/dpkg/*",
                "-not", "-path", "/tmp/.*",
                "-not", "-path", "*/__pycache__/*",
                "-not", "-path", "*/.git/*",
            ],
            capture_output=True, text=True, timeout=8,
            check=False,
        )
        lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return f"(scan-error: {type(e).__name__})"

    if extensions_filter:
        ext_lower = {e.lower() for e in extensions_filter}
        lines = [l for l in lines if Path(l).suffix.lower() in ext_lower]
    else:
        # Khi không có filter, ưu tiên file có extension document/archive
        doc_exts = {".doc", ".docx", ".pdf", ".zip", ".xls", ".xlsx",
                    ".rar", ".7z", ".txt", ".csv"}
        prio = [l for l in lines if Path(l).suffix.lower() in doc_exts]
        if prio:
            lines = prio
        # Loại các file partial download
        lines = [l for l in lines if not any(
            l.lower().endswith(s) for s in _INCOMPLETE_SUFFIXES
        )]

    if not lines:
        return ""
    return str(lines[:10])


def _sanitize_download_name(path: Path) -> Path:
    """Đổi tên file trên đĩa để tên + CDN URL sạch.

    1. Cắt hậu tố trùng " (N)" Chrome tự thêm khi file trùng tên: "name (2).ext"
       → "name.ext".
    2. Đổi khoảng trắng còn lại → "-" (space thô làm CDN URL không truy cập được).
    Giữ nguyên ngoặc đơn hợp lệ khác.

    Returns:
        Path mới (đã rename) nếu cần đổi; path gốc nếu không đổi / rename fail.
    """
    name = path.name
    stem, dot, ext = name.rpartition(".")
    if not dot:  # không có extension
        stem = name
    # Cắt " (N)" ở cuối stem (Chrome duplicate suffix)
    stem = re.sub(r"\s*\(\d+\)\s*$", "", stem)
    safe = f"{stem}.{ext}" if dot else stem
    # Collapse khoảng trắng còn lại → "-"
    safe = re.sub(r"\s+", "-", safe)
    if safe == name or not safe:
        return path
    new_path = path.with_name(safe)
    try:
        path.replace(new_path)  # replace: ghi đè nếu tên đích đã tồn tại (orphan cũ)
        return new_path
    except OSError as e:
        _log.warning("Rename '%s' → '%s' fail: %s — dùng tên gốc", name, safe, e)
        return path


def _normalize_extensions(exts) -> set[str]:
    if not exts:
        return set()
    out = set()
    for e in exts:
        s = (e or "").strip().lower()
        if not s:
            continue
        if not s.startswith("."):
            s = "." + s
        out.add(s)
    return out


def _wait_for_new_file(
    download_dir: Path,
    extensions_filter: set[str],
    mtime_threshold: float,
    timeout_s: float,
):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        candidate = _pick_candidate(
            download_dir, extensions_filter, mtime_threshold,
        )
        if candidate is not None and _is_stable(candidate):
            return candidate
        time.sleep(0.5)
    return None


def _pick_candidate(
    download_dir: Path,
    extensions_filter: set[str],
    mtime_threshold: float,
):
    """Chọn file mới nhất trong dir thỏa mãn:
    - không phải file đang download (suffix _INCOMPLETE_SUFFIXES)
    - mtime > mtime_threshold (lookback window — file cũ tự động skip)
    - extension match filter (nếu có)
    """
    best = None
    best_mtime = 0.0
    for p in download_dir.iterdir():
        if not p.is_file():
            continue
        lower = p.name.lower()
        if any(lower.endswith(s) for s in _INCOMPLETE_SUFFIXES):
            continue
        if extensions_filter and p.suffix.lower() not in extensions_filter:
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < mtime_threshold:
            continue
        if mtime > best_mtime:
            best = p
            best_mtime = mtime
    return best


def _is_stable(path: Path) -> bool:
    """File coi là stable nếu size không đổi sau _STABILITY_WAIT_S và size > 0."""
    try:
        size1 = path.stat().st_size
        if size1 == 0:
            return False
        time.sleep(_STABILITY_WAIT_S)
        size2 = path.stat().st_size
        return size1 == size2
    except OSError:
        return False


def _upload_to_cdn(rt, local_path: Path, remote_dir_override) -> str | None:
    """Upload file lên CDN. Lazy import ArtifactUploader để action không
    phụ thuộc ArtifactUploader nếu test fakes (test runs có thể stub)."""
    try:
        from services.artifact_uploader import ArtifactUploader
    except ImportError as e:
        _log.error("ArtifactUploader import fail: %s", e)
        return None

    uploader = ArtifactUploader()
    session_id = getattr(rt, "session_id", "") or "unknown"
    date_str = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    default_remote_dir = (
        f"public/tool-web/prod/sessions/{date_str}/{session_id}/downloads"
    )
    remote_dir = remote_dir_override or default_remote_dir
    remote_path = f"{remote_dir.rstrip('/')}/{local_path.name}"
    return uploader.upload_artifact(str(local_path), remote_path)
