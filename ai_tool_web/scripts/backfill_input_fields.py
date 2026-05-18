"""
scripts/backfill_input_fields.py — Backfill scenario_input_fields từ
scenario_revisions.normalized_spec_json.

Đọc field `inputs[]` từ JSON spec của mỗi revision và INSERT thành rows
trong bảng `scenario_input_fields`.

Idempotent: skip revisions đã có fields (check count > 0).
Dry-run: in preview, không commit.

Usage:
    # Dry-run (preview, không commit)
    python ai_tool_web/scripts/backfill_input_fields.py --dry-run

    # Apply thật
    python ai_tool_web/scripts/backfill_input_fields.py

    # Verbose log từng field
    python ai_tool_web/scripts/backfill_input_fields.py --verbose

    # Backfill 1 revision cụ thể
    python ai_tool_web/scripts/backfill_input_fields.py --revision-id 42

    # Force re-backfill (xóa fields cũ trước khi backfill)
    python ai_tool_web/scripts/backfill_input_fields.py --force

Config từ env (giống mysql_scenario_repo.py):
    MYSQL_HOST     (default: 172.28.8.11)
    MYSQL_PORT     (default: 3306)
    MYSQL_USER     (default: chatbotadmin)
    MYSQL_PASSWORD (REQUIRED — không hardcode trong file này)
    MYSQL_DATABASE (default: changchatbot)

Pre-requisite:
    1. Bảng `scenario_input_fields` đã được tạo qua migration
       `003_scenario_input_fields.sql`.
    2. pip install aiomysql pymysql

Output:
    Cuối script in summary: số revisions processed, fields inserted, skipped.
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

# Setup sys.path để import được từ ai_tool_web khi chạy script trực tiếp
_HERE = Path(__file__).resolve()
_AI_TOOL_WEB = _HERE.parent.parent
sys.path.insert(0, str(_AI_TOOL_WEB))

import aiomysql  # noqa: E402

from store.mysql_scenario_input_field_repo import (  # noqa: E402
    MysqlScenarioInputFieldRepo,
    ScenarioInputField,
)


_log = logging.getLogger("backfill_input_fields")


# ── Config ───────────────────────────────────────────────────────────────────

def _load_config() -> dict[str, Any]:
    pwd = os.getenv("MYSQL_PASSWORD", "")
    if not pwd:
        print(
            "[ERROR] MYSQL_PASSWORD chưa set. Export env hoặc set trực tiếp:\n"
            "    $env:MYSQL_PASSWORD = '...'   (PowerShell)\n"
            "    export MYSQL_PASSWORD=...     (bash)",
            file=sys.stderr,
        )
        sys.exit(2)

    return {
        "host":     os.getenv("MYSQL_HOST", "172.28.8.11"),
        "port":     int(os.getenv("MYSQL_PORT", "3306")),
        "user":     os.getenv("MYSQL_USER", "chatbotadmin"),
        "password": pwd,
        "db":       os.getenv("MYSQL_DATABASE", "changchatbot"),
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_spec_inputs(normalized_spec_json: str) -> list[dict[str, Any]]:
    """Parse normalized_spec_json từ scenario_revisions, trả về list inputs[].

    Return [] nếu spec không có inputs hoặc spec parse fail.
    """
    if not normalized_spec_json:
        return []
    try:
        spec = json.loads(normalized_spec_json)
    except json.JSONDecodeError as e:
        _log.warning("Failed to parse normalized_spec_json: %s", e)
        return []

    inputs = spec.get("inputs") or []
    if not isinstance(inputs, list):
        _log.warning("Field `inputs` không phải list, ignored")
        return []
    return inputs


def _input_to_field(
    inp: dict[str, Any],
    revision_id: int,
    scenario_id: int,
    order: int,
) -> ScenarioInputField:
    """Convert dict input từ spec → ScenarioInputField Pydantic.

    Default values cho các field UI/Phase2/Phase3 chưa có trong spec cũ:
        display_label   = name.replace("_", " ").title()   (heuristic)
        category        = "user_input"
        secret_ref      = None
        extraction_hint = None
        validation_rules = None
        display_order   = order (theo thứ tự xuất hiện trong spec.inputs)
    """
    name = inp.get("name") or ""
    if not name:
        raise ValueError(f"Input thiếu field 'name': {inp}")

    raw_default = inp.get("default")
    # Default từ YAML có thể là string/number/bool — store dạng text
    if raw_default is None:
        default_value = None
    elif isinstance(raw_default, bool):
        default_value = "true" if raw_default else "false"
    else:
        default_value = str(raw_default)

    return ScenarioInputField(
        revision_id=revision_id,
        scenario_id=scenario_id,
        name=name,
        display_label=name.replace("_", " ").title(),  # heuristic — admin sửa sau qua UI
        field_type=inp.get("type") or "string",
        is_required=bool(inp.get("required", False)),
        source=inp.get("source") or "context",
        default_value=default_value,
        description=inp.get("description") or None,
        validation_rules=None,            # Phase 1 chưa có trong spec
        placeholder=None,
        help_text=None,
        display_order=order,
        category="user_input",             # Phase 2 admin set sau qua UI
        secret_ref=None,
        extraction_hint=None,
        template_id=None,
    )


# ── Main backfill logic ──────────────────────────────────────────────────────

async def backfill(
    *,
    dry_run: bool,
    verbose: bool,
    force: bool,
    only_revision_id: Optional[int] = None,
) -> None:
    config = _load_config()
    masked = "***" + config["password"][-2:] if len(config["password"]) > 2 else "***"
    print(
        f"[CONNECT] {config['user']}:{masked}@"
        f"{config['host']}:{config['port']}/{config['db']}"
    )
    if dry_run:
        print("[MODE] DRY-RUN — không commit changes")
    if force:
        print("[MODE] FORCE — xóa fields cũ trước khi backfill")
    if only_revision_id is not None:
        print(f"[SCOPE] Chỉ backfill revision_id={only_revision_id}")

    # Open pool trực tiếp (không qua repo class) để dễ query custom
    pool = await aiomysql.create_pool(
        host=config["host"],
        port=config["port"],
        user=config["user"],
        password=config["password"],
        db=config["db"],
        charset="utf8mb4",
        autocommit=True,
        minsize=1,
        maxsize=3,
        init_command="SET time_zone='+00:00'",
    )

    repo = MysqlScenarioInputFieldRepo(
        host=config["host"],
        port=config["port"],
        user=config["user"],
        password=config["password"],
        db=config["db"],
    )
    await repo.init()

    stats = {
        "revisions_total":   0,
        "revisions_with_inputs": 0,
        "revisions_skipped_idempotent": 0,
        "revisions_skipped_no_inputs":  0,
        "revisions_processed": 0,
        "fields_inserted":   0,
        "fields_deleted_force": 0,
        "errors": 0,
    }

    try:
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                # Load list revisions cần xử lý
                if only_revision_id is not None:
                    await cur.execute(
                        """
                        SELECT id, scenario_id, version_no, normalized_spec_json
                        FROM scenario_revisions
                        WHERE id = %s
                        """,
                        (only_revision_id,),
                    )
                else:
                    await cur.execute(
                        """
                        SELECT id, scenario_id, version_no, normalized_spec_json
                        FROM scenario_revisions
                        ORDER BY id ASC
                        """
                    )
                revisions = await cur.fetchall()

        stats["revisions_total"] = len(revisions)
        print(f"\n[LOAD] {len(revisions)} revisions từ scenario_revisions")

        for rev in revisions:
            rev_id = int(rev["id"])
            scenario_id = int(rev["scenario_id"])
            version_no = int(rev["version_no"])

            # Idempotent check (skip nếu rev đã có fields, trừ khi --force)
            existing_count = await repo.count_by_revision(rev_id)
            if existing_count > 0 and not force:
                stats["revisions_skipped_idempotent"] += 1
                if verbose:
                    print(
                        f"  [SKIP idempotent] rev_id={rev_id} v{version_no} "
                        f"đã có {existing_count} fields"
                    )
                continue

            # Force: xóa cũ trước (chỉ khi không dry-run)
            if existing_count > 0 and force:
                if dry_run:
                    print(
                        f"  [DRY] Sẽ DELETE {existing_count} fields cũ của "
                        f"rev_id={rev_id}"
                    )
                else:
                    deleted = await repo.delete_by_revision(rev_id)
                    stats["fields_deleted_force"] += deleted
                    if verbose:
                        print(f"  [FORCE] Deleted {deleted} fields cũ của rev_id={rev_id}")

            # Parse inputs
            inputs = _parse_spec_inputs(rev["normalized_spec_json"])
            if not inputs:
                stats["revisions_skipped_no_inputs"] += 1
                if verbose:
                    print(f"  [SKIP no-inputs] rev_id={rev_id} v{version_no}")
                continue

            stats["revisions_with_inputs"] += 1

            # Convert + insert
            try:
                for order, inp in enumerate(inputs):
                    field = _input_to_field(inp, rev_id, scenario_id, order)
                    if dry_run:
                        print(
                            f"  [DRY] rev_id={rev_id} INSERT "
                            f"name={field.name!r:20s} "
                            f"type={field.field_type:7s} "
                            f"required={field.is_required} "
                            f"source={field.source}"
                        )
                    else:
                        new_id = await repo.create_field(field)
                        stats["fields_inserted"] += 1
                        if verbose:
                            print(
                                f"  [INSERT] id={new_id} rev_id={rev_id} "
                                f"name={field.name}"
                            )
                stats["revisions_processed"] += 1
                if not verbose and not dry_run:
                    print(
                        f"  ✓ rev_id={rev_id} v{version_no} "
                        f"→ {len(inputs)} fields"
                    )
            except Exception as e:
                stats["errors"] += 1
                _log.error(
                    "Failed backfill rev_id=%d: %s", rev_id, e, exc_info=True
                )
                if verbose:
                    print(f"  [ERROR] rev_id={rev_id}: {e}")

    finally:
        await repo.close()
        pool.close()
        await pool.wait_closed()

    # ── Summary ──────────────────────────────────────────────────────────────

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Total revisions:               {stats['revisions_total']}")
    print(f"  Revisions có inputs trong spec: {stats['revisions_with_inputs']}")
    print(f"  Revisions skipped (idempotent): {stats['revisions_skipped_idempotent']}")
    print(f"  Revisions skipped (no inputs): {stats['revisions_skipped_no_inputs']}")
    print(f"  Revisions processed:           {stats['revisions_processed']}")
    if force:
        print(f"  Fields deleted (--force):      {stats['fields_deleted_force']}")
    print(f"  Fields inserted:               {stats['fields_inserted']}")
    print(f"  Errors:                        {stats['errors']}")
    print("=" * 60)
    if dry_run:
        print("[DRY-RUN] Không có thay đổi nào được commit. Bỏ --dry-run để apply.")
    elif stats["errors"] > 0:
        print(f"⚠ Hoàn thành với {stats['errors']} lỗi — xem log chi tiết bên trên")
        sys.exit(1)
    else:
        print("✓ Backfill hoàn thành thành công")


# ── CLI entrypoint ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Backfill scenario_input_fields từ scenario_revisions.normalized_spec_json"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview changes, không commit",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="In log chi tiết từng field",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Xóa fields cũ trước khi backfill (nguy hiểm — chỉ dùng khi schema spec đổi)",
    )
    parser.add_argument(
        "--revision-id", type=int, default=None,
        help="Chỉ backfill 1 revision cụ thể (cho test)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    asyncio.run(backfill(
        dry_run=args.dry_run,
        verbose=args.verbose,
        force=args.force,
        only_revision_id=args.revision_id,
    ))


if __name__ == "__main__":
    main()
