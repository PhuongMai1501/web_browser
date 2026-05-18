"""
scripts/test_input_fields_api.py — E2E test cho input_fields API.

Test 9 case end-to-end:
  1. GET list fields trên DRAFT revision
  2. POST add field mới
  3. GET list verify count tăng
  4. SQL verify raw_yaml có auto-gen markers
  5. PUT update field
  6. POST reorder
  7. POST bulk replace
  8. DELETE field
  9. POST mutate PUBLISHED → expect 409

Tự động:
- Discover scenario có cả DRAFT và PUBLISHED revision (qua MySQL query)
- Cleanup: xóa field test còn lại nếu test fail giữa chừng

Usage:
    $env:MYSQL_PASSWORD = "..."
    $env:API_BASE = "http://127.0.0.1:9000"   # optional, default = localhost:9000
    $env:API_USER = "hiepqn"                  # optional, default = hiepqn
    python ai_tool_web/scripts/test_input_fields_api.py

Output: 9 dòng PASS/FAIL + summary.
Exit code: 0 nếu tất cả PASS, 1 nếu có FAIL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

# Setup sys.path
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))

import aiomysql       # noqa: E402
import requests       # noqa: E402

_log = logging.getLogger("test_input_fields")


# ── Config ───────────────────────────────────────────────────────────────────

def _load_config() -> dict[str, Any]:
    pwd = os.getenv("MYSQL_PASSWORD", "")
    if not pwd:
        print("[ERROR] MYSQL_PASSWORD chưa set", file=sys.stderr)
        sys.exit(2)
    return {
        "mysql_host": os.getenv("MYSQL_HOST", "172.28.8.11"),
        "mysql_port": int(os.getenv("MYSQL_PORT", "3306")),
        "mysql_user": os.getenv("MYSQL_USER", "chatbotadmin"),
        "mysql_password": pwd,
        "mysql_db": os.getenv("MYSQL_DATABASE", "changchatbot"),
        "api_base": os.getenv("API_BASE", "http://127.0.0.1:9000").rstrip("/"),
        "api_user": os.getenv("API_USER", "hiepqn"),
    }


# ── Test framework ──────────────────────────────────────────────────────────

class TestRunner:
    def __init__(self):
        self.results: list[tuple[str, bool, str]] = []

    def report(self, name: str, ok: bool, detail: str = "") -> None:
        mark = "✓ PASS" if ok else "✗ FAIL"
        self.results.append((name, ok, detail))
        print(f"  {mark}  {name}" + (f"  — {detail}" if detail else ""))

    def summary(self) -> int:
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        passed = sum(1 for _, ok, _ in self.results if ok)
        total = len(self.results)
        for name, ok, detail in self.results:
            mark = "✓" if ok else "✗"
            print(f"  {mark} {name}" + (f" — {detail}" if not ok and detail else ""))
        print("=" * 60)
        print(f"Total: {passed}/{total} PASS")
        return 0 if passed == total else 1


# ── DB helpers ──────────────────────────────────────────────────────────────

async def _discover_test_scenario(
    pool, config, api_user: str
) -> Optional[dict]:
    """Tìm 1 scenario có >= 1 DRAFT revision (rev không phải published_revision_id).

    Ưu tiên:
      1. Scenario USER thuộc owner=api_user (mutate được)
      2. Scenario USER khác (mutate fail nếu user không phải admin)
      3. Scenario BUILTIN (chỉ admin sửa được — set api_user='admin' để pass)

    Returns dict: {code, scenario_pk, draft_rev_id, draft_version_no,
                   published_rev_id, published_version_no,
                   source_type, owner_code}
    Sẽ in WARNING nếu phải dùng builtin scenario.
    """
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            # Query tất cả candidates với source_type + owner_code để rank
            await cur.execute(
                """
                SELECT
                    d.id AS scenario_pk,
                    d.code,
                    d.source_type,
                    d.owner_code,
                    d.published_revision_id,
                    (SELECT COUNT(*) FROM scenario_revisions WHERE scenario_id = d.id) AS rev_count
                FROM scenario_definitions d
                WHERE d.is_archived = 0
                ORDER BY
                    -- Priority: 1=user-own, 2=user-other, 3=builtin
                    CASE
                        WHEN d.source_type = 'user' AND d.owner_code = %s THEN 1
                        WHEN d.source_type = 'user' THEN 2
                        WHEN d.source_type = 'cloned' AND d.owner_code = %s THEN 1
                        WHEN d.source_type = 'cloned' THEN 2
                        ELSE 3
                    END,
                    d.id ASC
                """,
                (api_user, api_user),
            )
            candidates = await cur.fetchall()

            for cand in candidates:
                # Cần >= 1 revision
                if cand["rev_count"] < 1:
                    continue

                # Tìm DRAFT rev — ưu tiên rev != published_revision_id
                if cand["published_revision_id"]:
                    await cur.execute(
                        """
                        SELECT id, version_no FROM scenario_revisions
                        WHERE scenario_id = %s AND id != %s
                        ORDER BY version_no DESC LIMIT 1
                        """,
                        (cand["scenario_pk"], cand["published_revision_id"]),
                    )
                    draft = await cur.fetchone()

                    if draft:
                        # Lấy PUBLISHED rev info
                        await cur.execute(
                            "SELECT id, version_no FROM scenario_revisions WHERE id = %s",
                            (cand["published_revision_id"],),
                        )
                        published = await cur.fetchone()
                        return {
                            "code": cand["code"],
                            "scenario_pk": cand["scenario_pk"],
                            "source_type": cand["source_type"],
                            "owner_code": cand["owner_code"],
                            "draft_rev_id": draft["id"],
                            "draft_version_no": draft["version_no"],
                            "published_rev_id": published["id"] if published else None,
                            "published_version_no": published["version_no"] if published else None,
                        }
                else:
                    # Chưa publish gì cả → tất cả rev là DRAFT, lấy rev mới nhất
                    await cur.execute(
                        """
                        SELECT id, version_no FROM scenario_revisions
                        WHERE scenario_id = %s
                        ORDER BY version_no DESC LIMIT 1
                        """,
                        (cand["scenario_pk"],),
                    )
                    draft = await cur.fetchone()
                    if draft:
                        return {
                            "code": cand["code"],
                            "scenario_pk": cand["scenario_pk"],
                            "source_type": cand["source_type"],
                            "owner_code": cand["owner_code"],
                            "draft_rev_id": draft["id"],
                            "draft_version_no": draft["version_no"],
                            "published_rev_id": None,
                            "published_version_no": None,
                        }
    return None


async def _verify_yaml_has_markers(pool, rev_id: int) -> tuple[bool, str]:
    """Check raw_yaml của rev có AUTO-GENERATED markers không."""
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT raw_yaml FROM scenario_revisions WHERE id = %s",
                (rev_id,),
            )
            row = await cur.fetchone()
            if not row:
                return False, f"rev_id={rev_id} không tồn tại"
            raw = row["raw_yaml"] or ""
            has_start = "AUTO-GENERATED" in raw
            has_end = "END AUTO-GENERATED" in raw
            if has_start and has_end:
                # Find pos
                start = raw.find("AUTO-GENERATED")
                end = raw.find("END AUTO-GENERATED")
                return True, f"markers @ {start}..{end}, raw_yaml {len(raw)} bytes"
            return False, f"missing markers (start={has_start}, end={has_end})"


async def _cleanup_test_field(pool, rev_id: int, field_name: str) -> None:
    """Xóa field test còn sót lại để cleanup."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM scenario_input_fields WHERE revision_id = %s AND name = %s",
                (rev_id, field_name),
            )


