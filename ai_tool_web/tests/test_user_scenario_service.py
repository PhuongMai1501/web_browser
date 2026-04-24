"""
Test — UserScenarioService (orchestrator + permissions)
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import traceback
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

from auth.providers import AuthenticatedUser  # noqa: E402
from services.user_scenario_service import (  # noqa: E402
    MAX_SCENARIOS_PER_USER,
    QuotaExceeded,
    ScenarioBadRequest,
    ScenarioConflict,
    ScenarioForbidden,
    ScenarioNotFound,
    UserScenarioService,
)
from store.sqlite_scenario_repo import SqliteScenarioRepo  # noqa: E402


_PASS: list[str] = []
_FAIL: list[tuple[str, str]] = []


def _check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASS.append(label)
        print(f"  [PASS] {label}")
    else:
        _FAIL.append((label, detail))
        print(f"  [FAIL] {label}{(' -> ' + detail) if detail else ''}")


USER = AuthenticatedUser(user_id="hiepqn")
STRANGER = AuthenticatedUser(user_id="stranger")
ADMIN = AuthenticatedUser(user_id="admin", is_admin=True)


_GOOD_YAML = """
id: ignored_will_be_overridden
display_name: Test Scenario
description: Test description
start_url: https://example.com
allowed_domains: [example.com]
inputs:
  - name: keyword
    type: string
    required: true
    source: context
goal: Tìm {keyword}
"""


_BAD_PARSE_YAML = "!!! not yaml [[[ broken"


_BAD_VALIDATE_YAML = """
# YAML parse OK nhưng thiếu required field 'display_name'
id: some_id
"""


async def _make_service():
    tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    repo = SqliteScenarioRepo(str(Path(tmpdir.name) / "test.db"))
    await repo.init()
    return UserScenarioService(repo), repo, tmpdir


# ── Tests ────────────────────────────────────────────────────────────────────

async def test_validate_only():
    print("=== 1. VALIDATE (dry-run) ===")
    svc, repo, ctx = await _make_service()
    try:
        r = await svc.validate(_GOOD_YAML)
        _check("good YAML valid", r.validation_ok)

        r = await svc.validate(_BAD_PARSE_YAML)
        _check("bad YAML parse_ok=False", not r.parse_ok)

        r = await svc.validate(_BAD_VALIDATE_YAML)
        _check("semantic-bad YAML validation_ok=False",
               r.parse_ok and not r.validation_ok)

        # Không lưu DB
        defs = await repo.list_definitions(
            __import__("store.scenario_repo", fromlist=["DefinitionFilters"]).DefinitionFilters()
        )
        _check("validate không lưu DB", len(defs) == 0)
    finally:
        await repo.close()
        ctx.cleanup()


async def test_create():
    print("\n=== 2. CREATE ===")
    svc, repo, ctx = await _make_service()
    try:
        defn = await svc.create(_GOOD_YAML, USER)
        _check("create returns definition", defn is not None)
        _check("id auto-prefixed",
               defn.id.startswith("user_hiepqn_"))
        _check("owner_id set", defn.owner_id == "hiepqn")
        _check("source_type=user", defn.source_type == "user")

        # Revision 1 tạo
        latest = await repo.get_latest_revision(defn.id)
        _check("revision 1 created", latest is not None and latest.version_no == 1)
        _check("revision status passed",
               latest.static_validation_status == "passed")
        _check("parent_revision_id None cho rev 1",
               latest.parent_revision_id is None)
    finally:
        await repo.close()
        ctx.cleanup()


async def test_create_duplicate_name():
    print("\n=== 3. CREATE DUPLICATE NAME ===")
    svc, repo, ctx = await _make_service()
    try:
        await svc.create(_GOOD_YAML, USER)
        try:
            await svc.create(_GOOD_YAML, USER)
            _check("duplicate create raises", False)
        except ScenarioConflict as e:
            _check("duplicate create raises", True)
            _check("error code ID_CONFLICT", e.code == "ID_CONFLICT")
    finally:
        await repo.close()
        ctx.cleanup()


async def test_create_bad_yaml_rejected():
    print("\n=== 4. CREATE BAD YAML HARD REJECT ===")
    svc, repo, ctx = await _make_service()
    try:
        try:
            await svc.create(_BAD_PARSE_YAML, USER)
            _check("parse-fail hard reject", False)
        except ScenarioBadRequest:
            _check("parse-fail hard reject", True)

        # Semantic-fail → vẫn tạo (revision với status=failed)
        bad_hook_yaml = """
