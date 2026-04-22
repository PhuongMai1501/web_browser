"""Action: open_link — mở link same-tab qua JS navigation (bypass click handlers).

Vì nhiều site (vd thuvienphapluat.vn qua Cloudflare) có `onclick`
`preventDefault()` chặn programmatic click → link không navigate. Cách
chắc ăn: tìm link theo innerText trực tiếp trên DOM (JS eval) rồi gán
`window.location.href = link.href`. Cách này:
  - Bypass mọi click event handler (analytics, tracking, CF proxy)
  - Trigger navigation thật same-tab
  - Nếu href là CF proxy URL, Cloudflare server tự 302 về URL gốc

YAML ví dụ:
    - action: open_link
      target:
        role: link              # optional — chỉ dùng text_any là đủ
        text_any: ["{title_hint}"]
      nth: 0                    # pick link thứ mấy nếu nhiều match
"""

from __future__ import annotations

import json

from ..action_registry import ActionResult, action


@action("open_link")
def run_open_link(rt, step) -> ActionResult:
    if step.target is None:
        return ActionResult(ok=False, action_type="open_link",
                            error="step open_link thiếu 'target'")

    # Chỉ dùng text_any (hoặc text_all) làm criteria — role/placeholder không
    # map xuống DOM selector được tiện.
    needles = list(step.target.text_any or []) + list(step.target.text_all or [])
    needles = [n for n in needles if n]
    if not needles:
        return ActionResult(
            ok=False, action_type="open_link",
            error="open_link hiện chỉ support target.text_any / text_all",
        )

    # Tất cả needles phải match (AND) nếu text_all, OR nếu text_any
    mode = "all" if step.target.text_all else "any"
    nth = step.target.nth or 0

    url_before = _safe_url(rt.browser)

    # JS: tìm <a href> có innerText khớp needles (diacritic-insensitive),
    # pick phần tử thứ nth, navigate tới href của nó.
    js = _build_nav_js(needles, mode, nth)
    try:
        raw = rt.browser.eval_js(js, timeout=10)
    except Exception as e:
        return ActionResult(ok=False, action_type="open_link",
                            error=f"eval_js fail: {e}")

    # raw dạng JSON string; agent-browser đôi khi bọc thêm 1 lớp quotes.
    try:
        result = json.loads(raw)
        if isinstance(result, str):
            result = json.loads(result)
    except Exception:
        result = {"status": "parse_error", "raw": raw[:200]}

    status = result.get("status")
    if status != "navigating":
        msg = {
            "not_found": "Không tìm thấy <a> có innerText khớp needles trên DOM",
            "no_href":   "Tìm thấy link nhưng không có href (JS-only)",
            "parse_error": "Parse JSON eval result fail",
        }.get(status, f"status unknown: {status}")
        return ActionResult(ok=False, action_type="open_link",
                            error=f"{msg} (needles={needles}, mode={mode}, nth={nth}); "
                                  f"detail={result}")

    href = result.get("href", "")
    matched_text = result.get("text", "")

    # Chờ navigation
    try:
        rt.browser.wait_ms(3000)
    except Exception:
        pass

    url_after = _safe_url(rt.browser)
    rt.last_snapshot = ""

    if url_after == url_before:
        return ActionResult(
            ok=False, action_type="open_link",
            url_before=url_before, url_after=url_after,
            error=(
                f"JS set window.location.href='{href}' nhưng URL không đổi "
                f"sau 3s. Có thể CF proxy URL không serve redirect, hoặc "
                f"Cross-origin block. Link matched='{matched_text[:80]}'"
            ),
        )

    return ActionResult(
        ok=True, action_type="open_link",
        url_before=url_before, url_after=url_after,
        reason=step.note or f"Nav tới link '{matched_text[:60]}' (same-tab)",
    )


def _build_nav_js(needles: list[str], mode: str, nth: int) -> str:
    """Sinh JS tìm <a> theo innerText + navigate via window.location."""
    needles_json = json.dumps(needles, ensure_ascii=False)
    check_expr = (
        "ns.every(function(n){return txt.indexOf(n)!==-1;})"
        if mode == "all" else
        "ns.some(function(n){return txt.indexOf(n)!==-1;})"
    )
    return (
        "(function(){\n"
        "function strip(s){return s.normalize('NFD').replace(/[\\u0300-\\u036f]/g,'')"
        ".replace(/[\\u0111]/g,'d').replace(/[\\u0110]/g,'D').toLowerCase();}\n"
        f"var ns={needles_json}.map(strip);\n"
        "var links=Array.from(document.querySelectorAll('a[href]'));\n"
        "var matches=links.filter(function(a){\n"
        "  var txt=strip(a.innerText||a.textContent||'');\n"
        f"  return {check_expr};\n"
        "});\n"
        f"if(matches.length===0) return JSON.stringify({{status:'not_found',count:0}});\n"
        f"var idx={nth}; if(idx<0||idx>=matches.length) idx=0;\n"
        "var target=matches[idx];\n"
        "var href=target.getAttribute('href');\n"
        "if(!href) return JSON.stringify({status:'no_href',"
        "count:matches.length,text:(target.innerText||'').slice(0,100)});\n"
        "var absHref=new URL(href, window.location.href).href;\n"
        "window.location.href=absHref;\n"
        "return JSON.stringify({status:'navigating',href:absHref,"
        "text:(target.innerText||'').slice(0,100),count:matches.length,idx:idx});\n"
        "})()\n"
    )


def _safe_url(browser) -> str:
    try:
        return browser.get_current_url()
    except Exception:
        return ""
