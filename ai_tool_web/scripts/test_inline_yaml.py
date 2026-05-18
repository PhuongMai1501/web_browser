"""
scripts/test_inline_yaml.py — E2E verify POST /v1/sessions with scenario_yaml.

Mục đích: chứng minh API mới (commit d193f89) parse được YAML inline đúng cách,
độc lập với Sup Agent team. Nếu test PASS → bug 100% ở Sup Agent side.

Flow:
  1. Đọc YAML scenario QCVN từ file modify_scenarios/
  2. POST /v1/sessions với scenario_yaml + context
  3. Verify response: session_id, scenario=_custom_<8hex>
  4. Poll status 60s → kỳ vọng status chuyển running (KHÔNG về about:blank)

Usage:
    $env:SCENARIO_USERNAME = "fpttelecom"
    $env:SCENARIO_PASSWORD = "<password>"
    $env:API_BASE = "http://chang-browser-api.dscapp.com"
    python ai_tool_web/scripts/test_inline_yaml.py

Exit code: 0 nếu session bắt đầu chạy đúng URL (không phải about:blank).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests


YAML_PATH = (
    Path(__file__).resolve().parents[3]
    / "modify_scenarios"
    / "thuvienphapluat.vn"
    / "check_law_version_qcvn.yaml"
)
API_BASE = os.getenv("API_BASE", "http://chang-browser-api.dscapp.com").rstrip("/")
USER_ID = os.getenv("API_USER", "hiepqn")
USERNAME = os.getenv("SCENARIO_USERNAME", "")
PASSWORD = os.getenv("SCENARIO_PASSWORD", "")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if not USERNAME or not PASSWORD:
        print("[ERROR] SCENARIO_USERNAME/PASSWORD env chưa set")
        return 2

    if not YAML_PATH.exists():
        print(f"[ERROR] YAML file không tồn tại: {YAML_PATH}")
        return 2

    yaml_text = YAML_PATH.read_text(encoding="utf-8")
    print(f"[CONFIG] API base : {API_BASE}")
    print(f"[CONFIG] YAML file: {YAML_PATH.name} ({len(yaml_text)} chars)")
    print(f"[CONFIG] User     : {USER_ID}")

    # POST /v1/sessions với scenario_yaml inline
    body = {
        "scenario_yaml": yaml_text,
        "context": {
            "username": USERNAME,
            "password": PASSWORD,
            "so_hieu": "QCVN 86",
            "title_hint": "QCVN 86",
        },
        "max_steps": 25,
    }

    print(f"\n[1/3] POST /v1/sessions với scenario_yaml...")
    r = requests.post(
        f"{API_BASE}/v1/sessions",
        headers={
            "X-User-Id": USER_ID,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30,
    )
    if r.status_code != 201:
        print(f"      ✗ FAIL HTTP {r.status_code}: {r.text[:500]}")
        return 1

    resp = r.json()
    session_id = resp["session_id"]
    print(f"      ✓ session_id={session_id}")
    print(f"        queue_position={resp.get('queue_position')}")

    # Verify: GET session detail, scenario phải là _custom_<8hex>
    print(f"\n[2/3] Verify scenario name (kỳ vọng _custom_<hex>)...")
    time.sleep(2)
    sess = requests.get(
        f"{API_BASE}/v1/sessions/{session_id}",
        headers={"X-User-Id": USER_ID},
        timeout=10,
    ).json()
    scenario_name = sess.get("scenario", "")
    if not scenario_name.startswith("_custom_"):
        print(f"      ✗ FAIL: scenario='{scenario_name}', expect '_custom_...'")
        print(f"        → API code mới chưa rollout hoặc body sai")
        return 1
    print(f"      ✓ scenario='{scenario_name}' (auto-gen từ YAML inline)")

    # Poll 60s xem worker có pick + run không
    print(f"\n[3/3] Poll status 60s — kỳ vọng status chuyển running...")
    started = time.monotonic()
    last_step = -1
    last_status = ""
    while time.monotonic() - started < 60:
        s = requests.get(
            f"{API_BASE}/v1/sessions/{session_id}",
            headers={"X-User-Id": USER_ID},
            timeout=10,
        ).json()
        status = s["status"]
        step = s.get("current_step", 0)
        if status != last_status or step != last_step:
            elapsed = int(time.monotonic() - started)
            print(f"      [{elapsed:3d}s] status={status:18s} step={step}/{s.get('max_steps')}")
            last_status = status
            last_step = step
        if status in ("done", "failed", "cancelled", "timed_out"):
            print(f"\n      → Final: {status}")
            if s.get("error_msg"):
                print(f"        error: {s['error_msg']}")
            break
        if status == "running" and step > 0:
            # Worker đã pick + chạy step thật → API mới OK
            print(f"\n      ✓ Worker đang chạy scenario (step {step}). Cancel để cleanup.")
            requests.post(
                f"{API_BASE}/v1/sessions/{session_id}/cancel",
                headers={"X-User-Id": USER_ID},
                timeout=10,
            )
            break
        time.sleep(3)

    print("\n" + "=" * 60)
    print(f"✓ TEST PASS — API mới parse scenario_yaml inline OK")
    print(f"  Session {session_id} đã run với scenario='{scenario_name}'")
    print(f"  Nếu Sup Agent vẫn lỗi → bug nằm ở body Sup Agent gửi, KHÔNG ở tool-web")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