id: x
display_name: Bad Hook
hooks:
  pre_check: nonexistent_hook_xyz
"""
        defn = await svc.create(bad_hook_yaml, USER)
        latest = await repo.get_latest_revision(defn.id)
        _check("semantic-fail vẫn tạo definition", defn is not None)
        _check("revision status=failed",
               latest.static_validation_status == "failed")
    finally:
        await repo.close()
        ctx.cleanup()


async def test_update_creates_new_revision():
    print("\n=== 5. UPDATE = NEW REVISION ===")
    svc, repo, ctx = await _make_service()
    try:
        defn = await svc.create(_GOOD_YAML, USER)
        rev2 = await svc.update(defn.id, _GOOD_YAML + "\n# edit\n", USER)
        _check("update returns new revision", rev2.version_no == 2)
        _check("parent_revision_id set",
               rev2.parent_revision_id is not None)

        all_revs = await repo.list_revisions(defn.id)
        _check("2 revisions tồn tại", len(all_revs) == 2)
    finally:
        await repo.close()
        ctx.cleanup()


async def test_update_noop_rejected():
    print("\n=== 6. UPDATE NO-OP REJECTED ===")
    svc, repo, ctx = await _make_service()
    try:
        defn = await svc.create(_GOOD_YAML, USER)
        try:
            await svc.update(defn.id, _GOOD_YAML, USER)
            _check("no-op save rejected", False)
        except ScenarioConflict as e:
            _check("no-op save rejected", True)
            _check("error code NO_CHANGE", e.code == "NO_CHANGE")
    finally:
        await repo.close()
        ctx.cleanup()


async def test_update_permission():
    print("\n=== 7. UPDATE PERMISSION ===")
    svc, repo, ctx = await _make_service()
    try:
        defn = await svc.create(_GOOD_YAML, USER)
        try:
            await svc.update(defn.id, _GOOD_YAML + "# x\n", STRANGER)
            _check("non-owner update rejected", False)
        except ScenarioForbidden:
            _check("non-owner update rejected", True)

        # Admin can update anyone
        rev2 = await svc.update(defn.id, _GOOD_YAML + "# admin edit\n", ADMIN)
        _check("admin can update", rev2.version_no == 2)
    finally:
        await repo.close()
        ctx.cleanup()


async def test_archive():
    print("\n=== 8. ARCHIVE ===")
    svc, repo, ctx = await _make_service()
    try:
        defn = await svc.create(_GOOD_YAML, USER)
        await svc.archive(defn.id, USER)
        got = await repo.get_definition(defn.id)
        _check("archived flag set", got.is_archived is True)

        # Non-owner không archive được
        defn2 = await svc.create(_GOOD_YAML.replace("Test Scenario", "Test Two"), USER)
        try:
            await svc.archive(defn2.id, STRANGER)
            _check("non-owner archive rejected", False)
        except ScenarioForbidden:
            _check("non-owner archive rejected", True)
    finally:
        await repo.close()
        ctx.cleanup()


async def test_list_for_user_permission():
    print("\n=== 9. LIST — PERMISSION FILTER ===")
    svc, repo, ctx = await _make_service()
    try:
        # User tạo scenario
        await svc.create(_GOOD_YAML, USER)
        # Stranger tạo scenario
        await svc.create(_GOOD_YAML, STRANGER)

        # USER list → chỉ thấy own
        mine = await svc.list_for_user(USER)
        _check("USER list chỉ thấy của mình",
               all(d.owner_id in ("hiepqn", None) for d in mine))
        _check("USER không thấy stranger's",
               not any(d.owner_id == "stranger" for d in mine))

        # STRANGER list → chỉ thấy own
        theirs = await svc.list_for_user(STRANGER)
        _check("STRANGER list chỉ thấy của mình",
               all(d.owner_id in ("stranger", None) for d in theirs))

        # ADMIN list → thấy tất
        all_ = await svc.list_for_user(ADMIN)
        owners = {d.owner_id for d in all_}
        _check("ADMIN thấy cả 2 user scenarios",
               "hiepqn" in owners and "stranger" in owners)
    finally:
        await repo.close()
        ctx.cleanup()


async def test_get_detail_permission():
    print("\n=== 10. GET DETAIL — permission ===")
    svc, repo, ctx = await _make_service()
    try:
        defn = await svc.create(_GOOD_YAML, USER)

        # Owner đọc được
        detail = await svc.get_detail(defn.id, USER)
        _check("owner read OK",
               detail.definition.id == defn.id and detail.latest_revision is not None)

        # Stranger → 403
        try:
            await svc.get_detail(defn.id, STRANGER)
            _check("non-owner read rejected", False)
        except ScenarioForbidden:
            _check("non-owner read rejected", True)

        # Not found
        try:
            await svc.get_detail("xxx_not_exist", USER)
            _check("missing scenario raises", False)
        except ScenarioNotFound:
            _check("missing scenario raises", True)
    finally:
        await repo.close()
        ctx.cleanup()


async def test_clone():
    print("\n=== 11. CLONE ===")
    svc, repo, ctx = await _make_service()
    try:
        # User A tạo scenario
        defn_a = await svc.create(_GOOD_YAML, USER)
        # Publish rev 1 để clone dùng published
        latest = await repo.get_latest_revision(defn_a.id)
        await repo.set_published_revision(defn_a.id, latest.id)

        # Cùng user clone → chắc được
        cloned = await svc.clone(defn_a.id, USER, new_display_name="My Copy")
        _check("clone returns new def",
               cloned is not None and cloned.id != defn_a.id)
        _check("clone source_type=cloned", cloned.source_type == "cloned")
        _check("clone owner = user", cloned.owner_id == "hiepqn")

        # Check clone_source_revision_id set
        clone_rev = await repo.get_latest_revision(cloned.id)
        _check("clone_source_revision_id trỏ về rev gốc",
               clone_rev.clone_source_revision_id == latest.id)
        _check("parent_revision_id None (rev 1 của clone)",
               clone_rev.parent_revision_id is None)

        # Edit clone → rev 2, parent chain trong clone
        rev2 = await svc.update(cloned.id, clone_rev.raw_yaml + "# my edit\n", USER)
        _check("rev 2 của clone có parent",
               rev2.parent_revision_id == clone_rev.id)
        # clone_source_revision_id KHÔNG set ở rev 2 (chỉ set ở rev 1)
        _check("rev 2 clone_source_revision_id None",
               rev2.clone_source_revision_id is None)
    finally:
        await repo.close()
        ctx.cleanup()


async def test_clone_private_forbidden():
    print("\n=== 12. CLONE PRIVATE của user khác → forbidden ===")
    svc, repo, ctx = await _make_service()
    try:
        defn = await svc.create(_GOOD_YAML, USER)
        try:
            await svc.clone(defn.id, STRANGER)
            _check("non-owner clone private rejected", False)
        except ScenarioForbidden:
            _check("non-owner clone private rejected", True)

        # Admin clone được
        cloned = await svc.clone(defn.id, ADMIN)
        _check("admin clone OK", cloned is not None)
    finally:
        await repo.close()
        ctx.cleanup()


async def test_quota():
    print("\n=== 13. QUOTA ===")
    svc, repo, ctx = await _make_service()
    try:
        # Giả lập đã có MAX scenarios (dùng repo trực tiếp, faster)
        from datetime import datetime, timezone
        from store.scenario_repo import ScenarioDefinition
        now = datetime.now(timezone.utc)
        for i in range(MAX_SCENARIOS_PER_USER):
            d = ScenarioDefinition(
                id=f"user_hiepqn_fake_{i}",
                name=f"F{i}",
                owner_id="hiepqn",
                source_type="user",
                visibility="private",
                created_at=now, updated_at=now,
            )
            await repo.create_definition(d)

        try:
            await svc.create(_GOOD_YAML, USER)
            _check("quota exceeded raises", False)
        except QuotaExceeded:
            _check("quota exceeded raises", True)
    finally:
        await repo.close()
        ctx.cleanup()


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    groups = [
        test_validate_only,
        test_create,
        test_create_duplicate_name,
        test_create_bad_yaml_rejected,
        test_update_creates_new_revision,
        test_update_noop_rejected,
        test_update_permission,
        test_archive,
        test_list_for_user_permission,
        test_get_detail_permission,
        test_clone,
        test_clone_private_forbidden,
        test_quota,
    ]
    for fn in groups:
        try:
            await fn()
        except Exception:
            print(f"  [ERROR in {fn.__name__}]")
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
