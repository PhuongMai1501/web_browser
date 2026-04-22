"""
browser_adapter.py - Wrapper gọi agent-browser CLI qua subprocess.
"""

import subprocess
import base64
import re
import os
import platform
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

_BROWSER_TIMEOUT_S = int(os.getenv("BROWSER_TIMEOUT", "30"))

# ── Trace buffer (bật/tắt từ runner để log agent-browser CLI theo session) ────
_TRACE_ENABLED = False
_TRACE_BUFFER: list[dict] = []
_TRACE_CURRENT_STEP = 0
_TRACE_STDOUT_MAX = 5000   # truncate stdout per-entry để file không phồng


def start_trace() -> None:
    """Bắt đầu ghi tất cả _run() calls vào buffer. Reset buffer."""
    global _TRACE_ENABLED
    _TRACE_BUFFER.clear()
    _TRACE_ENABLED = True


def stop_trace() -> list[dict]:
    """Tắt trace và trả về toàn bộ buffer."""
    global _TRACE_ENABLED
    _TRACE_ENABLED = False
    data = list(_TRACE_BUFFER)
    _TRACE_BUFFER.clear()
    return data


def set_trace_step(step: int) -> None:
    """Gắn step hiện tại để các _run() tiếp theo thuộc step này."""
    global _TRACE_CURRENT_STEP
    _TRACE_CURRENT_STEP = step


def _truncate(s: str, limit: int = _TRACE_STDOUT_MAX) -> str:
    if not s:
        return ""
    if len(s) <= limit:
        return s
    return s[:limit] + f"... [truncated {len(s) - limit} chars]"


ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
ARTIFACTS_DIR.mkdir(exist_ok=True)

# Chỉ cho phép ref dạng e<số> (e1, e2, e11...)
_REF_PATTERN = re.compile(r"^e\d+$")

# Domain allowlist cho open_url — chỉ điều hướng đến các domain tin cậy
_DEFAULT_ALLOWED_DOMAINS = frozenset({
    "fpt.net", "microsoftonline.com", "microsoft.com",
    "live.com", "office.com", "sharepoint.com",
})

# Mutable override — được generic_runner set khi scenario khai báo
# allowed_domains, reset sau khi job xong. Worker đơn luồng → không race.
_ALLOWED_DOMAINS: frozenset = _DEFAULT_ALLOWED_DOMAINS


def set_allowed_domains(domains) -> None:
    """Override allowlist cho scenario hiện tại. Nhớ reset khi xong."""
    global _ALLOWED_DOMAINS
    _ALLOWED_DOMAINS = frozenset(d.strip().lower() for d in domains if d)


def reset_allowed_domains() -> None:
    """Khôi phục allowlist về default."""
    global _ALLOWED_DOMAINS
    _ALLOWED_DOMAINS = _DEFAULT_ALLOWED_DOMAINS


def _validate_ref(ref: str) -> None:
    """Validate ref khớp pattern e<số>. Raise ValueError nếu không hợp lệ."""
    if not _REF_PATTERN.match(ref):
        raise ValueError(
            f"Ref không hợp lệ: '{ref}'. Chỉ chấp nhận e<số> (ví dụ: e1, e11)."
        )


def _validate_url_domain(url: str) -> None:
    """Validate URL thuộc domain trong allowlist."""
    try:
        hostname = urlparse(url).hostname or ""
        if hostname and not any(
            hostname == d or hostname.endswith("." + d) for d in _ALLOWED_DOMAINS
        ):
            raise ValueError(
                f"Domain '{hostname}' không nằm trong allowlist. "
                f"Cho phép: {sorted(_ALLOWED_DOMAINS)}"
            )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"URL không hợp lệ: '{url}'") from exc


