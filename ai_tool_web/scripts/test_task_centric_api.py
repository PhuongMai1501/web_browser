"""
scripts/test_task_centric_api.py — Test POST /v1/tasks/{task_id}/run với 4 case.

Mục đích: Xem response trả về cho từng mode, đặc biệt feature mới
"prioritize scenario_yaml khi cả 2 cùng có" (commit 0f2b5bd).

Case test:
  1. Mode query — LLM gen YAML từ NL
  2. Mode scenario_yaml — paste YAML inline
  3. Mode BOTH (query + scenario_yaml) — backend mới ưu tiên yaml, ignore query
  4. Edge case — YAML invalid + có query kèm → fail-fast 422 (KHÔNG fallback gen LLM)

Config hardcode ở đầu file — anh sửa rồi chạy luôn không cần env var.

Usage (PowerShell):
    cd D:\\research\\ChangAI\\web_brower
    python dev/deploy_server/ai_tool_web/scripts/test_task_centric_api.py

    # Hoặc chỉ chạy 1 case:
    python dev/deploy_server/ai_tool_web/scripts/test_task_centric_api.py --only 3

    # Skip case query để khỏi tốn LLM call:
    python dev/deploy_server/ai_tool_web/scripts/test_task_centric_api.py --skip 1,3
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

import requests

# Windows PowerShell default cp1252 không encode được tiếng Việt — force UTF-8.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── Config — sửa trực tiếp ở đây ────────────────────────────────────────────
API_BASE = "http://chang-browser-api.dscapp.com"
USER_ID = "hiepqn-test"
TIMEOUT_S = 30  # query mode cần ~5-10s cho LLM gen
# ─────────────────────────────────────────────────────────────────────────────


def new_task_id(suffix: str = "") -> str:
    """Gen task_id mới khớp pattern Sup Agent."""
    base = f"t-{uuid.uuid4().hex[:10]}"
    return f"{base}-{suffix}" if suffix else base


def print_section(title: str) -> None:
    print()
    print("=" * 76)
    print(f"  {title}")
    print("=" * 76)


def print_request(method: str, url: str, body: dict) -> None:
    print(f"\n→ {method} {url}")
    print("  Headers: X-User-Id:", USER_ID)
    print("  Body:")
    print("    " + json.dumps(body, indent=2, ensure_ascii=False).replace("\n", "\n    "))


def print_response(resp: requests.Response, highlight_fields: list[str]) -> dict:
    print(f"\n← HTTP {resp.status_code} {resp.reason}")
    try:
        data = resp.json()
    except Exception:
        print("  (body không phải JSON):")
        print("  " + resp.text)
        return {}

    # Pretty-print full response
    print("  Body:")
    print("    " + json.dumps(data, indent=2, ensure_ascii=False).replace("\n", "\n    "))

    # Highlight key fields
    if highlight_fields:
        print("\n  📌 Key fields:")
        for f in highlight_fields:
            value = data.get(f)
            if isinstance(value, str) and len(value) > 80:
                display = value[:77] + "..."
            else:
                display = repr(value)
            print(f"     {f:30s} = {display}")
    return data


def run_case_1_query_only(task_id: str) -> dict:
    """Case 1: Chỉ truyền query — LLM gen YAML."""
    print_section("CASE 1: Mode `query` — LLM gen YAML từ NL")
    body = {
        "query": "Mở Google rồi search 'chang chatbot fpt'",
        "query_site_hint": "google.com",
        "max_steps": 5,
    }
    url = f"{API_BASE}/v1/tasks/{task_id}/run"
    print_request("POST", url, body)

    resp = requests.post(
        url, json=body,
        headers={"X-User-Id": USER_ID, "Content-Type": "application/json"},
        timeout=TIMEOUT_S,
    )
    data = print_response(
        resp,
        highlight_fields=[
            "task_id", "iteration", "session_id",
            "generated_from_query", "scenario_id", "model_used",
            "tokens_in", "tokens_out",
            "scenario_yaml",  # FULL YAML (sẽ truncate hiển thị)
        ],
    )

    if data.get("scenario_yaml"):
        print("\n  📄 Full scenario_yaml (LLM gen):")
        print("  " + "─" * 70)
        for line in data["scenario_yaml"].splitlines():
            print(f"    {line}")
        print("  " + "─" * 70)

    return data


def run_case_2_yaml_only(task_id: str) -> dict:
    """Case 2: Chỉ truyền scenario_yaml — không gọi LLM."""
    print_section("CASE 2: Mode `scenario_yaml` — Paste YAML inline")
    yaml_text = (
        "id: test_yaml_only\n"
        "display_name: Test YAML inline\n"
        "mode: flow\n"
        "start_url: https://www.google.com\n"
        "allowed_domains:\n"
        "  - google.com\n"
        "max_steps_default: 5\n"
        "steps:\n"
        "  - action: goto\n"
        "    url: https://www.google.com\n"
    )
    body = {
        "scenario_yaml": yaml_text,
        "max_steps": 5,
    }
    url = f"{API_BASE}/v1/tasks/{task_id}/run"
    print_request("POST", url, body)

    resp = requests.post(
        url, json=body,
        headers={"X-User-Id": USER_ID, "Content-Type": "application/json"},
        timeout=TIMEOUT_S,
    )
    return print_response(
        resp,
        highlight_fields=[
            "task_id", "iteration", "session_id",
            "generated_from_query", "scenario_yaml", "scenario_id",
            "model_used", "tokens_in", "tokens_out",
            "cancelled_prev_count",
        ],
    )


def run_case_3_both(task_id: str) -> dict:
    """Case 3: Cả query + scenario_yaml — backend MỚI ưu tiên yaml, ignore query.

    Đây là feature mới (commit 0f2b5bd). Trước đây API reject 422.
    """
    print_section("CASE 3: BOTH query + scenario_yaml — Feature mới (prioritize yaml)")
    yaml_text = (
        "id: test_both_fields\n"
        "display_name: Test cả 2 field\n"
        "mode: flow\n"
        "start_url: https://www.google.com\n"
        "allowed_domains:\n"
        "  - google.com\n"
        "max_steps_default: 5\n"
        "steps:\n"
        "  - action: goto\n"
        "    url: https://www.google.com\n"
    )
    body = {
        "query": "Query này sẽ BỊ IGNORE vì có scenario_yaml — chỉ là metadata audit",
        "scenario_yaml": yaml_text,
        "max_steps": 5,
    }
    url = f"{API_BASE}/v1/tasks/{task_id}/run"
    print_request("POST", url, body)
    print("\n  💡 Expect: generated_from_query=false, scenario_yaml=null, tokens=null")
    print("     (giống Case 2, query chỉ là metadata server-side log)")

    resp = requests.post(
        url, json=body,
        headers={"X-User-Id": USER_ID, "Content-Type": "application/json"},
        timeout=TIMEOUT_S,
    )
    return print_response(
        resp,
        highlight_fields=[
            "task_id", "iteration", "session_id",
            "generated_from_query",  # ← MUST be false
            "scenario_yaml",          # ← MUST be null
            "model_used",             # ← MUST be null
            "tokens_in", "tokens_out",  # ← MUST be null
        ],
    )


def run_case_4_invalid_yaml_with_query(task_id: str) -> dict:
    """Case 4: YAML invalid + có query kèm → 422 fail-fast (KHÔNG fallback gen LLM)."""
    print_section("CASE 4: YAML invalid + có query → 422 fail-fast")
    body = {
        "query": "Backup query nếu YAML hỏng — nhưng API KHÔNG fallback gen",
        "scenario_yaml": "id: broken yaml :::\n not valid yaml at all $$$",
        "max_steps": 5,
    }
    url = f"{API_BASE}/v1/tasks/{task_id}/run"
    print_request("POST", url, body)
    print("\n  💡 Expect: HTTP 422, detail: 'YAML inline parse fail: ...'")
    print("     (API log WARNING với query gốc để debug — xem kubectl logs)")

    resp = requests.post(
        url, json=body,
        headers={"X-User-Id": USER_ID, "Content-Type": "application/json"},
        timeout=TIMEOUT_S,
    )
    return print_response(
        resp,
        highlight_fields=["detail"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only", type=str, default="",
        help="Chỉ chạy 1 case (vd '--only 3'). Mặc định chạy hết.",
    )
    parser.add_argument(
        "--skip", type=str, default="",
        help="Skip case (vd '--skip 1,4' để bỏ qua LLM call). Comma-separated.",
    )
    args = parser.parse_args()

    cases = [
        (1, "query_only", run_case_1_query_only),
        (2, "yaml_only", run_case_2_yaml_only),
        (3, "both", run_case_3_both),
        (4, "invalid_with_query", run_case_4_invalid_yaml_with_query),
    ]

    if args.only:
        try:
            only_id = int(args.only.strip())
            cases = [c for c in cases if c[0] == only_id]
        except ValueError:
            print(f"❌ --only phải là số. Got: {args.only!r}")
            return 1

    if args.skip:
        try:
            skip_ids = {int(x.strip()) for x in args.skip.split(",") if x.strip()}
            cases = [c for c in cases if c[0] not in skip_ids]
        except ValueError:
            print(f"❌ --skip phải là số. Got: {args.skip!r}")
            return 1

    if not cases:
        print("❌ Không có case nào để chạy.")
        return 1

    print(f"🌐 API base: {API_BASE}")
    print(f"👤 User ID:  {USER_ID}")
    print(f"📋 Cases:    {[c[1] for c in cases]}")

    # Mỗi case tạo task_id riêng để không bị auto-cancel lẫn nhau.
    results = {}
    for case_id, name, fn in cases:
        tid = new_task_id(suffix=name)
        try:
            results[case_id] = fn(tid)
        except requests.exceptions.RequestException as e:
            print(f"\n❌ Case {case_id} network error: {e}")
            results[case_id] = None
        except Exception as e:
            print(f"\n❌ Case {case_id} unexpected error: {e}")
            results[case_id] = None

    # Summary
    print_section("📊 SUMMARY")
    for case_id, name, _ in cases:
        data = results.get(case_id)
        if data is None:
            status = "❌ NETWORK/ERROR"
        elif "detail" in data and case_id != 4:
            status = f"❌ {data.get('detail', '?')[:60]}"
        elif "session_id" in data:
            iter_n = data.get("iteration", "?")
            from_query = data.get("generated_from_query")
            extra = "(LLM gen)" if from_query else "(no LLM)"
            status = f"✅ iter={iter_n} session={data['session_id'][:8]}… {extra}"
        elif case_id == 4 and data.get("detail"):
            status = f"✅ 422 as expected: {data['detail'][:60]}"
        else:
            status = "❓ unknown"
        print(f"  Case {case_id} {name:25s} {status}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
