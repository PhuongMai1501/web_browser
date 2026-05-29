"""Action: http_login — HTTP-based SSO login chain, bypass Chrome JS limitation.

Use case (Plan B 2026-05-29): SSO flow phức tạp (vd FPT IAM `login.fpt.net`)
trả response HTML chỉ có `<script>location.href='X'</script>` mà agent-browser
daemon mode KHÔNG follow JS redirect → Chrome stuck.

Flow:
  1. `requests.Session()` với proxy từ env `HTTP_PROXY`
  2. GET login URL (form) → parse form action + hidden fields (CSRF, ViewState)
  3. POST credentials → server set cookies + 302
  4. Loop: nếu response 200 + `<script>location.href='X'</script>` → extract X → GET tiếp
  5. Tới final URL (hoặc max_redirects) → lấy cookies từ session
  6. Set từng cookie vào Chrome qua `agent-browser cookies set` CLI
  7. `browser.open_url(return_url)` → Chrome có cookies → render dashboard

Fields YAML:
  url:             login URL (form GET đầu) — reuse FlowStep.url
  username_from:   context key cho username (fallback step.value_from hoặc "username")
  password_from:   context key cho password (fallback "password"). Type=secret thì log mask.
  return_url:      URL final Chrome navigate sau cookies set
  max_redirects:   số JS chain max follow (default 5)

Ví dụ:
  - action: http_login
    url: "http://login.fpt.net/?urlreturn=inside.fpt.net"
    username_from: username
    password_from: password
    return_url: "http://inside.fpt.net/default.asp"
    max_redirects: 5
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urljoin

import requests

from ..action_registry import ActionResult, action

_log = logging.getLogger(__name__)

# JS redirect: location.href='X' | location.href="X" | window.location.href=...
_JS_REDIRECT_RE = re.compile(
    r"""(?:window\.)?location(?:\.href)?\s*=\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)

