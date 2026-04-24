"""
Test Case — ScenarioRepository (SQLite backend)

Unit tests cho SqliteScenarioRepo. Không cần Docker/Redis/API — chỉ cần python + aiosqlite.
Mỗi test group dùng 1 DB file riêng trong temp dir.

Chạy:
  cd deploy_server/ai_tool_web
  python tests/test_scenario_repo.py

Exit code:
  0 = all pass
  1 = có test fail
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 stdout cho Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

from store.scenario_repo import (  # noqa: E402
    DefinitionFilters,
    ScenarioDefinition,
    ScenarioRevision,
    ScenarioRun,
)
from store.sqlite_scenario_repo import SqliteScenarioRepo  # noqa: E402


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


async def _make_repo() -> tuple[SqliteScenarioRepo, Path, object]:
    """Tạo fresh repo với temp DB. Return (repo, db_path, tmpdir_ctx)."""
    tmpdir_ctx = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db_path = Path(tmpdir_ctx.name) / "test.db"
    repo = SqliteScenarioRepo(str(db_path))
    await repo.init()
    return repo, db_path, tmpdir_ctx


def _sample_def(
    sid: str = "test_s1",
    owner: str = "hiepqn",
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
    scenario_id: str = "s1",
    yaml_body: str = "goal: test",
    spec_dict: dict | None = None,
    status: str = "passed",
    parent: int | None = None,
    clone_src: int | None = None,
) -> ScenarioRevision:
    return ScenarioRevision(
        scenario_id=scenario_id,
        version_no=0,  # ignored, repo auto
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

async def test_definitions():
    _group("1. DEFINITIONS")
    repo, _, ctx = await _make_repo()
    try:
        # Create + get round-trip
        d1 = _sample_def("s1", "hiepqn", "user")
        await repo.create_definition(d1)
        got = await repo.get_definition("s1")
        _check("create + get round-trip", got is not None and got.id == "s1")
        _check("owner_id preserved", got.owner_id == "hiepqn")
        _check("source_type preserved", got.source_type == "user")
        _check("visibility default private", got.visibility == "private")
        _check("is_archived default False", got.is_archived is False)
        _check("published_revision_id default None", got.published_revision_id is None)

        # Duplicate rejected
        try:
            await repo.create_definition(d1)
            _check("duplicate id rejected", False, "should have raised")
        except ValueError:
            _check("duplicate id rejected", True)

        # Multiple + list filter by owner
        await repo.create_definition(_sample_def("s2", "hiepqn", "user"))
        await repo.create_definition(_sample_def("s3", "other_user", "user"))
        await repo.create_definition(_sample_def("s4", None, "builtin"))

        mine = await repo.list_definitions(DefinitionFilters(owner_id="hiepqn"))
        _check("list by owner_id", len(mine) == 2 and {d.id for d in mine} == {"s1", "s2"})

        builtins = await repo.list_definitions(DefinitionFilters(source_type="builtin"))
        _check("list by source_type=builtin", len(builtins) == 1 and builtins[0].id == "s4")

        # count_builtin + count_by_owner
        _check("count_builtin = 1", await repo.count_builtin() == 1)
        _check("count_by_owner hiepqn = 2", await repo.count_by_owner("hiepqn") == 2)

        # Archive
        await repo.archive_definition("s2")
        active = await repo.list_definitions(DefinitionFilters(owner_id="hiepqn"))
        _check("archive hidden from default list", len(active) == 1 and active[0].id == "s1")

        archived = await repo.list_definitions(
            DefinitionFilters(owner_id="hiepqn", is_archived=True)
        )
        _check("archived visible when is_archived=True", len(archived) == 1 and archived[0].id == "s2")

        # Archive idempotent
        await repo.archive_definition("s2")
        _check("archive idempotent", True)

        # count_by_owner ignores archived
        _check("count_by_owner ignores archived", await repo.count_by_owner("hiepqn") == 1)

        # Pagination (limit)
        limited = await repo.list_definitions(DefinitionFilters(limit=2))
        _check("list limit=2", len(limited) <= 2)
    finally:
        await repo.close()
        ctx.cleanup()


async def test_revisions():
    _group("2. REVISIONS")
    repo, _, ctx = await _make_repo()
    try:
        await repo.create_definition(_sample_def("s1"))

        # Auto version_no 1,2,3
        r1 = await repo.append_revision(_sample_rev(yaml_body="v1"))
        r2 = await repo.append_revision(_sample_rev(yaml_body="v2", parent=r1))
        r3 = await repo.append_revision(_sample_rev(yaml_body="v3", parent=r2))
        revs = await repo.list_revisions("s1")
        versions = sorted(r.version_no for r in revs)
        _check("auto version_no [1,2,3]", versions == [1, 2, 3])

        # get_revision + get_revision_by_version
        got = await repo.get_revision(r2)
        _check("get_revision by id", got is not None and got.version_no == 2)
        got_v1 = await repo.get_revision_by_version("s1", 1)
        _check("get_revision_by_version", got_v1 is not None and got_v1.id == r1)

        # get_latest_revision
        latest = await repo.get_latest_revision("s1")
        _check("get_latest_revision = v3", latest is not None and latest.version_no == 3)

        # Published NULL ban đầu
        pub = await repo.get_published_revision("s1")
        _check("published None when unset", pub is None)

        # Set publish + get
        await repo.set_published_revision("s1", r2)
        pub = await repo.get_published_revision("s1")
        _check("published follows pointer", pub is not None and pub.id == r2)

        # Unpublish (None)
        await repo.set_published_revision("s1", None)
        _check("unpublish (None)", await repo.get_published_revision("s1") is None)

        # parent_revision_id preserved
        rev2 = await repo.get_revision(r2)
        _check("parent_revision_id stored", rev2.parent_revision_id == r1)

        # Save draft with failed validation
        bad_rev = _sample_rev(yaml_body="broken", status="failed")
        bad_rev.static_validation_errors = [{"line": 3, "msg": "invalid action"}]
        r4 = await repo.append_revision(bad_rev)
        got_bad = await repo.get_revision(r4)
        _check("failed revision saved", got_bad.static_validation_status == "failed")
        _check("validation errors preserved",
               got_bad.static_validation_errors == [{"line": 3, "msg": "invalid action"}])

        # Pagination before_id
        page1 = await repo.list_revisions("s1", limit=2)
        _check("list_revisions limit=2", len(page1) == 2)
        _check("list_revisions DESC by id", page1[0].id > page1[1].id)
        page2 = await repo.list_revisions("s1", limit=10, before_id=page1[-1].id)
        _check("list_revisions before_id cursor", all(r.id < page1[-1].id for r in page2))

        # update_revision_test_status
        await repo.update_revision_test_status(r1, "passed", 999, _now())
        rev1 = await repo.get_revision(r1)
        _check("test_status updated", rev1.last_test_run_status == "passed")
        _check("test_run_id updated", rev1.last_test_run_id == 999)
        _check("test_run_at set", rev1.last_test_run_at is not None)

        # update_revision_test_status with bad status
        try:
            await repo.update_revision_test_status(r1, "bogus", 999, _now())
            _check("reject invalid test status", False)
        except ValueError:
            _check("reject invalid test status", True)

        # Clone source: simulate clone
        await repo.create_definition(_sample_def("s_clone", source="cloned"))
        cloned_rev = await repo.append_revision(
            _sample_rev(scenario_id="s_clone", yaml_body="clone", clone_src=r2)
        )
        got = await repo.get_revision(cloned_rev)
        _check("clone_source_revision_id stored", got.clone_source_revision_id == r2)

        # Unicode + long YAML
        long_yaml = "# " + ("Tìm kiếm nghị định " * 500) + "\ngoal: test"
        big_rev = _sample_rev(yaml_body=long_yaml, spec_dict={"vi": "nghị định"})
        big_id = await repo.append_revision(big_rev)
        got_big = await repo.get_revision(big_id)
        _check("long YAML round-trip", got_big.raw_yaml == long_yaml)
        _check("unicode JSON round-trip", got_big.normalized_spec_json == {"vi": "nghị định"})
    finally:
        await repo.close()
        ctx.cleanup()


async def test_concurrent_append():
    _group("3. CONCURRENT APPEND (version_no race)")
    repo, _, ctx = await _make_repo()
    try:
        await repo.create_definition(_sample_def("s1"))

        # 10 append đồng thời → version_no phải là 1..10 unique
        async def append_one(i):
            return await repo.append_revision(_sample_rev(yaml_body=f"v{i}"))

        ids = await asyncio.gather(*[append_one(i) for i in range(10)])
        _check("10 concurrent appends return 10 ids", len(set(ids)) == 10)

        revs = await repo.list_revisions("s1", limit=20)
        versions = sorted(r.version_no for r in revs)
        _check("versions = [1..10] no dup", versions == list(range(1, 11)))
    finally:
        await repo.close()
        ctx.cleanup()


async def test_runs():
    _group("4. RUNS")
    repo, _, ctx = await _make_repo()
    try:
        await repo.create_definition(_sample_def("s1"))
        rev_id = await repo.append_revision(_sample_rev())

        # Create run
        run = ScenarioRun(
            scenario_id="s1",
            revision_id=rev_id,
            session_id="sess-1",
            mode="test",
            started_by="hiepqn",
            runtime_policy_snapshot={"allowed_domains": ["example.com"], "max_steps": 20},
            status="running",
            created_at=_now(),
        )
        run_id = await repo.create_run(run)
        _check("create_run returns id", run_id > 0)

        got = await repo.get_run(run_id)
        _check("get_run round-trip", got is not None and got.session_id == "sess-1")
        _check("policy snapshot JSON preserved",
               got.runtime_policy_snapshot == {"allowed_domains": ["example.com"], "max_steps": 20})
        _check("mode preserved", got.mode == "test")

        # Lifecycle
        await repo.update_run_status(run_id, "completed")
        got = await repo.get_run(run_id)
        _check("update_run_status → completed", got.status == "completed")

        # Invalid status rejected
        try:
            await repo.update_run_status(run_id, "bogus")
            _check("reject invalid run status", False)
        except ValueError:
            _check("reject invalid run status", True)

        # Multiple runs per scenario
        for i in range(3):
            r = ScenarioRun(
                scenario_id="s1", revision_id=rev_id, session_id=f"sess-{i+2}",
                mode="production", started_by="hiepqn",
                runtime_policy_snapshot={}, status="running", created_at=_now(),
            )
            await repo.create_run(r)
        _check("multi runs per scenario", True)  # nếu create ok = ok
    finally:
        await repo.close()
        ctx.cleanup()


async def test_integrity():
    _group("5. INTEGRITY (FK + CHECK + datetime)")
    repo, _, ctx = await _make_repo()
    try:
        # FK: append revision với scenario_id không tồn tại
        try:
            await repo.append_revision(_sample_rev(scenario_id="nonexistent"))
            _check("FK: revision requires existing scenario", False,
                   "accepted orphan revision")
        except Exception:
            _check("FK: revision requires existing scenario", True)

        # CHECK source_type
        try:
            bad = ScenarioDefinition(
                id="bad", name="x",
                source_type="INVALID",  # not in CHECK list
                visibility="private",
                created_at=_now(), updated_at=_now(),
            )
            await repo.create_definition(bad)
            _check("CHECK source_type rejected", False)
        except Exception:
            _check("CHECK source_type rejected", True)

        # CHECK visibility
        try:
            bad = ScenarioDefinition(
                id="bad2", name="x", source_type="user",
                visibility="INVALID",
                created_at=_now(), updated_at=_now(),
            )
            await repo.create_definition(bad)
            _check("CHECK visibility rejected", False)
        except Exception:
            _check("CHECK visibility rejected", True)

        # Datetime round-trip
        await repo.create_definition(_sample_def("s_dt"))
        got = await repo.get_definition("s_dt")
        _check("datetime is datetime type", isinstance(got.created_at, datetime))
        _check("datetime timezone preserved",
               got.created_at.tzinfo is not None)

        # JSON null/empty preserved
        rev = _sample_rev(scenario_id="s_dt", spec_dict={})
        rid = await repo.append_revision(rev)
        got_rev = await repo.get_revision(rid)
        _check("empty JSON dict preserved", got_rev.normalized_spec_json == {})
        _check("validation_errors None preserved", got_rev.static_validation_errors is None)
    finally:
        await repo.close()
        ctx.cleanup()


# ── Entry ────────────────────────────────────────────────────────────────────

async def main():
    groups = [
        test_definitions,
        test_revisions,
        test_concurrent_append,
        test_runs,
        test_integrity,
    ]
    for fn in groups:
        try:
            await fn()
        except Exception:
            print(f"  [ERROR in group {fn.__name__}]")
            traceback.print_exc()
            _FAIL.append((fn.__name__, "uncaught exception"))

    print(f"\n{'='*50}")
    print(f"Total: {len(_PASS)} pass, {len(_FAIL)} fail")
    if _FAIL:
        print("\nFailed:")
        for label, detail in _FAIL:
            print(f"  - {label}: {detail}")
        return 1
    print("\n[ALL PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
