"""
scripts/test_yaml_parse_after_sync.py — Smoke test verify raw_yaml có
AUTO-GENERATED markers vẫn parse được qua yaml_normalizer.

Mục đích: chống regression — sau khi yaml_sync regenerate raw_yaml với markers,
worker re-parse raw_yaml (nếu cần) vẫn ra ScenarioSpec hợp lệ.

Kiểm tra cho mỗi revision có markers:
  1. parse_ok = True (YAML syntax valid với comment markers)
  2. validation_ok = True (Pydantic spec valid)
  3. spec.inputs count == DB fields count
  4. spec.inputs[].name set khớp DB fields name set
  5. spec.inputs[].type khớp DB field_type
  6. spec.inputs[].required khớp DB is_required
  7. spec.inputs thứ tự khớp DB display_order
  8. yaml_hash = sha256(raw_yaml) — verify hash đã lưu DB đúng

Usage:
    $env:MYSQL_PASSWORD = "..."
    python ai_tool_web/scripts/test_yaml_parse_after_sync.py

Output: per-revision PASS/FAIL + summary.
Exit 0 nếu tất cả PASS, 1 nếu fail.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

# Setup sys.path. Chỉ insert ai_tool_web — yaml_normalizer.py tự handle
# LLM_base path internally (xem services/yaml_normalizer.py line 30).
# KHÔNG insert LLM_base trực tiếp vì LLM_base/services/ conflict với
# ai_tool_web/services/ (cùng package name).
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))                  # ai_tool_web

import aiomysql       # noqa: E402

from services.yaml_normalizer import normalize_yaml  # noqa: E402


_log = logging.getLogger("test_yaml_parse")


def _load_config():
    pwd = os.getenv("MYSQL_PASSWORD", "")
    if not pwd:
        print("[ERROR] MYSQL_PASSWORD chưa set", file=sys.stderr)
        sys.exit(2)
    return {
        "host":     os.getenv("MYSQL_HOST", "172.28.8.11"),
        "port":     int(os.getenv("MYSQL_PORT", "3306")),
        "user":     os.getenv("MYSQL_USER", "chatbotadmin"),
        "password": pwd,
        "db":       os.getenv("MYSQL_DATABASE", "changchatbot"),
    }


async def _fetch_revisions_with_markers(pool):
    """Lấy revisions có markers AUTO-GENERATED."""
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                SELECT
                    r.id, r.scenario_id, r.version_no, r.raw_yaml,
                    r.normalized_spec_json, r.yaml_hash, r.static_validation_status,
                    d.code AS scenario_code
                FROM scenario_revisions r
                JOIN scenario_definitions d ON r.scenario_id = d.id
                WHERE r.raw_yaml LIKE '%AUTO-GENERATED%'
                ORDER BY r.id DESC
                LIMIT 20
                """
            )
            return await cur.fetchall()


