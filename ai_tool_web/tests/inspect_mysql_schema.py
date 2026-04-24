"""
Inspect MySQL schema + compare với expected fields từ code hiện tại.

Chạy:
  1. pip install pymysql (nếu chưa có)
  2. Set env hoặc edit config bên dưới
  3. python tests/inspect_mysql_schema.py

Output: paste toàn bộ cho Claude để verify schema.

Không cần paste password — script tự mask.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ── CONFIG: Edit nếu không muốn dùng env ─────────────────────────────────────

CONFIG = {
    "host":     os.getenv("MYSQL_HOST", "172.28.8.11"),
    "port":     int(os.getenv("MYSQL_PORT", "3306")),
    "user":     os.getenv("MYSQL_USER", "chatbotadmin"),
    "password": os.getenv("MYSQL_PASSWORD", "yi2uih235WbSzfOp"),      # ← điền trực tiếp hoặc dùng env
    "database": os.getenv("MYSQL_DATABASE", "changchatbot"),
}


# ── Expected schema (từ store/scenario_repo.py + 001_init_mysql.sql) ─────────

EXPECTED = {
    "scenario_definitions": {
        "columns": {
            "id":                    {"type": "varchar", "length": 64,  "nullable": "NO"},
            "name":                  {"type": "varchar", "length": 255, "nullable": "NO"},
            "owner_id":              {"type": "varchar", "length": 64,  "nullable": "YES"},
            "org_id":                {"type": "varchar", "length": 64,  "nullable": "YES"},
            "source_type":           {"type": "varchar", "length": 16,  "nullable": "NO"},
            "visibility":            {"type": "varchar", "length": 16,  "nullable": "NO"},
            "published_revision_id": {"type": "bigint",  "nullable": "YES"},
            "is_archived":           {"type": "tinyint", "nullable": "NO"},
            "created_at":            {"type": "datetime", "nullable": "NO"},
            "updated_at":            {"type": "datetime", "nullable": "NO"},
        },
        "primary_key": ["id"],
        "indexes": ["idx_def_owner", "idx_def_source"],
    },
    "scenario_revisions": {
        "columns": {
            "id":                          {"type": "bigint",   "nullable": "NO", "extra": "auto_increment"},
            "scenario_id":                 {"type": "varchar",  "length": 64,  "nullable": "NO"},
            "version_no":                  {"type": "int",      "nullable": "NO"},
            "raw_yaml":                    {"type": "longtext", "nullable": "NO"},
            "normalized_spec_json":        {"type": "json",     "nullable": "NO"},
            "yaml_hash":                   {"type": "char",     "length": 64,  "nullable": "NO"},
            "parent_revision_id":          {"type": "bigint",   "nullable": "YES"},
            "clone_source_revision_id":    {"type": "bigint",   "nullable": "YES"},
            "schema_version":              {"type": "int",      "nullable": "NO"},
            "static_validation_status":    {"type": "varchar",  "length": 16, "nullable": "NO"},
            "static_validation_errors":    {"type": "json",     "nullable": "YES"},
            "last_test_run_at":            {"type": "datetime", "nullable": "YES"},
            "last_test_run_status":        {"type": "varchar",  "length": 16, "nullable": "YES"},
            "last_test_run_id":            {"type": "bigint",   "nullable": "YES"},
            "created_by":                  {"type": "varchar",  "length": 64, "nullable": "NO"},
            "created_at":                  {"type": "datetime", "nullable": "NO"},
        },
        "primary_key": ["id"],
        "indexes": ["idx_rev_scenario", "idx_rev_hash", "uq_rev_version"],
        "fks": ["fk_rev_scenario", "fk_rev_parent", "fk_rev_clone_source"],
    },
    "scenario_runs": {
        "columns": {
            "id":                      {"type": "bigint",   "nullable": "NO", "extra": "auto_increment"},
            "scenario_id":             {"type": "varchar",  "length": 64, "nullable": "NO"},
            "revision_id":             {"type": "bigint",   "nullable": "NO"},
            "session_id":              {"type": "varchar",  "length": 64, "nullable": "NO"},
            "mode":                    {"type": "varchar",  "length": 16, "nullable": "NO"},
            "started_by":              {"type": "varchar",  "length": 64, "nullable": "NO"},
            "runtime_policy_snapshot": {"type": "json",     "nullable": "NO"},
            "status":                  {"type": "varchar",  "length": 16, "nullable": "NO"},
            "created_at":              {"type": "datetime", "nullable": "NO"},
        },
        "primary_key": ["id"],
        "indexes": ["idx_run_scenario", "idx_run_session", "idx_run_status"],
        "fks": ["fk_run_scenario", "fk_run_revision"],
    },
}


# ── Inspection ───────────────────────────────────────────────────────────────

def connect():
    try:
        import pymysql
    except ImportError:
        print("[ERROR] pymysql not installed. Run: pip install pymysql")
        sys.exit(1)

    if not CONFIG["password"]:
        pwd = os.getenv("MYSQL_PASSWORD", "")
        if not pwd:
            print("[ERROR] Password chưa set. Edit CONFIG hoặc set env MYSQL_PASSWORD.")
            sys.exit(1)
        CONFIG["password"] = pwd

    masked = "***" + CONFIG["password"][-2:] if len(CONFIG["password"]) > 2 else "***"
    print(f"[CONNECT] {CONFIG['user']}:{masked}@{CONFIG['host']}:{CONFIG['port']}/{CONFIG['database']}")

    return pymysql.connect(
        host=CONFIG["host"],
        port=CONFIG["port"],
        user=CONFIG["user"],
        password=CONFIG["password"],
        database=CONFIG["database"],
        charset="utf8mb4",
    )


def fetch_all(cur, sql, params=()):
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def inspect_table(cur, db, table):
    cols = fetch_all(cur, """
        SELECT column_name, data_type, character_maximum_length,
               is_nullable, column_default, extra, column_type, column_key
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
    """, (db, table))

    indexes = fetch_all(cur, """
        SELECT index_name, column_name, non_unique, seq_in_index
        FROM information_schema.statistics
        WHERE table_schema = %s AND table_name = %s
        ORDER BY index_name, seq_in_index
    """, (db, table))

    fks = fetch_all(cur, """
        SELECT constraint_name, column_name,
               referenced_table_name, referenced_column_name
        FROM information_schema.key_column_usage
        WHERE table_schema = %s AND table_name = %s
          AND referenced_table_name IS NOT NULL
        ORDER BY constraint_name, ordinal_position
    """, (db, table))

    checks = fetch_all(cur, """
        SELECT constraint_name, check_clause
        FROM information_schema.check_constraints
        WHERE constraint_schema = %s AND table_name = %s
    """, (db, table))

    return cols, indexes, fks, checks


def print_table(name, cols, indexes, fks, checks, expected):
    print(f"\n{'='*70}")
    print(f"TABLE: {name}")
    print('='*70)

    if not cols:
        print("[MISSING] Table không tồn tại!")
        return False

    # Columns
    print("\n-- Columns --")
    print(f"  {'name':28s} {'type':20s} {'nullable':10s} {'extra'}")
    actual_cols = {}
    for c in cols:
        actual_cols[c["column_name"]] = c
        extras = []
        if c.get("extra"):
            extras.append(c["extra"])
        if c.get("column_key"):
            extras.append(c["column_key"])
        print(f"  {c['column_name']:28s} {c['column_type']:20s} "
              f"{c['is_nullable']:10s} {' '.join(extras)}")

    # Compare columns
    print("\n-- Column diff vs expected --")
    exp_cols = expected.get("columns", {})
    all_ok = True
    for name_exp, spec in exp_cols.items():
        if name_exp not in actual_cols:
            print(f"  [MISSING] {name_exp}")
            all_ok = False
            continue
        c = actual_cols[name_exp]
        issues = []
        if spec["type"].lower() not in c["data_type"].lower():
            issues.append(f"type expected {spec['type']}, got {c['data_type']}")
        if spec["nullable"] != c["is_nullable"]:
            issues.append(f"nullable expected {spec['nullable']}, got {c['is_nullable']}")
        if "length" in spec and c.get("character_maximum_length"):
            if int(c["character_maximum_length"]) != spec["length"]:
                issues.append(f"length expected {spec['length']}, got {c['character_maximum_length']}")
        if spec.get("extra") and spec["extra"] not in (c.get("extra") or "").lower():
            issues.append(f"extra expected '{spec['extra']}', got '{c.get('extra')}'")
        if issues:
            print(f"  [MISMATCH] {name_exp}: {'; '.join(issues)}")
            all_ok = False

    extras_cols = set(actual_cols) - set(exp_cols)
    if extras_cols:
        print(f"  [UNEXPECTED EXTRA] {sorted(extras_cols)}")

    if all_ok and not extras_cols:
        print("  ✓ All columns match")

    # Indexes
    print("\n-- Indexes --")
    idx_groups = {}
    for r in indexes:
        idx_groups.setdefault(r["index_name"], []).append(r["column_name"])
    for idx_name, col_list in idx_groups.items():
        unique = "UNIQUE" if any(r["index_name"] == idx_name and r["non_unique"] == 0 for r in indexes) else ""
        print(f"  {idx_name:30s} ({', '.join(col_list)}) {unique}")

    # FKs
    print("\n-- Foreign Keys --")
    if not fks:
        print("  (none)")
    for fk in fks:
        print(f"  {fk['constraint_name']:30s} {fk['column_name']} -> "
              f"{fk['referenced_table_name']}.{fk['referenced_column_name']}")

    # CHECK constraints
    print("\n-- CHECK Constraints --")
    if not checks:
        print("  (none detected — MySQL 5.7 không enforce CHECK, MySQL 8+ enforce)")
    for chk in checks:
        print(f"  {chk['constraint_name']:30s} {chk['check_clause']}")

    return all_ok


def main():
    try:
        conn = connect()
    except Exception as e:
        print(f"[ERROR] Connect failed: {e}")
        sys.exit(1)

    db = CONFIG["database"]
    try:
        with conn.cursor() as cur:
            # Server version
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
            print(f"\n[SERVER] MySQL version: {version}")

            # Charset
            cur.execute("""
                SELECT default_character_set_name, default_collation_name
                FROM information_schema.schemata WHERE schema_name = %s
            """, (db,))
            charset_row = cur.fetchone()
            if charset_row:
                print(f"[SCHEMA] {db}: charset={charset_row[0]}, collation={charset_row[1]}")

            # Tables
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = %s ORDER BY table_name
            """, (db,))
            tables = [r[0] for r in cur.fetchall()]
            print(f"\n[TABLES in {db}] {tables}")

            all_results = {}
            for t in ["scenario_definitions", "scenario_revisions", "scenario_runs"]:
                cols, indexes, fks, checks = inspect_table(cur, db, t)
                ok = print_table(t, cols, indexes, fks, checks, EXPECTED.get(t, {}))
                all_results[t] = ok

            print(f"\n{'='*70}")
            print("SUMMARY")
            print('='*70)
            for t, ok in all_results.items():
                status = "✓ PASS" if ok else "✗ MISMATCH"
                print(f"  {status}  {t}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