def _run(args: list[str], timeout: int = _BROWSER_TIMEOUT_S) -> str:
    """Chạy lệnh agent-browser và trả về stdout.
    Dùng shell=False với list args để tránh shell injection.
    Trên Windows dùng cmd /c để tương thích với .cmd file.
    """
    if platform.system() == "Windows":
        cmd = ["cmd", "/c", "agent-browser"] + args
    else:
        cmd = ["agent-browser"] + args
    ts = datetime.now().isoformat()
    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            shell=False,
        )
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        if _TRACE_ENABLED:
            _TRACE_BUFFER.append({
                "step": _TRACE_CURRENT_STEP,
                "timestamp": ts,
                "command": args,
                "duration_ms": duration_ms,
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "exception": f"{type(exc).__name__}: {exc}",
            })
        raise
    duration_ms = int((time.monotonic() - started) * 1000)
    if _TRACE_ENABLED:
        _TRACE_BUFFER.append({
            "step": _TRACE_CURRENT_STEP,
            "timestamp": ts,
            "command": args,
            "duration_ms": duration_ms,
            "returncode": result.returncode,
            "stdout": _truncate(result.stdout or ""),
            "stderr": _truncate(result.stderr or "", 2000),
        })
    if result.returncode != 0:
        raise RuntimeError(
            f"agent-browser error (exit {result.returncode}):\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


def open_url(url: str) -> str:
    """Mở URL trong browser."""
    _validate_url_domain(url)
    return _run(["open", url], timeout=60)


def close_browser() -> str:
    """Đóng browser."""
    try:
        return _run(["close"])
    except Exception:
        return ""


def page_contains_any(texts: tuple[str, ...]) -> bool:
    """
    Kiểm tra trang có chứa bất kỳ chuỗi nào trong texts (case-insensitive) không.
    Dùng JS đọc document.body.innerText và normalize diacritics (NFD) trước khi so sánh
    để "chang biet tuot" khớp được "Chang Biết Tuốt", "hi anh" khớp "Hi! anh".
    """
    import json as _json
    import base64 as _b64
    import unicodedata
    try:
        # Normalize indicators phía Python: bỏ dấu + lowercase để so sánh chuẩn
        def _strip(s: str) -> str:
            nfd = unicodedata.normalize("NFD", s)
            return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()

        needle_plain = [_strip(t) for t in texts]   # bỏ dấu, lowercase
        needle_raw   = [t.lower() for t in texts]   # giữ nguyên dấu, lowercase
        all_needles  = list(dict.fromkeys(needle_plain + needle_raw))  # deduplicate

        escaped = _json.dumps(all_needles)

        # JS: tạo 2 version của DOM text — có dấu và không dấu — rồi check cả hai
        js = (
            "(function(){"
            "function norm(s){"
            "  return s.normalize('NFD')"
            "    .replace(/[\\u0300-\\u036f]/g,'')"
            "    .replace(/[\\u0111]/g,'d')"   # đ → d
            "    .replace(/[\\u0110]/g,'D')"   # Đ → D
            "    .toLowerCase();"
            "}"
            "var raw=(document.body&&document.body.innerText||'').toLowerCase();"
            "var stripped=norm(document.body&&document.body.innerText||'');"
            "var arr=" + escaped + ";"
            "return arr.some(function(s){return raw.indexOf(s)!==-1||stripped.indexOf(s)!==-1;});"
            "})()"
        )
        b64 = _b64.b64encode(js.encode()).decode()
        raw_out = _run(["eval", "-b", b64], timeout=10)
        result = _parse_json_output(raw_out)
        return result is True or str(result).lower() == "true"
    except Exception:
        return False


def take_snapshot() -> str:
    """Lấy accessibility tree snapshot, bổ sung metadata và inject error elements nếu có."""
    raw = _run(["snapshot", "-i"])
    enriched = _enrich_snapshot_with_dom_hints(raw)
    return _inject_page_errors(enriched)


def _parse_json_output(raw: str):
    """Parse JSON output từ agent-browser eval. Xử lý double-encoded string."""
    import json as _json
    try:
        result = _json.loads(raw)
        # agent-browser đôi khi trả JSON string bọc thêm 1 lớp quotes
        if isinstance(result, str):
            result = _json.loads(result)
        return result
    except Exception:
        return raw


def _inject_page_errors(snapshot: str) -> str:
    """
    Chạy JS để tìm các error/alert element trên trang.
    Nếu có, inject vào đầu snapshot dưới dạng cảnh báo để LLM nhận ra lỗi.
    """
    import base64 as _b64

    try:
        js = """
(function() {
  var selectors = [
    '[role="alert"]', '[aria-live="assertive"]',
    '[class*="error"]', '[class*="Error"]', '[class*="invalid"]', '[class*="Invalid"]',
    '[class*="danger"]', '[class*="Danger"]',
    '.field-error', '.form-error', '.input-error', '.validation-error'
  ];
  // Lọc chuỗi trông như số thống kê: "16K+", "427K+", "12,345", "99%"...
  var statsPattern = /^[\\d][\\d.,\\s]*[KkMmBb%+]*[+]?$/;
  var seen = new Set();
  var errors = [];
  selectors.forEach(function(sel) {
    try {
      document.querySelectorAll(sel).forEach(function(el) {
        var text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
        if (!text || text.length < 15) return;        // quá ngắn → bỏ qua
        if (statsPattern.test(text)) return;          // trông như số liệu → bỏ qua
        if (!seen.has(text)) { seen.add(text); errors.push(text); }
      });
    } catch(e) {}
  });
  return JSON.stringify(errors);
})()
"""
        b64 = _b64.b64encode(js.encode()).decode()
        raw_out = _run(["eval", "-b", b64], timeout=10)
        errors = _parse_json_output(raw_out)
        if errors:
            error_lines = "\n".join(f'  ⚠️ LỖI: "{e}"' for e in errors)
            return f"=== LỖI TRÊN TRANG ===\n{error_lines}\n=== KẾT THÚC LỖI ===\n\n" + snapshot
    except Exception:
        pass
    return snapshot


def _enrich_snapshot_with_dom_hints(snapshot: str) -> str:
    """
    Annotate trực tiếp vào snapshot cho các element tương tác không nhãn.
    Hiện tại tập trung vào link/button không có tên rõ ràng và bổ sung các hint:
    href, aria-label, title, text, icon, type.
    """
    import re as _re
    import base64 as _b64

    try:
        js = """
(function() {
  function cleanText(value) {
    return (value || '').replace(/\\s+/g, ' ').trim();
  }

  function iconHint(el) {
    if (!el || !el.querySelector) return '';
    var iconEl = el.querySelector('svg, i, [data-icon], [class*="icon"], [class*="lucide-"]');
    if (!iconEl) return '';
    var cls = cleanText(iconEl.getAttribute('class'));
    var dataIcon = cleanText(iconEl.getAttribute('data-icon'));
    var aria = cleanText(iconEl.getAttribute('aria-label'));
    if (dataIcon) return dataIcon;
    if (aria) return aria;
    var m = cls.match(/lucide-([\\w-]+)/);
    if (m) return m[1];
    if (cls) return cls.split(' ')[0];
    return cleanText(iconEl.tagName).toLowerCase();
  }

  function collect(selector, tagHint) {
    return Array.from(document.querySelectorAll(selector)).filter(function(e) {
      var text = cleanText(e.innerText || e.textContent || e.value);
      var aria = cleanText(e.getAttribute('aria-label'));
      var title = cleanText(e.getAttribute('title'));
      return !(text || aria || title);
    }).map(function(e) {
      var href = cleanText(e.getAttribute('href'));
      var type = cleanText(e.getAttribute('type'));
      var icon = iconHint(e);
      return {
        href: href,
        ariaLabel: cleanText(e.getAttribute('aria-label')),
        title: cleanText(e.getAttribute('title')),
        text: cleanText(e.innerText || e.textContent || e.value),
        type: type,
        icon: icon,
        tag: tagHint
      };
    });
  }

  return JSON.stringify({
    links: collect('a[href]', 'link'),
    buttons: collect('button, input[type="button"], input[type="submit"], input[type="reset"], [role="button"]', 'button')
  });
})()
"""
        b64 = _b64.b64encode(js.encode()).decode()
        raw = _run(["eval", "-b", b64], timeout=10)
        dom_data = _parse_json_output(raw)
    except Exception:
        return snapshot

    if not dom_data:
        return snapshot

    def _format_hint(item: dict) -> str:
        parts = []
        if item.get("href"):
            parts.append(f'href={item["href"]}')
        if item.get("ariaLabel"):
            parts.append(f'aria-label={item["ariaLabel"]}')
        if item.get("title"):
            parts.append(f'title={item["title"]}')
        if item.get("text"):
            parts.append(f'text={item["text"]}')
        if item.get("type"):
            parts.append(f'type={item["type"]}')
        if item.get("icon"):
            icon = item["icon"]
            login_tag = ""
            if icon.lower() in ("key", "lock", "log-in", "log-in-icon", "user", "login"):
                login_tag = " ← CÓ THỂ LÀ NÚT ĐĂNG NHẬP"
            parts.append(f"icon={icon}{login_tag}")
        return ", ".join(parts)

    def _annotate_role(text: str, role: str, items: list[dict]) -> str:
        unnamed_refs = _re.findall(rf"{role} \[ref=(e\d+)\](?!\s*\")", text)
        if not unnamed_refs or not items:
            return text

        ref_map = {}
        for i, ref in enumerate(unnamed_refs):
            if i >= len(items):
                break
            hint = _format_hint(items[i])
            if hint:
                ref_map[ref] = hint

        if not ref_map:
            return text

        def replace_ref(m):
            ref = m.group(1)
            hint = ref_map.get(ref)
            if hint:
                return f"{role} [ref={ref}] ({hint})"
            return m.group(0)

        return _re.sub(rf"{role} \[ref=(e\d+)\](?!\s*\")", replace_ref, text)

    snapshot = _annotate_role(snapshot, "link", dom_data.get("links", []))
    snapshot = _annotate_role(snapshot, "button", dom_data.get("buttons", []))
    return snapshot


def take_screenshot(save_path: str | None = None, full_page: bool = False) -> tuple[str, str]:
    """
    Chụp screenshot và trả về (base64_string, file_path).
    Nếu save_path không cung cấp, lưu vào artifacts/.
    full_page=True dùng `screenshot --full` để chụp toàn trang (không chỉ viewport).
    """
    if save_path is None:
        save_path = str(ARTIFACTS_DIR / "screenshot.png")

    args = ["screenshot"]
    if full_page:
        args.append("--full")
    args.append(save_path)
    _run(args)

    with open(save_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")

    return b64, save_path


def take_annotated_screenshot(save_path: str | None = None, full_page: bool = False) -> tuple[str, str]:
    """
    Chụp screenshot có đánh số label lên từng interactive element.
    Dùng để hiển thị cho user thấy GPT "nhìn" vào element nào.
    Trả về (base64_string, file_path).
    """
    if save_path is None:
        save_path = str(ARTIFACTS_DIR / "screenshot_annotated.png")

    args = ["screenshot", "--annotate"]
    if full_page:
        args.append("--full")
    args.append(save_path)
    try:
        _run(args)
    except Exception:
        # Fallback: chụp screenshot thường nếu --annotate không hỗ trợ
        fb_args = ["screenshot"]
        if full_page:
            fb_args.append("--full")
        fb_args.append(save_path)
        _run(fb_args)

    with open(save_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")

    return b64, save_path


def scroll_page(direction: str = "down", amount: int | None = None) -> str:
    """Scroll trang theo direction. direction: up|down|left|right|top|bottom.
    amount: pixels (None → default 300 của agent-browser; 'top'/'bottom' bỏ qua)."""
    d = (direction or "down").lower()
    if d in ("top", "bottom"):
        # Dùng JS eval để về đúng đầu/cuối document
        import base64 as _b64
        if d == "top":
            js = "window.scrollTo(0, 0); 'top'"
        else:
            js = "window.scrollTo(0, document.body.scrollHeight); 'bottom'"
        b64 = _b64.b64encode(js.encode()).decode()
        return _run(["eval", "-b", b64], timeout=10)
    args = ["scroll", d]
    if amount is not None:
        args.append(str(int(amount)))
    return _run(args, timeout=10)


def click_element(ref: str) -> str:
    """Click element theo ref (e.g. 'e11')."""
    ref = ref.lstrip("@")
    _validate_ref(ref)
    return _run(["click", f"@{ref}"], timeout=15)


def type_text(ref: str, text: str) -> str:
    """Nhập text vào element."""
    ref = ref.lstrip("@")
    _validate_ref(ref)
    return _run(["fill", f"@{ref}", text], timeout=15)


def press_key(key: str) -> str:
    """Nhấn phím (e.g. 'Enter', 'Tab')."""
    return _run(["press", key], timeout=10)


def wait_ms(ms: int) -> str:
    """Đợi số milliseconds."""
    return _run(["wait", str(ms)], timeout=max(ms // 1000 + 10, 15))


def get_current_url() -> str:
    """Lấy URL hiện tại."""
    return _run(["get", "url"], timeout=10)


def get_page_title() -> str:
    """Lấy title trang hiện tại."""
    return _run(["get", "title"], timeout=10)


def get_attr(ref: str, attr_name: str) -> str:
    """Đọc attribute của element theo ref (vd 'href', 'src').

    LƯU Ý: một số version agent-browser không hỗ trợ `@ref` cho command
    `get attr` → caller nên fallback sang JS eval hoặc click + target override.
    """
    ref = ref.lstrip("@")
    _validate_ref(ref)
    return _run(["get", "attr", attr_name, f"@{ref}"], timeout=10)


def force_same_tab_links() -> str:
    """Override tất cả <a target='_blank'> thành target='_self' + unset onclick
    window.open, để click link navigate same-tab (agent-browser session không
    follow tab mới)."""
    import base64 as _b64
    js = (
        "(function(){"
        "var n=0;"
        "document.querySelectorAll('a[target=\"_blank\"]').forEach(function(a){"
        "  a.target='_self'; n++;"
        "});"
        "return 'overrode ' + n + ' links';"
        "})()"
    )
    b64 = _b64.b64encode(js.encode()).decode()
    return _run(["eval", "-b", b64], timeout=10)


def eval_js(js_code: str, timeout: int = 10) -> str:
    """Chạy JS tuỳ ý trên trang hiện tại, trả về stdout thô của agent-browser.
    Caller tự parse JSON nếu cần. Dùng base64 để tránh shell escape issue."""
    import base64 as _b64
    b64 = _b64.b64encode(js_code.encode("utf-8")).decode()
    return _run(["eval", "-b", b64], timeout=timeout)


def extract_refs(snapshot: str) -> set[str]:
    """Parse snapshot text và trả về set các ref hợp lệ (e.g. {'e1', 'e2', ...})."""
    return {f"e{n}" for n in re.findall(r"\be(\d+)\b", snapshot)}


def ref_exists(ref: str, snapshot: str) -> bool:
    """Kiểm tra ref có tồn tại trong snapshot không."""
    clean_ref = ref.lstrip("@")
    refs = extract_refs(snapshot)
    return clean_ref in refs


def element_has_description(ref: str, snapshot: str) -> bool:
    """
    Returns False nếu element không có text, hint, icon, href hay bất kỳ mô tả nào.
    Kiểm tra cả text trước ref (label trong "") lẫn metadata sau ref (icon, href...).
    """
    clean_ref = ref.lstrip("@")
    for line in snapshot.splitlines():
        if f"[ref={clean_ref}]" in line:
            # Kiểm tra sau ref: enriched hints như (icon=key, href=...)
            after_ref = line.split(f"[ref={clean_ref}]", 1)[-1].strip()
            if after_ref:
                return True
            # Kiểm tra trước ref: label/text nằm trong dấu ngoặc kép
            before_ref = line.split(f"[ref={clean_ref}]", 1)[0]
            if re.search(r'"[^"]+"', before_ref):
                return True
            return False
    return True  # Ref không tìm thấy → không trigger fallback
