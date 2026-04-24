"""
Test — Phase 1 end-to-end integration

Flow đầy đủ: seed builtin → user create → validate → update → list revisions
→ clone → edit cloned → archive → permission check → inputs validate.

Chạy:
  cd deploy_server/ai_tool_web
  python tests/test_integration_phase1.py

Đây là GATE 2 test. Pass = cho phép chuyển sang frontend.
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


from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.exception_handlers import register_scenario_exception_handlers  # noqa: E402
from api.routes import user_hooks, user_scenarios  # noqa: E402
from auth.mock_provider import MockAuthProvider  # noqa: E402
from services.builtin_seeder import seed_builtin_from_yaml  # noqa: E402
from services.inputs_validator import (  # noqa: E402
    InputValidationError,
    validate_inputs,
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


_TMPDIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
_DB_PATH = str(Path(_TMPDIR.name) / "e2e.db")
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


async def _init():
    repo = SqliteScenarioRepo(_DB_PATH)
    await repo.init()
    return repo


_REPO = _LOOP.run_until_complete(_init())


def _make_app() -> FastAPI:
    app = FastAPI()
    app.state.scenario_repo = _REPO
    app.state.auth_provider = MockAuthProvider()
    app.include_router(user_scenarios.router)
    app.include_router(user_hooks.router)
    register_scenario_exception_handlers(app)
    return app


_APP = _make_app()
_C = TestClient(_APP)

_USER = {"x-user-id": "hiepqn"}
_OTHER = {"x-user-id": "alice"}
_ADMIN = {"x-user-id": "admin"}


# Full YAML dùng xuyên suốt — có đủ declarative flow v2 fields
_FULL_YAML = """
id: ignored
display_name: Tìm kiếm pháp luật E2E
description: Test end-to-end
start_url: https://thuvienphapluat.vn
allowed_domains: [thuvienphapluat.vn]
mode: agent
max_steps_default: 10
inputs:
  - name: keyword
    type: string
    required: true
    source: context
    description: Từ khóa cần tìm
  - name: otp
    type: string
    required: false
    source: ask_user
    description: OTP sẽ hỏi runtime
