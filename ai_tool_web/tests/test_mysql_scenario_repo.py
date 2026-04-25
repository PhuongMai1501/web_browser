"""
Test Case — ScenarioRepository (MySQL/MariaDB backend, real connection)

Integration test cho MysqlScenarioRepo. KẾT NỐI THẬT tới MariaDB server.
Mọi row test có code prefix 'test_mysql_repo_' → cleanup dễ.

Config: tự động load từ `dev/deploy_server/.env` (qua python-dotenv).
Các biến cần có trong .env:
  MYSQL_HOST     (default: 172.28.8.11)
  MYSQL_PORT     (default: 3306)
  MYSQL_USER     (default: chatbotadmin)
  MYSQL_PASSWORD (REQUIRED)
  MYSQL_DB       (default: changchatbot)

Chạy (không cần set env):
  cd deploy_server/ai_tool_web
  python tests/test_mysql_scenario_repo.py

Override env nếu muốn test DB khác:
  $env:MYSQL_HOST = "..."   # ưu tiên hơn .env

Exit code:
  0 = all pass
  1 = có test fail
  2 = không kết nối được DB

Cleanup: chạy lại test luôn xóa rows test_mysql_repo_* trước (idempotent).
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 stdout cho Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

# Auto-load .env từ dev/deploy_server/.env (parent của ai_tool_web)
try:
    from dotenv import load_dotenv
    _env_file = Path(__file__).resolve().parents[2] / ".env"
    if _env_file.exists():
        load_dotenv(_env_file)
except ImportError:
    pass  # python-dotenv không bắt buộc nếu env đã set sẵn

import aiomysql  # noqa: E402

from store.mysql_scenario_repo import MysqlScenarioRepo  # noqa: E402
from store.scenario_repo import (  # noqa: E402
    DefinitionFilters,
    ScenarioDefinition,
    ScenarioRevision,
    ScenarioRun,
)


# ── Config từ env ────────────────────────────────────────────────────────────

_MYSQL_HOST = os.getenv("MYSQL_HOST", "172.28.8.11")
_MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
_MYSQL_USER = os.getenv("MYSQL_USER", "chatbotadmin")
_MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD")
_MYSQL_DB = os.getenv("MYSQL_DB", "changchatbot")

_TEST_PREFIX = "test_mysql_repo_"


# ── Tracking ─────────────────────────────────────────────────────────────────

_PASS: list[str] = []
_FAIL: list[tuple[str, str]] = []


def _check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASS.append(label)
        print(f"  [PASS] {label}")
    else:
        _FAIL.append((label, detail))
        print(f"  [FAIL] {label}{(' -> ' + detail) if detail else ''}")


def _group(title: str) -> None:
    print(f"\n=== {title} ===")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Fixtures ─────────────────────────────────────────────────────────────────

async def _cleanup_test_rows() -> None:
    """Xoá mọi row có code prefix 'test_mysql_repo_'.
    Chạy trước test (idempotent) và sau test (cleanup)."""
    conn = await aiomysql.connect(
        host=_MYSQL_HOST, port=_MYSQL_PORT,
        user=_MYSQL_USER, password=_MYSQL_PASSWORD,
        db=_MYSQL_DB, charset="utf8mb4", autocommit=True,
    )
    try:
        async with conn.cursor() as cur:
            # Lấy id BIGINT của test scenarios để delete dependent rows
            await cur.execute(
                "SELECT id FROM scenario_definitions WHERE code LIKE %s",
                (f"{_TEST_PREFIX}%",),
            )
            ids = [r[0] for r in await cur.fetchall()]
            if ids:
                placeholders = ",".join(["%s"] * len(ids))
                await cur.execute(
                    f"DELETE FROM scenario_runs WHERE scenario_id IN ({placeholders})",
                    ids,
                )
                await cur.execute(
                    f"DELETE FROM scenario_revisions WHERE scenario_id IN ({placeholders})",
                    ids,
                )
            await cur.execute(
                "DELETE FROM scenario_definitions WHERE code LIKE %s",
                (f"{_TEST_PREFIX}%",),
            )
    finally:
        conn.close()


async def _make_repo() -> MysqlScenarioRepo:
    repo = MysqlScenarioRepo(
        host=_MYSQL_HOST, port=_MYSQL_PORT,
        user=_MYSQL_USER, password=_MYSQL_PASSWORD,
        db=_MYSQL_DB, pool_size=3,
    )
    await repo.init()
    return repo


def _sample_def(
    sid: str = f"{_TEST_PREFIX}s1",
    owner: str | None = "hiepqn",
    source: str = "user",
) -> ScenarioDefinition:
    return ScenarioDefinition(
        id=sid,
        name=f"Name of {sid}",
        owner_id=owner,
        source_type=source,
        visibility="private",
        created_at=_now(),
        updated_at=_now(),
    )


def _sample_rev(
    scenario_id: str,
    yaml_body: str = "goal: test",
    spec_dict: dict | None = None,
    status: str = "passed",
    parent: int | None = None,
    clone_src: int | None = None,
) -> ScenarioRevision:
    return ScenarioRevision(
        scenario_id=scenario_id,
        version_no=0,  # ignored
        raw_yaml=yaml_body,
        normalized_spec_json=spec_dict if spec_dict is not None else {"goal": "test"},
        yaml_hash="hash_" + str(abs(hash(yaml_body)) % 10**10),
        parent_revision_id=parent,
        clone_source_revision_id=clone_src,
        static_validation_status=status,
        created_by="hiepqn",
        created_at=_now(),
    )


# ── Test groups ──────────────────────────────────────────────────────────────

async def test_definitions(repo: MysqlScenarioRepo) -> None:
    _group("1. DEFINITIONS")

    d1 = _sample_def(f"{_TEST_PREFIX}s1", "hiepqn", "user")
    await repo.create_definition(d1)
    got = await repo.get_definition(f"{_TEST_PREFIX}s1")
    _check("create + get round-trip",
           got is not None and got.id == f"{_TEST_PREFIX}s1")
    _check("owner_id (owner_code) preserved", got.owner_id == "hiepqn")
    _check("source_type preserved", got.source_type == "user")
    _check("visibility default private", got.visibility == "private")
    _check("is_archived default False", got.is_archived is False)
    _check("published_revision_id default None",
           got.published_revision_id is None)

    # Duplicate code rejected (UNIQUE)
    try:
        await repo.create_definition(d1)
        _check("duplicate id rejected", False, "should have raised")
    except ValueError:
        _check("duplicate id rejected", True)

    # Multiple + list filter
    await repo.create_definition(_sample_def(f"{_TEST_PREFIX}s2", "hiepqn", "user"))
    await repo.create_definition(_sample_def(f"{_TEST_PREFIX}s3", "other_user", "user"))
    await repo.create_definition(_sample_def(f"{_TEST_PREFIX}s4", None, "builtin"))

    mine = await repo.list_definitions(DefinitionFilters(owner_id="hiepqn"))
    mine_ids = {d.id for d in mine}
    _check("list by owner_id (hiepqn = 2)",
           {f"{_TEST_PREFIX}s1", f"{_TEST_PREFIX}s2"}.issubset(mine_ids))

    # count_by_owner only counts test+other rows owned by hiepqn
    cnt = await repo.count_by_owner("hiepqn")
    _check("count_by_owner hiepqn >= 2", cnt >= 2)

    # Archive
    await repo.archive_definition(f"{_TEST_PREFIX}s2")
    active = await repo.list_definitions(DefinitionFilters(owner_id="hiepqn"))
    _check("archive hidden from default list",
           f"{_TEST_PREFIX}s2" not in {d.id for d in active})

    archived = await repo.list_definitions(
        DefinitionFilters(owner_id="hiepqn", is_archived=True)
    )
    _check("archived visible when is_archived=True",
           f"{_TEST_PREFIX}s2" in {d.id for d in archived})

    # Idempotent
    await repo.archive_definition(f"{_TEST_PREFIX}s2")
    _check("archive idempotent", True)


async def test_revisions(repo: MysqlScenarioRepo) -> None:
    _group("2. REVISIONS")

    sid = f"{_TEST_PREFIX}rev_s"
    await repo.create_definition(_sample_def(sid))

    # First revision
    rev1 = _sample_rev(sid, "goal: a")
    rid1 = await repo.append_revision(rev1)
    _check("append_revision returns int id", isinstance(rid1, int) and rid1 > 0)

    got = await repo.get_revision(rid1)
    _check("get_revision round-trip",
           got is not None and got.id == rid1 and got.scenario_id == sid)
    _check("version_no auto = 1", got.version_no == 1)
    _check("normalized_spec_json roundtrip",
           got.normalized_spec_json == {"goal": "test"})
    _check("yaml_hash preserved", got.yaml_hash == rev1.yaml_hash)

    # Second revision auto-increment version
    rev2 = _sample_rev(sid, "goal: b", parent=rid1)
    rid2 = await repo.append_revision(rev2)
    got2 = await repo.get_revision(rid2)
    _check("version_no auto-increment to 2", got2.version_no == 2)
    _check("parent_revision_id preserved", got2.parent_revision_id == rid1)

    # by_version
    by_v = await repo.get_revision_by_version(sid, 1)
    _check("get_revision_by_version", by_v is not None and by_v.id == rid1)

    # Latest
    latest = await repo.get_latest_revision(sid)
    _check("get_latest_revision = v2", latest is not None and latest.id == rid2)

    # List newest-first
    revs = await repo.list_revisions(sid, limit=10)
    _check("list_revisions newest-first",
           [r.id for r in revs] == [rid2, rid1])

    # Pagination
    page = await repo.list_revisions(sid, limit=10, before_id=rid2)
    _check("list_revisions before_id", [r.id for r in page] == [rid1])

    # Publish
    await repo.set_published_revision(sid, rid2)
    pub = await repo.get_published_revision(sid)
    _check("get_published_revision after publish",
           pub is not None and pub.id == rid2)

    # Unpublish
    await repo.set_published_revision(sid, None)
    pub2 = await repo.get_published_revision(sid)
    _check("get_published_revision = None after unpublish", pub2 is None)

    # Update test status
    await repo.update_revision_test_status(rid2, "passed", run_id=999, at=_now())
    got3 = await repo.get_revision(rid2)
    _check("update_revision_test_status saved",
           got3.last_test_run_status == "passed"
           and got3.last_test_run_id == 999
           and got3.last_test_run_at is not None)

    # Invalid status raises
    try:
        await repo.update_revision_test_status(rid2, "weird", run_id=1, at=_now())
        _check("invalid test_status rejected", False, "should have raised")
    except ValueError:
        _check("invalid test_status rejected", True)


async def test_runs(repo: MysqlScenarioRepo) -> None:
    _group("3. RUNS")

    sid = f"{_TEST_PREFIX}run_s"
    await repo.create_definition(_sample_def(sid))
    rid = await repo.append_revision(_sample_rev(sid))

    run = ScenarioRun(
        scenario_id=sid,
        revision_id=rid,
        session_id="9ee0143d-f924-4ef9-a894-3817188242c0",
        mode="production",
        started_by="hiepqn",
        runtime_policy_snapshot={"allowed_domains": ["example.com"], "quota": 5},
        status="running",
        created_at=_now(),
    )
    run_id = await repo.create_run(run)
    _check("create_run returns int id",
           isinstance(run_id, int) and run_id > 0)

    got = await repo.get_run(run_id)
    _check("get_run round-trip",
           got is not None and got.id == run_id and got.scenario_id == sid)
    _check("session_id preserved",
           got.session_id == "9ee0143d-f924-4ef9-a894-3817188242c0")
    _check("mode preserved", got.mode == "production")
    _check("runtime_policy_snapshot roundtrip",
           got.runtime_policy_snapshot == {"allowed_domains": ["example.com"], "quota": 5})
    _check("status running", got.status == "running")

    # Update status terminal
    await repo.update_run_status(run_id, "completed")
    got2 = await repo.get_run(run_id)
    _check("update_run_status to completed", got2.status == "completed")

    # Invalid status
    try:
        await repo.update_run_status(run_id, "weird")
        _check("invalid run_status rejected", False, "should have raised")
    except ValueError:
        _check("invalid run_status rejected", True)


async def test_clone_source(repo: MysqlScenarioRepo) -> None:
    _group("4. CLONE SOURCE TRACKING")

    src_sid = f"{_TEST_PREFIX}clone_src"
    await repo.create_definition(_sample_def(src_sid, owner=None, source="builtin"))
    src_rid = await repo.append_revision(_sample_rev(src_sid, "goal: source"))

    # Clone scenario reference cùng src_rid
    dst_sid = f"{_TEST_PREFIX}clone_dst"
    await repo.create_definition(_sample_def(dst_sid, owner="hiepqn", source="cloned"))
    dst_rid = await repo.append_revision(
        _sample_rev(dst_sid, "goal: dst", clone_src=src_rid)
    )

    got = await repo.get_revision(dst_rid)
    _check("clone_source_revision_id preserved",
           got.clone_source_revision_id == src_rid)


async def test_resolve_pk_error(repo: MysqlScenarioRepo) -> None:
    _group("5. ERROR HANDLING")

    # append_revision với scenario không tồn tại
    rev = _sample_rev(f"{_TEST_PREFIX}does_not_exist")
    try:
        await repo.append_revision(rev)
        _check("append_revision unknown scenario rejected",
               False, "should have raised ValueError")
    except ValueError as e:
        _check("append_revision unknown scenario rejected",
               "không tồn tại" in str(e))


# ── Runner ───────────────────────────────────────────────────────────────────

async def main() -> int:
    if not _MYSQL_PASSWORD:
        print("[ERROR] MYSQL_PASSWORD env var chưa set.")
        print("Chạy lại với: $env:MYSQL_PASSWORD = '...'")
        return 2

    print(f"Target: {_MYSQL_USER}@{_MYSQL_HOST}:{_MYSQL_PORT}/{_MYSQL_DB}")
    print(f"Test row prefix: '{_TEST_PREFIX}' (sẽ cleanup trước & sau)")

    # Pre-cleanup
    try:
        await _cleanup_test_rows()
        print("Pre-cleanup OK")
    except Exception as e:
        print(f"[ERROR] Không kết nối được MySQL: {e}")
        traceback.print_exc()
        return 2

    repo = await _make_repo()
    try:
        await test_definitions(repo)
        await test_revisions(repo)
        await test_runs(repo)
        await test_clone_source(repo)
        await test_resolve_pk_error(repo)
    except Exception:
        traceback.print_exc()
        _FAIL.append(("uncaught", "exception in test"))
    finally:
        await repo.close()
        # Post-cleanup
        try:
            await _cleanup_test_rows()
            print("\nPost-cleanup OK")
        except Exception as e:
            print(f"[WARN] Post-cleanup fail: {e}")

    print("\n" + "=" * 60)
    print(f"PASS: {len(_PASS)}    FAIL: {len(_FAIL)}")
    if _FAIL:
        print("\nFailed:")
        for label, detail in _FAIL:
            print(f"  - {label}: {detail}")
        return 1
    print("\nAll tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
