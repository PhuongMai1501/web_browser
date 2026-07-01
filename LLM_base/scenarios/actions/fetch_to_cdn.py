"""Action: fetch_to_cdn — tải file qua fetch IN-PAGE (XHR đồng bộ) rồi upload CDN.

Vì sao cần action này (thay cho upload_download):
  Một số site phục vụ file qua origin HTTP (vd http://mvc.inside.fpt.net). Chrome
  Safe Browsing / insecure-download chặn download → file kẹt `.crdownload` và hiện
  nút "Giữ lại" trên thanh download của TRÌNH DUYỆT (không nằm trong DOM → không
  click được bằng eval_js/click). Hệ quả: upload_download luôn timeout.

  Cách lách: KHÔNG dùng cơ chế download của Chrome. Chạy XMLHttpRequest đồng bộ
  NGAY TRONG trang (cùng origin → tự kèm cookie session), đọc nội dung file dạng
  binary, encode base64, trả về worker. Worker giải mã + upload CDN/MinIO. fetch
  trong trang KHÔNG bị Safe Browsing chặn vì không phải "download" của trình duyệt.
  Đồng thời tránh luôn lỗi DNS egress trực tiếp từ pod (mvc.inside.fpt.net NXDOMAIN)
  vì request đi qua đúng network path của browser (đã kết nối được).

YAML usage:

    - action: fetch_to_cdn
      url: "http://mvc.inside.fpt.net/IBB/MBSv4Report/Download?vUrl=&Token={Token}"
      extensions: [".xls", ".xlsx", ".zip"]   # optional — validate đuôi file
      filename: "MBS.xlsx"                      # optional — override tên file
      remote_dir: "public/.../downloads"        # optional — override subdir CDN
      timeout_ms: 120000                         # optional — timeout XHR + upload
      note: "Tải MBS qua XHR in-page (bypass Safe Browsing) rồi upload MinIO"

  Placeholder `{Token}` trong url sẽ được JS thay bằng query param `Token` của
  trang hiện tại (URL-encoded). Có thể dùng url tương đối; JS resolve theo
  document.baseURI. Nếu không truyền extensions → nhận file bất kỳ.

Hạn chế: file được trả qua stdout của agent-browser dưới dạng base64 → phù hợp
file vài MB. File rất lớn (vài chục MB) có thể chậm/tràn buffer — khi đó cân nhắc
hướng tắt Safe Browsing ở tầng Chrome (AGENT_BROWSER_ARGS).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..action_registry import ActionResult, action

_log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_MS = 120000
_EVAL_OVERHEAD_S = 15  # buffer cho upload + parse sau khi XHR xong


@action("fetch_to_cdn")
def run_fetch_to_cdn(rt, step) -> ActionResult:
    url = (step.url or "").strip()
    if not url:
        return ActionResult(
            ok=False, action_type="fetch_to_cdn",
            error="step fetch_to_cdn thiếu field 'url'",
        )

    timeout_s = (step.timeout_ms or _DEFAULT_TIMEOUT_MS) / 1000.0
    eval_timeout = int(timeout_s + _EVAL_OVERHEAD_S)

    js = _build_fetch_js(url)
    try:
        raw = rt.browser.eval_js(js, timeout=eval_timeout)
    except Exception as e:
        return ActionResult(
            ok=False, action_type="fetch_to_cdn",
            error=f"eval_js XHR fail: {e}",
        )

    payload = _parse_json(raw)
    if not isinstance(payload, dict):
        return ActionResult(
            ok=False, action_type="fetch_to_cdn",
            error=f"XHR trả output không parse được JSON: {str(raw)[:200]}",
        )
    if not payload.get("ok"):
        return ActionResult(
            ok=False, action_type="fetch_to_cdn",
            error=f"XHR fail: {payload.get('error') or 'unknown'} (url={url})",
        )

    b64 = payload.get("b64") or ""
    if not b64:
        return ActionResult(
            ok=False, action_type="fetch_to_cdn",
            error="XHR ok nhưng nội dung file rỗng",
        )

    try:
        data = base64.b64decode(b64)
    except Exception as e:
        return ActionResult(
            ok=False, action_type="fetch_to_cdn",
            error=f"Giải mã base64 fail: {e}",
        )

    filename = _resolve_filename(step.filename, payload.get("cd", ""), url)

    ext_ok, ext_err = _validate_extension(filename, step.extensions)
    if not ext_ok:
        return ActionResult(
            ok=False, action_type="fetch_to_cdn", error=ext_err,
        )

    tmp_path = _write_temp(data, filename)
    try:
        cdn_url = _upload_to_cdn(rt, tmp_path, filename, step.remote_dir)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    if not cdn_url:
        return ActionResult(
            ok=False, action_type="fetch_to_cdn",
            error=f"Upload {filename} → CDN thất bại (xem worker log)",
        )

    # Đưa link vào output chính (result.data) để Sup Agent đọc như "đầu ra".
    _write_output_holder(rt, filename, cdn_url)

    return ActionResult(
        ok=True, action_type="fetch_to_cdn",
        downloaded_filename=filename,
        downloaded_cdn_url=cdn_url,
        reason=step.note or f"Fetch {filename} ({len(data)} bytes) → CDN",
    )


def _write_output_holder(rt, filename: str, cdn_url: str) -> None:
    """Ghi link tải vào rt.output_holder["data"] → surface vào result.data.
    Merge vào dict data sẵn có (không đè extract_data); gom thêm vào downloads[]."""
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


def _build_fetch_js(url: str) -> str:
    """JS chạy XHR đồng bộ, đọc binary qua charset x-user-defined, encode base64.

    XHR đồng bộ (async=false) để eval_js trả kết quả ngay, không cần await Promise
    (agent-browser eval không đảm bảo await được). overrideMimeType giữ nguyên byte
    thô; charCodeAt(i)&0xff lấy lại byte gốc trước khi btoa.
    """
    url_json = json.dumps(url)  # an toàn cho mọi ký tự trong url
    return (
        "(function(){"
        "try{"
        f"var u={url_json};"
        "var tok=new URLSearchParams(window.location.search).get('Token')||'';"
        "u=u.replace('{Token}',encodeURIComponent(tok));"
        "var x=new XMLHttpRequest();"
        "x.open('GET',u,false);"
        "x.overrideMimeType('text/plain; charset=x-user-defined');"
        "x.send();"
        "if(x.status!==200){return JSON.stringify({ok:false,error:'HTTP '+x.status});}"
        "var bin=x.responseText,len=bin.length;"
        "var bytes=new Uint8Array(len);"
        "for(var i=0;i<len;i++){bytes[i]=bin.charCodeAt(i)&0xff;}"
        "var CH=0x8000,s='';"
        "for(var j=0;j<len;j+=CH){s+=String.fromCharCode.apply(null,bytes.subarray(j,j+CH));}"
        "var b64=btoa(s);"
        "var cd=x.getResponseHeader('Content-Disposition')||'';"
        "return JSON.stringify({ok:true,b64:b64,len:len,cd:cd});"
        "}catch(e){return JSON.stringify({ok:false,error:String(e)});}"
        "})()"
    )


def _parse_json(raw):
    """Parse output eval, xử lý double-encoded string (giống browser_adapter)."""
    if isinstance(raw, (dict, list)):
        return raw
    try:
        result = json.loads(raw)
        if isinstance(result, str):
            result = json.loads(result)
        return result
    except Exception:
        return None


def _resolve_filename(override: str | None, content_disposition: str, url: str) -> str:
    """Ưu tiên: filename override → Content-Disposition → tên từ URL → fallback."""
    if override:
        return _sanitize(override)

    if content_disposition:
        # filename*=UTF-8''... (RFC 5987) ưu tiên, rồi filename="..."
        m = re.search(r"filename\*\=(?:UTF-8'')?\"?([^\";]+)", content_disposition, re.I)
        if not m:
            m = re.search(r'filename\=\"?([^\";]+)', content_disposition, re.I)
        if m:
            from urllib.parse import unquote
            return _sanitize(unquote(m.group(1).strip()))

    # Tên từ path của URL (bỏ query)
    path_part = url.split("?", 1)[0].rstrip("/")
    tail = path_part.rsplit("/", 1)[-1] if "/" in path_part else ""
    if tail and "." in tail:
        return _sanitize(tail)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"download_{ts}.bin"


def _sanitize(name: str) -> str:
    name = name.strip().replace("\\", "_").replace("/", "_")
    # Collapse khoảng trắng → "-" (space thô trong CDN URL làm link không truy cập được)
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[^\w.\-()]", "_", name)
    return name or "download.bin"


def _validate_extension(filename: str, extensions) -> tuple[bool, str]:
    if not extensions:
        return True, ""
    allowed = set()
    for e in extensions:
        s = (e or "").strip().lower()
        if not s:
            continue
        if not s.startswith("."):
            s = "." + s
        allowed.add(s)
    if not allowed:
        return True, ""
    suffix = Path(filename).suffix.lower()
    if suffix in allowed:
        return True, ""
    return False, (
        f"File '{filename}' (đuôi '{suffix or 'none'}') không khớp filter "
        f"{sorted(allowed)}. Có thể URL trả về trang HTML/lỗi thay vì file."
    )


def _write_temp(data: bytes, filename: str) -> Path:
    suffix = Path(filename).suffix or ".bin"
    fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="fetch_cdn_")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return Path(tmp)


def _upload_to_cdn(rt, local_path: Path, filename: str, remote_dir_override) -> str | None:
    """Upload lên CDN qua ArtifactUploader, giữ tên file gốc."""
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
    remote_path = f"{remote_dir.rstrip('/')}/{filename}"
    return uploader.upload_artifact(str(local_path), remote_path)
