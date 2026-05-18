"""
scripts/test_super_agent_simulation.py — E2E test mô phỏng Super Agent
gọi API tool-web để chạy scenario.

Flow:
  1. Health check API
  2. GET /v1/scenarios/{code}/revisions/{ver}/input-fields → lấy inputs schema
  3. Build context dict từ schema (auto-fill placeholders, secret từ env)
  4. POST /v1/sessions với context → enqueue job
  5. Poll GET /v1/sessions/{id} mỗi 5s đến terminal status
  6. GET /v1/sessions/{id}/result → verify cdn_url
  7. (Optional) Download cdn_url để verify file accessible

Đây mô phỏng đúng cách Super Agent integrate:
  - Đọc inputs schema từ DB (Phase 1)
  - Build context dynamic theo schema
  - POST session + track status
  - Receive result via polling (callback_url cũng support nhưng cần endpoint webhook)

Usage:
    # Set credentials cho scenario QCVN (KHÔNG hardcode trong file)
    $env:SCENARIO_USERNAME = "fpttelecom"
    $env:SCENARIO_PASSWORD = "<password>"

    # Set API base — prod hoặc local
    $env:API_BASE = "http://chang-browser-api.dscapp.com"     # prod
    # hoặc:
    $env:API_BASE = "http://127.0.0.1:9000"                   # local

    # Run với scenario QCVN default
    python ai_tool_web/scripts/test_super_agent_simulation.py

    # Run với scenario khác
    python ai_tool_web/scripts/test_super_agent_simulation.py \
        --scenario user_hiepqn_check_law_version_qcvn \
        --version 17

    # Custom context values (JSON inline)
    python ai_tool_web/scripts/test_super_agent_simulation.py \
        --context '{"so_hieu":"QCVN 86","title_hint":"Bản đồ địa hình"}'

    # No-wait — chỉ POST rồi return session_id (cho async test)
    python ai_tool_web/scripts/test_super_agent_simulation.py --no-wait

Exit code: 0 nếu session done + cdn_url accessible, 1 nếu fail.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Optional

import requests


_log = logging.getLogger("test_super_agent")


# ── Defaults ─────────────────────────────────────────────────────────────────

DEFAULT_API_BASE = "http://chang-browser-api.dscapp.com"
DEFAULT_USER = "hiepqn"
DEFAULT_SCENARIO = "user_hiepqn_check_law_version_qcvn"

# Default context overrides cho scenario QCVN (chỉ user_input fields,
# credentials lấy từ env SCENARIO_USERNAME/PASSWORD)
DEFAULT_CONTEXT_QCVN = {
    "so_hieu": "QCVN 86",
    "title_hint": "QCVN 86",
}

# Mock values cho field type khi scenario khác (auto-fill nếu user không override)
TYPE_DEFAULTS = {
    "string": "test-value",
    "number": 0,
    "bool": False,
}


# ── Helpers ──────────────────────────────────────────────────────────────────

class ApiClient:
    def __init__(self, base: str, user: str):
        self.base = base.rstrip("/")
        self.user = user
        self.headers = {"X-User-Id": user}

    def get(self, path: str) -> Any:
        r = requests.get(f"{self.base}{path}", headers=self.headers, timeout=15)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict) -> Any:
        r = requests.post(
            f"{self.base}{path}",
            headers={**self.headers, "Content-Type": "application/json"},
            json=body, timeout=30,
        )
        if not r.ok:
            print(f"[ERROR] POST {path} → HTTP {r.status_code}: {r.text[:400]}")
            r.raise_for_status()
        return r.json()


def _build_context(
    fields_schema: list[dict],
    user_overrides: dict,
    credentials: dict,
) -> dict:
    """Build context dict từ inputs schema.

    Priority:
      1. user_overrides (--context CLI arg)
      2. credentials (env SCENARIO_USERNAME/PASSWORD cho secret fields)
      3. DEFAULT_CONTEXT_QCVN cho scenario QCVN known fields
      4. TYPE_DEFAULTS theo field_type

    Skip fields có source='ask_user' (runtime hỏi, không cần POST body).
    """
    context = {}
    missing_required = []

    for field in fields_schema:
        name = field["name"]
        ftype = field["field_type"]
        source = field.get("source", "context")
        required = field.get("is_required", False)

        if source == "ask_user":
            continue  # runtime sẽ hỏi, không gửi qua context

        # Pick value theo priority
        if name in user_overrides:
            value = user_overrides[name]
        elif name in credentials:
            value = credentials[name]
        elif name in DEFAULT_CONTEXT_QCVN:
            value = DEFAULT_CONTEXT_QCVN[name]
        elif ftype in TYPE_DEFAULTS:
            value = TYPE_DEFAULTS[ftype]
        else:
            value = None

        if value is None or value == "":
            if required:
                missing_required.append(f"{name} (type={ftype})")
            continue

        context[name] = value

    if missing_required:
        print(f"[ERROR] Required fields không có value: {missing_required}")
        print(f"        Hint: set env SCENARIO_USERNAME/PASSWORD hoặc --context")
        sys.exit(2)

    return context


def _format_session(sess: dict) -> str:
    """Pretty 1-line session status."""
    return (
        f"status={sess['status']:18s}  "
        f"step={sess.get('current_step', 0):3d}/{sess.get('max_steps', 0):3d}"
    )


def _poll_session(api: ApiClient, session_id: str, max_wait_s: int = 600) -> dict:
    """Poll GET /v1/sessions/{id} mỗi 5s đến terminal status hoặc timeout.

    Terminal: done, failed, cancelled, timed_out
    """
    terminal = {"done", "failed", "cancelled", "timed_out"}
    started = time.monotonic()
    last_step = -1
    last_status = ""

    while True:
        elapsed = time.monotonic() - started
        if elapsed > max_wait_s:
            print(f"\n[TIMEOUT] Vượt {max_wait_s}s, session vẫn chưa terminal")
            return {"status": "polling_timeout"}

        try:
            sess = api.get(f"/v1/sessions/{session_id}")
        except requests.HTTPError as e:
            print(f"[ERROR] Poll fail: {e}")
            return {"status": "polling_error", "error": str(e)}

        status = sess["status"]
        step = sess.get("current_step", 0)

        # Print khi status hoặc step thay đổi
        if status != last_status or step != last_step:
            print(f"  [{int(elapsed):3d}s]  {_format_session(sess)}")
            last_status = status
            last_step = step

        if status in terminal:
            return sess

        time.sleep(5)


def _verify_cdn_url(url: str) -> tuple[bool, str]:
    """HEAD/GET URL → verify file accessible + có size."""
    try:
        # HEAD first
        r = requests.head(url, timeout=15, allow_redirects=True)
        if r.status_code == 405:
            # HEAD not allowed → fallback GET range
            r = requests.get(url, timeout=15, headers={"Range": "bytes=0-1023"})
        if r.status_code in (200, 206):
            size = r.headers.get("Content-Length", "?")
            content_type = r.headers.get("Content-Type", "?")
            return True, f"HTTP {r.status_code}, size={size}, type={content_type}"
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="E2E test mô phỏng Super Agent gọi tool-web"
    )
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO,
                        help="Scenario code (default: QCVN)")
    parser.add_argument("--version", type=int, default=None,
                        help="Revision version_no (default: published)")
    parser.add_argument("--context", default="{}",
                        help='JSON context overrides, vd \'{"so_hieu":"QCVN 86"}\'')
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument("--no-wait", action="store_true",
                        help="Chỉ POST, không poll status")
    parser.add_argument("--no-verify-cdn", action="store_true",
                        help="Skip verify cdn_url accessible")
    parser.add_argument("--max-wait", type=int, default=600,
                        help="Max polling seconds (default: 600 = 10 min)")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING)

    # Config
    base = os.getenv("API_BASE", DEFAULT_API_BASE).rstrip("/")
    user = os.getenv("API_USER", DEFAULT_USER)
    username = os.getenv("SCENARIO_USERNAME", "")
    password = os.getenv("SCENARIO_PASSWORD", "")
    user_overrides = json.loads(args.context) if args.context else {}

    print(f"[CONFIG] API base : {base}")
    print(f"[CONFIG] API user : {user}")
    print(f"[CONFIG] Scenario : {args.scenario}")
    print(f"[CONFIG] Username : {'set' if username else 'NOT SET'}")
    print(f"[CONFIG] Password : {'set' if password else 'NOT SET'}")
    print(f"[CONFIG] Overrides: {user_overrides}")

    api = ApiClient(base, user)

    # ── Step 1: Health check ──
    print("\n[1/6] Health check API…")
    try:
        h = api.get("/v1/health")
        print(f"      ✓ {h}")
    except Exception as e:
        print(f"      ✗ FAIL: {e}")
        sys.exit(1)

    # ── Step 2: Find revision (nếu user không truyền version) ──
    if args.version is None:
        print(f"\n[2/6] Lookup published revision của '{args.scenario}'…")
        try:
            detail = api.get(f"/v1/scenarios/{args.scenario}")
            pub_rev = detail.get("published_revision")
            latest_rev = detail.get("latest_revision")
            if pub_rev:
                args.version = pub_rev["version_no"]
                print(f"      ✓ published v{args.version} (rev_id={pub_rev['id']})")
            elif latest_rev:
                args.version = latest_rev["version_no"]
                print(f"      ⚠ chưa publish, dùng latest v{args.version} (rev_id={latest_rev['id']})")
            else:
                print(f"      ✗ Scenario chưa có revision nào")
                sys.exit(1)
        except Exception as e:
            print(f"      ✗ FAIL: {e}")
            sys.exit(1)
    else:
        print(f"\n[2/6] User chỉ định version_no={args.version}")

    # ── Step 3: Lấy inputs schema ──
    print(f"\n[3/6] GET input-fields schema cho '{args.scenario}' v{args.version}…")
    try:
        sch = api.get(
            f"/v1/scenarios/{args.scenario}/revisions/{args.version}/input-fields"
        )
        fields = sch["fields"]
        is_draft = sch["revision_is_draft"]
        print(f"      ✓ {len(fields)} fields, is_draft={is_draft}")
        for f in fields:
            req = "REQUIRED" if f["is_required"] else "optional"
            print(f"        - {f['name']:30s} type={f['field_type']:6s} {req}  source={f['source']}")
    except Exception as e:
        print(f"      ✗ FAIL: {e}")
        sys.exit(1)

    # ── Step 4: Build context ──
    print(f"\n[4/6] Build context dict theo schema…")
    credentials = {}
    for f in fields:
        # Map "username"/"password" name pattern → env credentials,
        # bất kể field_type (username thường type=string, password type=secret).
        if "user" in f["name"].lower() and username:
            credentials[f["name"]] = username
        elif "pass" in f["name"].lower() and password:
            credentials[f["name"]] = password

    context = _build_context(fields, user_overrides, credentials)
    print("      ✓ Context built:")
    for k, v in context.items():
        # Mask secret values trong log
        is_secret = any(
            f["name"] == k and f["field_type"] == "secret" for f in fields
        )
        display = "***" + str(v)[-2:] if is_secret and len(str(v)) > 2 else v
        print(f"        - {k:30s} = {display!r}")

    # ── Step 5: POST /v1/sessions ──
    print(f"\n[5/6] POST /v1/sessions…")
    payload = {
        "scenario": args.scenario,
        "context": context,
        "max_steps": args.max_steps,
    }
    try:
        resp = api.post("/v1/sessions", payload)
        session_id = resp["session_id"]
        print(f"      ✓ session_id={session_id}")
        print(f"        status={resp['status']}  queue_position={resp.get('queue_position')}")
    except Exception as e:
        print(f"      ✗ FAIL: {e}")
        sys.exit(1)

    if args.no_wait:
        print("\n[6/6] --no-wait → skip polling. Session đã enqueue.")
        print(f"      Poll manual: curl -H 'X-User-Id: {user}' '{base}/v1/sessions/{session_id}'")
        sys.exit(0)

    # ── Step 6: Poll status đến terminal ──
    print(f"\n[6/6] Poll status (max {args.max_wait}s)…")
    final = _poll_session(api, session_id, max_wait_s=args.max_wait)
    final_status = final.get("status", "?")
    print(f"\n  → Final status: {final_status}")
    if final.get("error_msg"):
        print(f"     error_msg: {final['error_msg']}")
    if final.get("duration_seconds"):
        print(f"     duration : {final['duration_seconds']}s")

    if final_status != "done":
        print("\n✗ Session KHÔNG done — test FAIL")
        print(f"  Detail: {json.dumps(final, indent=2, ensure_ascii=False)[:600]}")
        sys.exit(1)

    # ── Verify result + cdn_url ──
    print("\n[VERIFY] GET /v1/sessions/{id}/result…")
    try:
        result = api.get(f"/v1/sessions/{session_id}/result")
    except Exception as e:
        print(f"  ✗ Result endpoint fail: {e}")
        sys.exit(1)

    # Tìm download info trong result hoặc step cuối
    download_url = None
    download_name = None
    steps = result.get("steps") or []
    for step in steps:
        action = step.get("action") or {}
        if action.get("action") == "upload_download":
            download_url = action.get("downloaded_cdn_url")
            download_name = action.get("downloaded_filename")
            break

    if download_url:
        print(f"  ✓ Found download:")
        print(f"      filename: {download_name}")
        print(f"      cdn_url : {download_url}")

        if not args.no_verify_cdn:
            print("\n[VERIFY] Check cdn_url accessible…")
            ok, info = _verify_cdn_url(download_url)
            mark = "✓" if ok else "✗"
            print(f"  {mark} {info}")
            if not ok:
                sys.exit(1)
    else:
        print("  ⚠ Không có upload_download step trong result")
        print("    (scenario này có thể không có download — skip cdn verify)")

    print("\n" + "=" * 60)
    print(f"✓ E2E TEST PASS — session {session_id} done")
    print("=" * 60)


if __name__ == "__main__":
    main()