# Form parsing regex (avoid BeautifulSoup dependency)
_FORM_RE = re.compile(r"<form[^>]*>.*?</form>", re.IGNORECASE | re.DOTALL)
_FORM_ACTION_RE = re.compile(r'action\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)
_INPUT_RE = re.compile(r"<input[^>]*>", re.IGNORECASE)
_INPUT_NAME_RE = re.compile(r'name\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_INPUT_VALUE_RE = re.compile(r'value\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)


@action("http_login")
def run_http_login(rt, step) -> ActionResult:
    login_url = (step.url or "").strip()
    if not login_url:
        return ActionResult(
            ok=False, action_type="http_login",
            error="http_login thiếu field `url` (login form URL)",
        )

    username_from = (getattr(step, "username_from", None) or step.value_from or "username").strip()
    password_from = (getattr(step, "password_from", None) or "password").strip()
    return_url = (getattr(step, "return_url", None) or "").strip()
    max_redirects = int(getattr(step, "max_redirects", None) or 5)

    ctx = rt.context or {}
    username = (ctx.get(username_from) or "").strip()
    password = ctx.get(password_from) or ""
    if not username or not password:
        return ActionResult(
            ok=False, action_type="http_login",
            error=(
                f"http_login thiếu credential — "
                f"context['{username_from}']={'<set>' if username else '<empty>'}, "
                f"context['{password_from}']={'<set>' if password else '<empty>'}"
            ),
        )

    # Session với proxy + UA giả Chrome desktop
    session = requests.Session()
    http_p = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
    https_p = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if http_p:
        session.proxies["http"] = http_p
    if https_p:
        session.proxies["https"] = https_p
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi,en;q=0.9",
    })

    try:
        # 1. GET login form
        r = session.get(login_url, timeout=15, allow_redirects=True)
        r.raise_for_status()
        _log.info("http_login: GET %s → %d (%d bytes)", login_url, r.status_code, len(r.text))

        # 2. Parse form (action URL + input fields)
        form_action, form_data = _parse_form(r.text, r.url)

        # 3. Inject credentials — match heuristic field name
        injected = False
        for k in list(form_data.keys()):
            kl = k.lower()
            if any(t in kl for t in ("user", "name", "email", "account", "login")):
                form_data[k] = username
                injected = True
            elif any(t in kl for t in ("pass", "pwd")):
                form_data[k] = password
                injected = True
        # Fallback nếu form không có field detect được
        if not injected:
            form_data.setdefault("UserName", username)
            form_data.setdefault("username", username)
            form_data.setdefault("Password", password)
            form_data.setdefault("password", password)

        # 4. POST form
        post_url = form_action or r.url or login_url
        r = session.post(post_url, data=form_data, timeout=15, allow_redirects=True)
        _log.info("http_login: POST %s → %d (final URL %s)", post_url, r.status_code, r.url)

        # 5. Follow JS redirect chain (location.href trong response HTML)
        for i in range(max_redirects):
            m = _JS_REDIRECT_RE.search(r.text)
            if not m:
                break
            next_url = m.group(1)
            # Resolve relative URL
            if not next_url.lower().startswith(("http://", "https://")):
                next_url = urljoin(r.url, next_url)
            _log.info("http_login: JS chain #%d → %s", i + 1, next_url)
            r = session.get(next_url, timeout=15, allow_redirects=True)

        final_url = r.url
        cookies_total = len(session.cookies)
        _log.info("http_login: final URL=%s | cookies=%d", final_url, cookies_total)

        # 6. Set cookies vào Chrome qua agent-browser cookies set CLI
        # Lazy import _run để tránh circular nếu test isolated.
        from browser_adapter import _run as _ab_run

        cookies_set = 0
        for cookie in session.cookies:
            try:
                args = ["cookies", "set", cookie.name, cookie.value or ""]
                if cookie.domain:
                    args += ["--domain", cookie.domain]
                if cookie.path:
                    args += ["--path", cookie.path]
                _ab_run(args, timeout=10)
                cookies_set += 1
            except Exception as e:
                _log.warning("http_login: set_cookie %s fail: %s", cookie.name, e)

        # 7. Chrome navigate return_url với cookies session
        chrome_url_after = ""
        if return_url:
            try:
                rt.browser.open_url(return_url)
            except Exception as e:
                _log.warning("http_login: open_url(%s) fail: %s", return_url, e)
            try:
                rt.browser.wait_ms(2000)
            except Exception:
                pass
            try:
                chrome_url_after = rt.browser.get_current_url() or ""
            except Exception:
                chrome_url_after = return_url

        return ActionResult(
            ok=True,
            action_type="http_login",
            url_after=chrome_url_after or final_url,
            reason=(
                f"HTTP login OK | {cookies_total} cookies từ session "
                f"({cookies_set} set vào Chrome) | final HTTP URL: {final_url}"
            ),
            text_typed="***",  # mask password log
        )

    except requests.exceptions.RequestException as e:
        _log.error("http_login network error: %s", e, exc_info=True)
        return ActionResult(
            ok=False, action_type="http_login",
            error=f"http_login network: {type(e).__name__}: {str(e)[:200]}",
        )
    except Exception as e:
        _log.error("http_login unexpected: %s", e, exc_info=True)
        return ActionResult(
            ok=False, action_type="http_login",
            error=f"http_login: {type(e).__name__}: {str(e)[:200]}",
        )


def _parse_form(html: str, base_url: str) -> tuple[str, dict]:
    """Extract form action + input fields. Pick form chứa input[type=password]
    (form login chắc chắn) trước, fallback form đầu tiên."""
    forms = _FORM_RE.findall(html)
    if not forms:
        return "", {}

    target_form = forms[0]
    for f in forms:
        if 'type="password"' in f.lower() or "type='password'" in f.lower():
            target_form = f
            break

    am = _FORM_ACTION_RE.search(target_form)
    action_url = urljoin(base_url, am.group(1)) if am and am.group(1) else base_url

    fields: dict = {}
    for input_match in _INPUT_RE.finditer(target_form):
        input_tag = input_match.group(0)
        nm = _INPUT_NAME_RE.search(input_tag)
        if not nm:
            continue
        name = nm.group(1)
        vm = _INPUT_VALUE_RE.search(input_tag)
        fields[name] = vm.group(1) if vm else ""

    return action_url, fields
