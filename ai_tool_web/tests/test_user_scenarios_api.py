"""
Test — user_scenarios API routes (isolated FastAPI app, TestClient, in-memory SQLite).

Không cần Redis/worker/uvicorn. Chạy:
  cd deploy_server/ai_tool_web
  python tests/test_user_scenarios_api.py
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


# Tempdir + event loop shared cho toàn bộ test
_TMPDIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
_DB_PATH = str(Path(_TMPDIR.name) / "test_api.db")
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


async def _init_repo():
    repo = SqliteScenarioRepo(_DB_PATH)
    await repo.init()
    return repo


_REPO = _LOOP.run_until_complete(_init_repo())


def _make_app() -> FastAPI:
    app = FastAPI()
    app.state.scenario_repo = _REPO
    app.state.auth_provider = MockAuthProvider()
    app.include_router(user_scenarios.router)
    app.include_router(user_hooks.router)
    register_scenario_exception_handlers(app)
    return app


_APP = _make_app()
_CLIENT = TestClient(_APP)


_GOOD_YAML = """
id: ignored
display_name: API Test Scenario
start_url: https://example.com
allowed_domains: [example.com]
inputs:
  - name: q
    type: string
    required: true
    source: context
goal: Tìm {q}
"""


_USER_HEADERS = {"x-user-id": "hiepqn"}
_OTHER_HEADERS = {"x-user-id": "stranger"}
_ADMIN_HEADERS = {"x-user-id": "admin"}
_NO_AUTH = {}


# ── Tests ────────────────────────────────────────────────────────────────────

def test_auth_required():
    print("=== 1. AUTH REQUIRED ===")
    r = _CLIENT.get("/v1/scenarios", headers=_NO_AUTH)
    _check("GET /v1/scenarios không header → 401", r.status_code == 401)

    r = _CLIENT.get("/v1/hooks", headers=_NO_AUTH)
    _check("GET /v1/hooks không header → 401", r.status_code == 401)


def test_validate_endpoint():
    print("\n=== 2. VALIDATE ENDPOINT ===")
    r = _CLIENT.post(
        "/v1/scenarios/validate",
        json={"raw_yaml": _GOOD_YAML},
        headers=_USER_HEADERS,
    )
    _check("POST /validate good → 200", r.status_code == 200, r.text[:200])
    data = r.json()
    _check("parse_ok=True", data["parse_ok"] is True)
    _check("validation_ok=True", data["validation_ok"] is True)
    _check("yaml_hash hex", len(data["yaml_hash"]) == 64)

    # Bad YAML
    r = _CLIENT.post(
        "/v1/scenarios/validate",
        json={"raw_yaml": "!!! broken"},
        headers=_USER_HEADERS,
    )
    _check("bad YAML validate → 200 với parse_ok=False",
           r.status_code == 200 and r.json()["parse_ok"] is False)


def test_create_and_list():
    print("\n=== 3. CREATE + LIST ===")
    r = _CLIENT.post(
        "/v1/scenarios",
        json={"raw_yaml": _GOOD_YAML},
        headers=_USER_HEADERS,
    )
    _check("POST /v1/scenarios → 201", r.status_code == 201, r.text[:200])
    defn = r.json()
    _check("id auto-prefixed user_hiepqn_",
           defn["id"].startswith("user_hiepqn_"))
    _check("owner_id=hiepqn", defn["owner_id"] == "hiepqn")

    # List
    r = _CLIENT.get("/v1/scenarios", headers=_USER_HEADERS)
    _check("GET /v1/scenarios → 200", r.status_code == 200)
    items = r.json()
    _check("list có ít nhất 1 item",
           any(d["id"] == defn["id"] for d in items))


def test_duplicate_conflict():
    print("\n=== 4. DUPLICATE → 409 ===")
    r = _CLIENT.post(
        "/v1/scenarios",
        json={"raw_yaml": _GOOD_YAML},
        headers=_USER_HEADERS,
    )
    _check("POST lần 2 cùng display_name → 409",
           r.status_code == 409, r.text[:200])
    _check("code ID_CONFLICT", r.json().get("code") == "ID_CONFLICT")


def test_update_flow():
    print("\n=== 5. UPDATE (new revision) ===")
    # Lấy scenario đã tạo ở T3
    r = _CLIENT.get("/v1/scenarios", headers=_USER_HEADERS)
    scenario_id = r.json()[0]["id"]

    # Edit
    r = _CLIENT.put(
        f"/v1/scenarios/{scenario_id}",
        json={"raw_yaml": _GOOD_YAML + "\n# edit\n"},
        headers=_USER_HEADERS,
    )
    _check("PUT → 200", r.status_code == 200, r.text[:200])
    _check("version_no=2", r.json()["version_no"] == 2)

    # Same YAML → 409 NO_CHANGE
    r = _CLIENT.put(
        f"/v1/scenarios/{scenario_id}",
        json={"raw_yaml": _GOOD_YAML + "\n# edit\n"},
        headers=_USER_HEADERS,
    )
    _check("PUT same YAML → 409",
           r.status_code == 409 and r.json().get("code") == "NO_CHANGE")


def test_permission():
    print("\n=== 6. PERMISSION ===")
    # Stranger list → không thấy scenario của USER
    r = _CLIENT.get("/v1/scenarios", headers=_OTHER_HEADERS)
    _check("stranger list → 200", r.status_code == 200)
    _check("stranger không thấy user's private",
           not any(d["owner_id"] == "hiepqn" for d in r.json()))

    # Stranger update của USER → 403
    r = _CLIENT.get("/v1/scenarios", headers=_USER_HEADERS)
    scenario_id = r.json()[0]["id"]

    r = _CLIENT.put(
        f"/v1/scenarios/{scenario_id}",
        json={"raw_yaml": _GOOD_YAML + "# stranger edit\n"},
        headers=_OTHER_HEADERS,
    )
    _check("stranger PUT → 403", r.status_code == 403)

    # Stranger GET detail của user private → 403
    r = _CLIENT.get(f"/v1/scenarios/{scenario_id}", headers=_OTHER_HEADERS)
    _check("stranger GET detail private → 403", r.status_code == 403)


def test_revisions_endpoints():
    print("\n=== 7. REVISIONS ENDPOINTS ===")
    r = _CLIENT.get("/v1/scenarios", headers=_USER_HEADERS)
    scenario_id = r.json()[0]["id"]

    r = _CLIENT.get(
        f"/v1/scenarios/{scenario_id}/revisions",
        headers=_USER_HEADERS,
    )
    _check("GET /revisions → 200", r.status_code == 200)
    revs = r.json()
    _check("≥ 1 revision", len(revs) >= 1)
    _check("newest-first",
           all(revs[i]["id"] > revs[i+1]["id"] for i in range(len(revs)-1))
           if len(revs) > 1 else True)

    # Full rev
    rev_id = revs[0]["id"]
    r = _CLIENT.get(
        f"/v1/scenarios/{scenario_id}/revisions/{rev_id}",
        headers=_USER_HEADERS,
    )
    _check("GET full revision → 200", r.status_code == 200)
    full = r.json()
    _check("full có raw_yaml", "raw_yaml" in full and full["raw_yaml"])
    _check("full có normalized_spec_json",
           "normalized_spec_json" in full and isinstance(full["normalized_spec_json"], dict))


def test_archive():
    print("\n=== 8. ARCHIVE ===")
    # Tạo 1 scenario mới (khác display_name để tránh conflict)
    yaml = _GOOD_YAML.replace("API Test Scenario", "Archive Me")
    r = _CLIENT.post("/v1/scenarios", json={"raw_yaml": yaml}, headers=_USER_HEADERS)
    scenario_id = r.json()["id"]

    r = _CLIENT.delete(f"/v1/scenarios/{scenario_id}", headers=_USER_HEADERS)
    _check("DELETE → 204", r.status_code == 204)

    # List mặc định không thấy
    r = _CLIENT.get("/v1/scenarios", headers=_USER_HEADERS)
    ids = [d["id"] for d in r.json()]
    _check("archived ẩn khỏi list mặc định", scenario_id not in ids)

    # is_archived=true → thấy
    r = _CLIENT.get("/v1/scenarios?is_archived=true", headers=_USER_HEADERS)
    ids = [d["id"] for d in r.json()]
    _check("is_archived=true thấy archived", scenario_id in ids)


def test_clone():
    print("\n=== 9. CLONE ===")
    r = _CLIENT.get("/v1/scenarios", headers=_USER_HEADERS)
    source_id = r.json()[0]["id"]

    r = _CLIENT.post(
        "/v1/scenarios/clone",
        json={"from_scenario_id": source_id, "new_display_name": "My Clone"},
        headers=_USER_HEADERS,
    )
    _check("POST /clone → 201", r.status_code == 201, r.text[:200])
    cloned = r.json()
    _check("source_type=cloned", cloned["source_type"] == "cloned")

    # Stranger clone private → 403
    r = _CLIENT.post(
        "/v1/scenarios/clone",
        json={"from_scenario_id": source_id},
        headers=_OTHER_HEADERS,
    )
    _check("stranger clone private → 403", r.status_code == 403)


def test_not_found():
    print("\n=== 10. NOT FOUND ===")
    r = _CLIENT.get("/v1/scenarios/non_exist_xxx", headers=_USER_HEADERS)
    _check("GET không tồn tại → 404", r.status_code == 404)


def test_hooks_endpoint():
    print("\n=== 11. HOOKS ENDPOINT ===")
    r = _CLIENT.get("/v1/hooks", headers=_USER_HEADERS)
    _check("GET /v1/hooks → 200", r.status_code == 200, r.text[:200])
    # Có thể empty nếu scenarios.hooks chưa import — OK
    _check("response là list", isinstance(r.json(), list))


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    groups = [
        test_auth_required,
        test_validate_endpoint,
        test_create_and_list,
        test_duplicate_conflict,
        test_update_flow,
        test_permission,
        test_revisions_endpoints,
        test_archive,
        test_clone,
        test_not_found,
        test_hooks_endpoint,
    ]
    for fn in groups:
        try:
            fn()
        except Exception:
            print(f"  [ERROR in {fn.__name__}]")
            traceback.print_exc()
            _FAIL.append((fn.__name__, "uncaught exception"))

    # Cleanup
    _LOOP.run_until_complete(_REPO.close())
    _TMPDIR.cleanup()

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
    sys.exit(main())
