"""Quick smoke test cho SqliteScenarioRepo. Chạy tay:
    cd ai_tool_web && python -m store._smoke_test
"""

import asyncio
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 stdout cho Windows (tránh cp1252 encoding error với tiếng Việt)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

from store.scenario_repo import (
    DefinitionFilters, ScenarioDefinition, ScenarioRevision, ScenarioRun,
)
from store.sqlite_scenario_repo import SqliteScenarioRepo


def _now():
    return datetime.now(timezone.utc)


async def main():
    # ignore_cleanup_errors=True: SQLite WAL files trên Windows bị lock tạm sau close
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        repo = SqliteScenarioRepo(db_path)
        await repo.init()
        print(f"[OK] init -> {db_path}")

        # 1. Create definition
        defn = ScenarioDefinition(
            id="test_user_scenario",
            name="Test user scenario",
            owner_id="hiepqn",
            source_type="user",
            visibility="private",
            created_at=_now(),
            updated_at=_now(),
        )
        await repo.create_definition(defn)
        print(f"[OK] create_definition: {defn.id}")

        # 2. Duplicate -> should raise
        try:
            await repo.create_definition(defn)
            print("[FAIL] duplicate should have raised")
        except ValueError as e:
            print(f"[OK] duplicate rejected: {e}")

        # 3. append_revision × 3 -> version_no auto 1,2,3
        rev_ids = []
        for i in range(3):
            rev = ScenarioRevision(
                scenario_id="test_user_scenario",
                version_no=0,  # ignored
                raw_yaml=f"goal: test v{i+1}",
                normalized_spec_json={"goal": f"test v{i+1}"},
                yaml_hash=f"hash{i+1}",
                parent_revision_id=rev_ids[-1] if rev_ids else None,
                static_validation_status="passed",
                created_by="hiepqn",
                created_at=_now(),
            )
            new_id = await repo.append_revision(rev)
            rev_ids.append(new_id)
        print(f"[OK] 3 revisions appended: ids={rev_ids}")

        # 4. Verify version_no auto-assigned
        rev_list = await repo.list_revisions("test_user_scenario")
        versions = sorted(r.version_no for r in rev_list)
        assert versions == [1, 2, 3], f"expected [1,2,3] got {versions}"
        print(f"[OK] version_no auto: {versions}")

        # 5. get_latest_revision
        latest = await repo.get_latest_revision("test_user_scenario")
        assert latest and latest.version_no == 3
        print(f"[OK] latest: v{latest.version_no} id={latest.id}")

        # 6. set_published_revision + get_published_revision
        await repo.set_published_revision("test_user_scenario", rev_ids[1])  # v2
        pub = await repo.get_published_revision("test_user_scenario")
        assert pub and pub.id == rev_ids[1]
        print(f"[OK] published = v{pub.version_no}")

        # 7. list_definitions filter by owner
        defns = await repo.list_definitions(
            DefinitionFilters(owner_id="hiepqn")
        )
        assert len(defns) == 1
        print(f"[OK] list by owner: {[d.id for d in defns]}")

        # 8. archive_definition + filter
        await repo.archive_definition("test_user_scenario")
        active = await repo.list_definitions(
            DefinitionFilters(owner_id="hiepqn")
        )
        archived = await repo.list_definitions(
            DefinitionFilters(owner_id="hiepqn", is_archived=True)
        )
        assert len(active) == 0 and len(archived) == 1
        print(f"[OK] archive: active={len(active)} archived={len(archived)}")

        # 9. count_builtin (should be 0)
        assert await repo.count_builtin() == 0
        print(f"[OK] count_builtin = 0")

        # 10. create_run + update_run_status
        run = ScenarioRun(
            scenario_id="test_user_scenario",
            revision_id=rev_ids[1],
            session_id="sess-abc",
            mode="test",
            started_by="hiepqn",
            runtime_policy_snapshot={"allowed_domains": ["example.com"]},
            status="running",
            created_at=_now(),
        )
        run_id = await repo.create_run(run)
        await repo.update_run_status(run_id, "completed")
        got = await repo.get_run(run_id)
        assert got.status == "completed"
        print(f"[OK] run lifecycle: id={run_id} status={got.status}")

        # 11. update_revision_test_status
        await repo.update_revision_test_status(rev_ids[1], "passed", run_id, _now())
        rev = await repo.get_revision(rev_ids[1])
        assert rev.last_test_run_status == "passed"
        print(f"[OK] rev test status: {rev.last_test_run_status}")

        await repo.close()
        print("\n[PASS] All smoke tests passed")


if __name__ == "__main__":
    asyncio.run(main())
