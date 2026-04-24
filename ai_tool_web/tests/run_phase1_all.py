"""
Master runner — chạy tất cả test Phase 1 theo thứ tự dependency.

Usage:
    cd deploy_server/ai_tool_web
    python tests/run_phase1_all.py

Exit 0 = all pass (GATE 2 OK), 1 = có fail.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


_TESTS_DIR = Path(__file__).parent
_ROOT = _TESTS_DIR.parent


# Thứ tự từ thấp → cao layer (fail sớm ở layer thấp → không chạy layer cao)
_TEST_FILES = [
    ("Repository (SQLite)", "tests/test_scenario_repo.py"),
    ("Auth (Mock)", "tests/test_mock_auth.py"),
    ("YAML normalizer", "tests/test_yaml_normalizer.py"),
    ("Inputs validator", "tests/test_inputs_validator.py"),
    ("Service layer", "tests/test_user_scenario_service.py"),
    ("API routes", "tests/test_user_scenarios_api.py"),
    ("Integration E2E (GATE 2)", "tests/test_integration_phase1.py"),
]


def run_one(label: str, rel_path: str) -> tuple[bool, float, str]:
    """Chạy 1 test file. Return (ok, duration_s, last_line)."""
    start = time.time()
    proc = subprocess.run(
        [sys.executable, rel_path],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    dur = time.time() - start
    ok = proc.returncode == 0

    # Last non-empty line để summary
    all_lines = (proc.stdout + proc.stderr).strip().splitlines()
    last = next(
        (ln for ln in reversed(all_lines)
         if ln.strip() and not ln.strip().startswith("[PASS]")),
        ""
    )
    return ok, dur, last[:120]


def main() -> int:
    print("=" * 70)
    print("Phase 1 Test Suite — User-Configurable Scenario")
    print("=" * 70)

    results: list[tuple[str, bool, float, str]] = []
    total_start = time.time()

    for label, path in _TEST_FILES:
        print(f"\n▶ {label}  ({path})")
        ok, dur, last = run_one(label, path)
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"  {status}  ({dur:.2f}s)  {last}")
        results.append((label, ok, dur, last))
        if not ok:
            # Fail-fast: layer thấp fail → layer cao chắc chắn fail
            print(f"\n⚠  Stop at '{label}' — fix trước khi chạy tiếp.")
            break

    total_dur = time.time() - total_start

    print("\n" + "=" * 70)
    print(f"Summary ({total_dur:.2f}s total):")
    print("=" * 70)
    for label, ok, dur, _ in results:
        tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}] {label:30s}  {dur:6.2f}s")

    all_ok = all(r[1] for r in results) and len(results) == len(_TEST_FILES)
    print()
    if all_ok:
        print("🎉 GATE 2 — ALL PHASE 1 BACKEND TESTS PASS")
        return 0
    print("❌ Fail. Xem log chi tiết bên trên.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