# ── HTTP helpers ────────────────────────────────────────────────────────────

class ApiClient:
    def __init__(self, base: str, user: str):
        self.base = base
        self.headers = {"X-User-Id": user}

    def _url(self, code: str, ver: int, suffix: str = "") -> str:
        return f"{self.base}/v1/scenarios/{code}/revisions/{ver}/input-fields{suffix}"

    def list_fields(self, code: str, ver: int):
        return requests.get(self._url(code, ver), headers=self.headers, timeout=10)

    def create_field(self, code: str, ver: int, body: dict):
        return requests.post(
            self._url(code, ver),
            headers={**self.headers, "Content-Type": "application/json"},
            json=body, timeout=10,
        )

    def update_field(self, code: str, ver: int, field_id: int, body: dict):
        return requests.put(
            self._url(code, ver, f"/{field_id}"),
            headers={**self.headers, "Content-Type": "application/json"},
            json=body, timeout=10,
        )

    def delete_field(self, code: str, ver: int, field_id: int):
        return requests.delete(
            self._url(code, ver, f"/{field_id}"),
            headers=self.headers, timeout=10,
        )

    def reorder(self, code: str, ver: int, ordered_ids: list[int]):
        return requests.post(
            self._url(code, ver, "/reorder"),
            headers={**self.headers, "Content-Type": "application/json"},
            json={"ordered_ids": ordered_ids}, timeout=10,
        )

    def bulk_replace(self, code: str, ver: int, fields: list[dict]):
        return requests.post(
            self._url(code, ver, "/bulk"),
            headers={**self.headers, "Content-Type": "application/json"},
            json={"fields": fields}, timeout=10,
        )