async def _fetch_db_fields(pool, rev_id):
    """Lấy DB fields của 1 revision, ORDER BY display_order ASC."""
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                SELECT id, name, display_label, field_type, is_required, source,
                       default_value, display_order
                FROM scenario_input_fields
                WHERE revision_id = %s
                ORDER BY display_order ASC, id ASC
                """,
                (rev_id,),
            )
            return await cur.fetchall()


def _test_one_revision(rev, db_fields):
    """Trả về (ok, list_of_messages). Test 8 thứ."""
    issues = []
    rev_id = rev["id"]
    raw = rev["raw_yaml"] or ""
    code = rev["scenario_code"]

    # 1. parse_ok
    result = normalize_yaml(raw, force_id=code)
    if not result.parse_ok:
        issues.append(f"parse_ok=False: {[e.message for e in result.errors[:3]]}")
        return False, issues

    # 2. validation_ok
    if not result.validation_ok:
        msgs = [f"{e.field}: {e.message}" for e in result.errors[:3]]
        issues.append(f"validation_ok=False: {msgs}")
        # Vẫn tiếp tục check khác — validation fail vẫn có thể có spec

    if not result.spec:
        issues.append("spec is None")
        return False, issues

    spec = result.spec
    spec_inputs = spec.inputs or []

    # 3. count
    if len(spec_inputs) != len(db_fields):
        issues.append(
            f"inputs count mismatch: YAML={len(spec_inputs)} vs DB={len(db_fields)}"
        )

    # 4. name set
    spec_names = {i.name for i in spec_inputs}
    db_names = {f["name"] for f in db_fields}
    if spec_names != db_names:
        only_yaml = spec_names - db_names
        only_db = db_names - spec_names
        if only_yaml:
            issues.append(f"names only in YAML: {sorted(only_yaml)}")
        if only_db:
            issues.append(f"names only in DB: {sorted(only_db)}")

    # 5-7. Per-field check (theo display_order)
    for idx, db_f in enumerate(db_fields):
        if idx >= len(spec_inputs):
            break
        spec_f = spec_inputs[idx]
        # name same position?
        if spec_f.name != db_f["name"]:
            issues.append(
                f"order[{idx}] name mismatch: YAML='{spec_f.name}' vs DB='{db_f['name']}'"
            )
        # type
        if spec_f.type != db_f["field_type"]:
            issues.append(
                f"field '{db_f['name']}' type: YAML='{spec_f.type}' vs DB='{db_f['field_type']}'"
            )
        # required
        if bool(spec_f.required) != bool(db_f["is_required"]):
            issues.append(
                f"field '{db_f['name']}' required: YAML={spec_f.required} vs DB={bool(db_f['is_required'])}"
            )

    # 8. yaml_hash khớp DB
    expected_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if rev["yaml_hash"] != expected_hash:
        issues.append(
            f"yaml_hash mismatch in DB: stored={rev['yaml_hash'][:12]} "
            f"vs computed={expected_hash[:12]}"
        )

    ok = len(issues) == 0
    return ok, issues


async def run():
    config = _load_config()
    masked = "***" + config["password"][-2:] if len(config["password"]) > 2 else "***"
    print(f"[CONNECT] {config['user']}:{masked}@{config['host']}:{config['port']}/{config['db']}")

    pool = await aiomysql.create_pool(
        host=config["host"], port=config["port"],
        user=config["user"], password=config["password"],
        db=config["db"], charset="utf8mb4",
        autocommit=True, minsize=1, maxsize=2,
    )

    total = 0
    passed = 0
    failed_revs = []

    try:
        revs = await _fetch_revisions_with_markers(pool)
        print(f"\n[LOAD] {len(revs)} revisions có AUTO-GENERATED markers")
        if not revs:
            print("⚠ Không có revision nào có markers. "
                  "Cần add field qua UI input_fields trước.")
            return 0

        for rev in revs:
            total += 1
            db_fields = await _fetch_db_fields(pool, rev["id"])
            ok, issues = _test_one_revision(rev, db_fields)
            mark = "✓ PASS" if ok else "✗ FAIL"
            print(
                f"  {mark}  rev_id={rev['id']:3d} "
                f"v{rev['version_no']:2d}  "
                f"code={rev['scenario_code']}  "
                f"({len(db_fields)} DB fields)"
            )
            if ok:
                passed += 1
            else:
                failed_revs.append((rev, issues))
                for msg in issues:
                    print(f"           └─ {msg}")

    finally:
        pool.close()
        await pool.wait_closed()

    print("\n" + "=" * 60)
    print(f"SUMMARY: {passed}/{total} PASS")
    print("=" * 60)
    if failed_revs:
        print("\nFailed revisions:")
        for rev, issues in failed_revs:
            print(f"  rev_id={rev['id']} ({rev['scenario_code']} v{rev['version_no']}):")
            for msg in issues:
                print(f"    - {msg}")
        return 1
    print("✓ Tất cả raw_yaml có markers parse thành công + sync với DB fields")
    return 0


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING)
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