goal: "Tìm {keyword} trên thuvienphapluat.vn"
"""


# ── Tests ────────────────────────────────────────────────────────────────────

def test_seed_builtin():
    print("=== 1. SEED BUILTIN (G2) ===")
    n = _LOOP.run_until_complete(seed_builtin_from_yaml(_REPO))
    _check("seed returns count >= 0", n >= 0)

    # List builtins qua API
    r = _C.get("/v1/scenarios?source_type=builtin", headers=_USER)
    _check("GET /v1/scenarios?source_type=builtin → 200", r.status_code == 200)
    builtins = r.json()
    _check(f"có {n} builtin trong list", len(builtins) == n)
    if builtins:
        _check("builtin owner_id=None",
               all(d["owner_id"] is None for d in builtins))
        _check("builtin có published_revision_id",
               all(d["published_revision_id"] is not None for d in builtins))

    # Re-seed idempotent
    n2 = _LOOP.run_until_complete(seed_builtin_from_yaml(_REPO))
    _check("re-seed idempotent (returns 0)", n2 == 0)


def test_full_user_flow():
    print("\n=== 2. FULL USER FLOW ===")

    # 2.1 Validate trước
    r = _C.post("/v1/scenarios/validate",
                json={"raw_yaml": _FULL_YAML}, headers=_USER)
    _check("validate OK", r.status_code == 200 and r.json()["validation_ok"])

    # 2.2 Create
    r = _C.post("/v1/scenarios", json={"raw_yaml": _FULL_YAML}, headers=_USER)
    _check("create → 201", r.status_code == 201)
    sid = r.json()["id"]

    # 2.3 Get detail
    r = _C.get(f"/v1/scenarios/{sid}", headers=_USER)
    _check("get detail → 200", r.status_code == 200)
    detail = r.json()
    _check("detail.definition.id match", detail["definition"]["id"] == sid)
    _check("detail có latest_revision", detail["latest_revision"] is not None)
    _check("detail published_revision=None (chưa admin publish)",
           detail["published_revision"] is None)

    # 2.4 Update 2 lần
    for i in range(2):
        r = _C.put(f"/v1/scenarios/{sid}",
                   json={"raw_yaml": _FULL_YAML + f"\n# edit {i}\n"},
                   headers=_USER)
        _check(f"PUT edit {i+1} → 200", r.status_code == 200)

    # 2.5 List revisions
    r = _C.get(f"/v1/scenarios/{sid}/revisions", headers=_USER)
    _check("list revisions → 200", r.status_code == 200)
    revs = r.json()
    _check("3 revisions (1 create + 2 update)", len(revs) == 3)
    _check("versions [3,2,1] newest-first",
           [r["version_no"] for r in revs] == [3, 2, 1])

    # 2.6 Parent chain
    _check("rev 1 parent=None", revs[2]["parent_revision_id"] is None)
    _check("rev 2 parent=rev1.id",
           revs[1]["parent_revision_id"] == revs[2]["id"])
    _check("rev 3 parent=rev2.id",
           revs[0]["parent_revision_id"] == revs[1]["id"])

    # 2.7 Get full revision
    rev_id = revs[0]["id"]
    r = _C.get(f"/v1/scenarios/{sid}/revisions/{rev_id}", headers=_USER)
    _check("get full rev → 200", r.status_code == 200)
    full = r.json()
    _check("full có raw_yaml", "raw_yaml" in full and len(full["raw_yaml"]) > 0)
    _check("full có normalized_spec_json inputs",
           full["normalized_spec_json"].get("inputs", []))

    # 2.8 Simulate admin publish qua "SQL" (direct repo call)
    _LOOP.run_until_complete(_REPO.set_published_revision(sid, rev_id))

    r = _C.get(f"/v1/scenarios/{sid}", headers=_USER)
    _check("sau publish: detail.published_revision_id match",
           r.json()["definition"]["published_revision_id"] == rev_id)

    # 2.9 Clone
    r = _C.post("/v1/scenarios/clone",
                json={"from_scenario_id": sid, "new_display_name": "My Fork"},
                headers=_USER)
    _check("clone → 201", r.status_code == 201)
    clone_id = r.json()["id"]
    _check("cloned source_type=cloned", r.json()["source_type"] == "cloned")

    # 2.10 Edit cloned → parent trong clone, không link về source
    r = _C.put(f"/v1/scenarios/{clone_id}",
               json={"raw_yaml": _FULL_YAML + "# my fork edit\n"},
               headers=_USER)
    _check("edit cloned → 200", r.status_code == 200)

    r = _C.get(f"/v1/scenarios/{clone_id}/revisions", headers=_USER)
    clone_revs = r.json()
    _check("cloned có 2 revisions", len(clone_revs) == 2)
    _check("cloned rev 1 có clone_source_revision_id set",
           clone_revs[1]["clone_source_revision_id"] == rev_id)
    _check("cloned rev 2 clone_source=None (chỉ rev 1 set)",
           clone_revs[0]["clone_source_revision_id"] is None)

    # 2.11 Archive original
    r = _C.delete(f"/v1/scenarios/{sid}", headers=_USER)
    _check("archive original → 204", r.status_code == 204)

    # 2.12 List ẩn archived (mặc định)
    r = _C.get("/v1/scenarios", headers=_USER)
    ids = [d["id"] for d in r.json()]
    _check("archived ẩn khỏi default list", sid not in ids)
    _check("cloned vẫn trong list", clone_id in ids)


def test_permission_matrix():
    print("\n=== 3. PERMISSION MATRIX ===")

    # Alice create
    r = _C.post("/v1/scenarios",
                json={"raw_yaml": _FULL_YAML.replace("pháp luật E2E", "Alice's Scenario")},
                headers=_OTHER)
    _check("alice create → 201", r.status_code == 201)
    alice_sid = r.json()["id"]

    # hiepqn không thấy alice's private
    r = _C.get("/v1/scenarios", headers=_USER)
    ids = [d["id"] for d in r.json()]
    _check("hiepqn không thấy alice's", alice_sid not in ids)

    # hiepqn không get detail được
    r = _C.get(f"/v1/scenarios/{alice_sid}", headers=_USER)
    _check("hiepqn GET alice's detail → 403", r.status_code == 403)

    # hiepqn không update được
    r = _C.put(f"/v1/scenarios/{alice_sid}",
               json={"raw_yaml": _FULL_YAML + "# sneak\n"}, headers=_USER)
    _check("hiepqn PUT alice's → 403", r.status_code == 403)

    # hiepqn không archive được
    r = _C.delete(f"/v1/scenarios/{alice_sid}", headers=_USER)
    _check("hiepqn DELETE alice's → 403", r.status_code == 403)

    # hiepqn không clone private
    r = _C.post("/v1/scenarios/clone",
                json={"from_scenario_id": alice_sid}, headers=_USER)
    _check("hiepqn clone alice's private → 403", r.status_code == 403)

    # Admin thấy tất
    r = _C.get("/v1/scenarios", headers=_ADMIN)
    ids = [d["id"] for d in r.json()]
    _check("admin thấy alice's", alice_sid in ids)

    # Admin clone được
    r = _C.post("/v1/scenarios/clone",
                json={"from_scenario_id": alice_sid,
                      "new_display_name": "Admin Clone"},
                headers=_ADMIN)
    _check("admin clone private OK", r.status_code == 201)


def test_inputs_validator_e2e():
    print("\n=== 4. INPUTS VALIDATOR (G5) ===")

    # Lấy scenario từ DB để có spec
    import sys as _sys
    _LLM_BASE = Path(__file__).parent.parent.parent / "LLM_base"
    if str(_LLM_BASE) not in _sys.path:
        _sys.path.append(str(_LLM_BASE))
    from scenarios.spec import ScenarioSpec

    r = _C.get("/v1/scenarios", headers=_OTHER)   # alice's scenario
    alice_sid = [d["id"] for d in r.json() if d["owner_id"] == "alice"][0]
    r = _C.get(f"/v1/scenarios/{alice_sid}", headers=_OTHER)
    latest_rev_id = r.json()["latest_revision"]["id"]
    r = _C.get(f"/v1/scenarios/{alice_sid}/revisions/{latest_rev_id}",
               headers=_OTHER)
    normalized = r.json()["normalized_spec_json"]
    spec = ScenarioSpec.model_validate(normalized)

    # Happy path
    result = validate_inputs(spec, {"keyword": "nghị định"})
    _check("required keyword pass", result.context["keyword"] == "nghị định")
    _check("ask_user field tách riêng", "otp" in result.ask_user_fields)
    _check("ask_user không vào context", "otp" not in result.context)

    # Missing required
    try:
        validate_inputs(spec, {})
        _check("missing required raises", False)
    except InputValidationError as e:
        _check("missing required raises", True)
        _check("error mention keyword",
               any(x["field"] == "keyword" for x in e.errors))


def test_hooks_endpoint():
    print("\n=== 5. HOOKS ENDPOINT (G7) ===")
    r = _C.get("/v1/hooks", headers=_USER)
    _check("GET /v1/hooks → 200", r.status_code == 200)
    hooks = r.json()
    _check("response là list", isinstance(hooks, list))
    # Note: HOOK_REGISTRY rỗng nếu scenarios.hooks không import.
    # Just verify format.
    if hooks:
        _check("hook có name + description",
               all("name" in h and "description" in h for h in hooks))


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    groups = [
        test_seed_builtin,
        test_full_user_flow,
        test_permission_matrix,
        test_inputs_validator_e2e,
        test_hooks_endpoint,
    ]
    for fn in groups:
        try:
            fn()
        except Exception:
            print(f"  [ERROR in {fn.__name__}]")
            traceback.print_exc()
            _FAIL.append((fn.__name__, "uncaught exception"))

    _LOOP.run_until_complete(_REPO.close())
    _TMPDIR.cleanup()

    print(f"\n{'='*50}")
    print(f"Total: {len(_PASS)} pass, {len(_FAIL)} fail")
    if _FAIL:
        print("\nFailed:")
        for label, detail in _FAIL:
            print(f"  - {label}: {detail}")
        return 1
    print("\n[ALL PASS — GATE 2 OK]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