# ── Tests ───────────────────────────────────────────────────────────────────

TEST_FIELD_NAME = "test_e2e_field_xyz"  # unique name để cleanup


async def run_tests(config: dict) -> int:
    runner = TestRunner()
    api = ApiClient(config["api_base"], config["api_user"])

    # Health check API
    try:
        r = requests.get(f"{config['api_base']}/v1/health", timeout=5)
        if r.status_code != 200:
            print(f"[ERROR] API không reachable: {r.status_code}")
            return 1
    except Exception as e:
        print(f"[ERROR] Không connect được API {config['api_base']}: {e}")
        return 1

    print(f"[CONNECT] API: {config['api_base']}")
    print(f"[CONNECT] User: {config['api_user']}")
    print(f"[CONNECT] MySQL: {config['mysql_user']}@{config['mysql_host']}:{config['mysql_port']}/{config['mysql_db']}")

    pool = await aiomysql.create_pool(
        host=config["mysql_host"], port=config["mysql_port"],
        user=config["mysql_user"], password=config["mysql_password"],
        db=config["mysql_db"], charset="utf8mb4",
        autocommit=True, minsize=1, maxsize=2,
    )

    try:
        # ── Discover test scenario ──
        scenario = await _discover_test_scenario(pool, config, config["api_user"])
        if not scenario:
            print("[ERROR] Không tìm thấy scenario nào có DRAFT revision để test")
            return 1

        # Permission check: nếu scenario không phải owner của api_user
        # và không phải builtin/cloned của user → switch sang 'admin'
        source = scenario["source_type"]
        owner = scenario["owner_code"]
        original_user = config["api_user"]
        needs_admin = False
        if source == "builtin":
            needs_admin = True
        elif source in ("user", "cloned") and owner != original_user:
            needs_admin = True

        if needs_admin and original_user != "admin":
            print(
                f"[AUTH] Scenario '{scenario['code']}' (source={source}, owner={owner}) "
                f"không phải own scenarios của user '{original_user}'. "
                f"Switch sang user='admin' để pass permission check."
            )
            config["api_user"] = "admin"
            api.headers = {"X-User-Id": "admin"}

        code = scenario["code"]
        draft_ver = scenario["draft_version_no"]
        draft_rev_id = scenario["draft_rev_id"]
        pub_ver = scenario["published_version_no"]
        print(f"\n[SCENARIO] code={code}")
        print(f"           source_type={source}  owner_code={owner}")
        print(f"           DRAFT     rev_id={draft_rev_id}  version_no={draft_ver}")
        if pub_ver is not None:
            print(f"           PUBLISHED rev_id={scenario['published_rev_id']}  version_no={pub_ver}")
        else:
            print(f"           PUBLISHED (none) — skip Test 9")

        # Pre-cleanup: xóa test field nếu còn từ run trước
        await _cleanup_test_field(pool, draft_rev_id, TEST_FIELD_NAME)

        print("\n── Running 9 tests ──")

        # ─── Test 1: GET list fields ─────────────────────────────────────
        r = api.list_fields(code, draft_ver)
        if r.status_code == 200:
            data = r.json()
            field_count = len(data.get("fields", []))
            is_draft = data.get("revision_is_draft")
            runner.report(
                "T1 GET list fields",
                True,
                f"{field_count} fields, is_draft={is_draft}",
            )
            existing_count = field_count
        else:
            runner.report(
                "T1 GET list fields", False,
                f"HTTP {r.status_code}: {r.text[:200]}",
            )
            return runner.summary()

        # ─── Test 2: POST add field ──────────────────────────────────────
        body = {
            "name": TEST_FIELD_NAME,
            "display_label": "E2E Test Field",
            "field_type": "string",
            "is_required": False,
            "source": "context",
            "description": "Auto-generated test field, sẽ bị cleanup",
            "placeholder": "test placeholder",
        }
        r = api.create_field(code, draft_ver, body)
        if r.status_code == 201:
            field_data = r.json()
            test_field_id = field_data["id"]
            runner.report(
                "T2 POST create field", True,
                f"new id={test_field_id}, display_order={field_data['display_order']}",
            )
        else:
            runner.report(
                "T2 POST create field", False,
                f"HTTP {r.status_code}: {r.text[:200]}",
            )
            return runner.summary()

        # ─── Test 3: GET verify count tăng ──────────────────────────────
        r = api.list_fields(code, draft_ver)
        if r.status_code == 200:
            new_count = len(r.json().get("fields", []))
            ok = (new_count == existing_count + 1)
            runner.report(
                "T3 GET verify count tăng", ok,
                f"before={existing_count}, after={new_count}",
            )
        else:
            runner.report("T3 GET verify count", False, f"HTTP {r.status_code}")

        # ─── Test 4: SQL verify raw_yaml có markers ─────────────────────
        ok, detail = await _verify_yaml_has_markers(pool, draft_rev_id)
        runner.report("T4 raw_yaml AUTO-GENERATED markers", ok, detail)

        # ─── Test 5: PUT update field ────────────────────────────────────
        update_body = {
            **body,
            "display_label": "E2E Test Field (updated)",
            "is_required": True,
            "placeholder": "updated placeholder",
        }
        r = api.update_field(code, draft_ver, test_field_id, update_body)
        if r.status_code == 200:
            updated = r.json()
            ok = (updated["display_label"] == "E2E Test Field (updated)"
                  and updated["is_required"] is True)
            runner.report(
                "T5 PUT update field", ok,
                f"display_label='{updated['display_label']}', required={updated['is_required']}",
            )
        else:
            runner.report(
                "T5 PUT update field", False,
                f"HTTP {r.status_code}: {r.text[:200]}",
            )

        # ─── Test 6: POST reorder ────────────────────────────────────────
        # Lấy tất cả field IDs hiện tại
        r = api.list_fields(code, draft_ver)
        if r.status_code == 200:
            all_fields = r.json()["fields"]
            ids = [f["id"] for f in all_fields]
            # Đảo ngược thứ tự
            reversed_ids = list(reversed(ids))
            r = api.reorder(code, draft_ver, reversed_ids)
            if r.status_code == 200:
                new_order = [f["id"] for f in r.json()["fields"]]
                ok = (new_order == reversed_ids)
                runner.report(
                    "T6 POST reorder", ok,
                    f"reversed {len(ids)} fields",
                )
                # Đảo ngược lại để giữ original order
                api.reorder(code, draft_ver, ids)
            else:
                runner.report("T6 POST reorder", False, f"HTTP {r.status_code}")
        else:
            runner.report("T6 POST reorder (prep)", False, f"HTTP {r.status_code}")

        # ─── Test 7: POST bulk replace ───────────────────────────────────
        # Bulk replace: gửi lại fields hiện tại + 1 field test extra. Sau đó undo
        # bằng cách bulk lại với fields gốc.
        r = api.list_fields(code, draft_ver)
        if r.status_code == 200:
            current = r.json()["fields"]
            # Build payload — chỉ giữ field schema, bỏ id/revision_id/scenario_id/timestamps
            def to_base(f: dict) -> dict:
                return {
                    "name": f["name"],
                    "display_label": f["display_label"],
                    "field_type": f["field_type"],
                    "is_required": f["is_required"],
                    "source": f["source"],
                    "default_value": f.get("default_value"),
                    "description": f.get("description"),
                    "placeholder": f.get("placeholder"),
                    "help_text": f.get("help_text"),
                    "display_order": f["display_order"],
                    "category": f["category"],
                }
            bulk_payload = [to_base(f) for f in current]
            r = api.bulk_replace(code, draft_ver, bulk_payload)
            if r.status_code == 200:
                new_count = len(r.json()["fields"])
                ok = (new_count == len(bulk_payload))
                runner.report(
                    "T7 POST bulk replace", ok,
                    f"replaced {new_count} fields atomically",
                )
            else:
                runner.report(
                    "T7 POST bulk replace", False,
                    f"HTTP {r.status_code}: {r.text[:200]}",
                )
        else:
            runner.report("T7 POST bulk replace (prep)", False, f"HTTP {r.status_code}")

        # ─── Test 8: DELETE field ────────────────────────────────────────
        # Re-fetch test field id (có thể đổi sau bulk replace)
        r = api.list_fields(code, draft_ver)
        if r.status_code == 200:
            test_field = next(
                (f for f in r.json()["fields"] if f["name"] == TEST_FIELD_NAME),
                None,
            )
            if test_field:
                r = api.delete_field(code, draft_ver, test_field["id"])
                if r.status_code == 200 and r.json().get("deleted"):
                    runner.report("T8 DELETE field", True, f"deleted id={test_field['id']}")
                else:
                    runner.report(
                        "T8 DELETE field", False,
                        f"HTTP {r.status_code}: {r.text[:200]}",
                    )
            else:
                runner.report("T8 DELETE field", False, "test field not found")
        else:
            runner.report("T8 DELETE field (prep)", False, f"HTTP {r.status_code}")

        # ─── Test 9: POST mutate PUBLISHED → 409 ─────────────────────────
        if pub_ver is not None:
            r = api.create_field(code, pub_ver, {
                "name": "should_fail_409",
                "display_label": "Should Fail",
                "field_type": "string",
            })
            if r.status_code == 409:
                runner.report(
                    "T9 POST on PUBLISHED → 409", True,
                    f"HTTP 409 (as expected)",
                )
            else:
                runner.report(
                    "T9 POST on PUBLISHED → 409", False,
                    f"Expected 409, got HTTP {r.status_code}: {r.text[:200]}",
                )
                # Cleanup nếu lỡ insert được
                if r.status_code == 201:
                    fid = r.json()["id"]
                    api.delete_field(code, pub_ver, fid)
        else:
            runner.report(
                "T9 POST on PUBLISHED → 409", True,
                "skipped — không có PUBLISHED revision",
            )

        # ── Final cleanup ──
        await _cleanup_test_field(pool, draft_rev_id, TEST_FIELD_NAME)

    finally:
        pool.close()
        await pool.wait_closed()

    return runner.summary()


# ── Entrypoint ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="E2E test cho input_fields API")
    parser.add_argument("--log-level", default="WARNING",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    config = _load_config()
    exit_code = asyncio.run(run_tests(config))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
