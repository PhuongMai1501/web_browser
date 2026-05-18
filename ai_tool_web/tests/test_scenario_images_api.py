"""
Test — scenario_images API routes (TestClient + dependency override).

Mock ScenarioImageService trong DI override; verify route serialization,
auth, error mapping cho 3 endpoint POST/GET/DELETE.

Chạy:
  cd deploy_server/ai_tool_web
  python tests/test_scenario_images_api.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.dependencies import get_image_service  # noqa: E402
from api.exception_handlers import (  # noqa: E402
    register_scenario_exception_handlers,
)
from api.routes import scenario_images  # noqa: E402
from auth.mock_provider import MockAuthProvider  # noqa: E402
from services.scenario_image_service import (  # noqa: E402
    ScenarioImageBadRequest,
    ScenarioImageNotFound,
    ScenarioImageUploadFailed,
)
from services.user_scenario_service import ScenarioForbidden  # noqa: E402
from store.mysql_scenario_image_repo import ScenarioImage  # noqa: E402


_PASS: list[str] = []
_FAIL: list[tuple[str, str]] = []


def _check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASS.append(label)
        print(f"  [PASS] {label}")
    else:
        _FAIL.append((label, detail))
        print(f"  [FAIL] {label}{(' -> ' + detail) if detail else ''}")


def _sample_image(filename: str = "step_10.png") -> ScenarioImage:
    return ScenarioImage(
        id=1, revision_id=100, scenario_id=42,
        filename=filename, cdn_url=f"https://cdn/{filename}",
        mime_type="image/png", size_bytes=1024, sha256="a" * 64,
        step_index=10, step_note="Click here",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _make_app(service_mock) -> FastAPI:
    """Build minimal app với service overridden."""
    app = FastAPI()
    app.state.auth_provider = MockAuthProvider()
    app.include_router(scenario_images.router)
    register_scenario_exception_handlers(app)
    app.dependency_overrides[get_image_service] = lambda: service_mock
    return app


# ── Tests ────────────────────────────────────────────────────────────────────

def test_upload_no_auth():
    print("\n[test_upload_no_auth]")
    service = AsyncMock()
    app = _make_app(service)
    client = TestClient(app)
    r = client.post(
        "/v1/scenarios/foo/revisions/1/images",
        files={"file": ("a.png", b"data", "image/png")},
    )
    _check("no auth → 401", r.status_code == 401, f"got {r.status_code}")


def test_upload_happy_path():
    print("\n[test_upload_happy_path]")
    service = AsyncMock()
    service.upload_image = AsyncMock(return_value=_sample_image())
    app = _make_app(service)
    client = TestClient(app)

    r = client.post(
        "/v1/scenarios/foo/revisions/1/images",
        headers={"X-User-Id": "hiepqn"},
        files={"file": ("step_10.png", b"x" * 100, "image/png")},
        data={"step_index": "10", "step_note": "Click here"},
    )
    _check("upload happy → 201", r.status_code == 201, r.text[:200])
    if r.status_code == 201:
        body = r.json()
        _check("response has id", body.get("id") == 1)
        _check("response has cdn_url", body.get("cdn_url", "").startswith("https://"))
        _check("response has step_index", body.get("step_index") == 10)
        # Verify service was called with correct args
        call = service.upload_image.await_args
        _check(
            "service called scenario_code=foo",
            call.kwargs["scenario_code"] == "foo",
        )
        _check(
            "service called version_no=1",
            call.kwargs["version_no"] == 1,
        )
        _check(
            "service got file_bytes",
            call.kwargs["file_bytes"] == b"x" * 100,
        )


def test_upload_bad_request():
    print("\n[test_upload_bad_request]")
    service = AsyncMock()
    service.upload_image = AsyncMock(
        side_effect=ScenarioImageBadRequest("file too big")
    )
    app = _make_app(service)
    client = TestClient(app)
    r = client.post(
        "/v1/scenarios/foo/revisions/1/images",
        headers={"X-User-Id": "hiepqn"},
        files={"file": ("a.png", b"x", "image/png")},
    )
    _check("bad request → 400", r.status_code == 400, r.text[:100])
    _check("error detail in body", "file too big" in r.text)


def test_upload_not_found():
    print("\n[test_upload_not_found]")
    service = AsyncMock()
    service.upload_image = AsyncMock(
        side_effect=ScenarioImageNotFound("scenario missing")
    )
    app = _make_app(service)
    client = TestClient(app)
    r = client.post(
        "/v1/scenarios/foo/revisions/1/images",
        headers={"X-User-Id": "hiepqn"},
        files={"file": ("a.png", b"x", "image/png")},
    )
    _check("not found → 404", r.status_code == 404)


def test_upload_forbidden():
    print("\n[test_upload_forbidden]")
    service = AsyncMock()
    service.upload_image = AsyncMock(
        side_effect=ScenarioForbidden("nope")
    )
    app = _make_app(service)
    client = TestClient(app)
    r = client.post(
        "/v1/scenarios/foo/revisions/1/images",
        headers={"X-User-Id": "stranger"},
        files={"file": ("a.png", b"x", "image/png")},
    )
    _check("forbidden → 403", r.status_code == 403)


def test_upload_minio_failure():
    print("\n[test_upload_minio_failure]")
    service = AsyncMock()
    service.upload_image = AsyncMock(
        side_effect=ScenarioImageUploadFailed("MinIO down")
    )
    app = _make_app(service)
    client = TestClient(app)
    r = client.post(
        "/v1/scenarios/foo/revisions/1/images",
        headers={"X-User-Id": "hiepqn"},
        files={"file": ("a.png", b"x", "image/png")},
    )
    _check("upload failure → 502", r.status_code == 502)


def test_upload_step_note_too_long():
    print("\n[test_upload_step_note_too_long]")
    service = AsyncMock()
    service.upload_image = AsyncMock(return_value=_sample_image())
    app = _make_app(service)
    client = TestClient(app)
    r = client.post(
        "/v1/scenarios/foo/revisions/1/images",
        headers={"X-User-Id": "hiepqn"},
        files={"file": ("a.png", b"x" * 100, "image/png")},
        data={"step_note": "x" * 256},
    )
    _check(
        "step_note >255 chars → 400",
        r.status_code == 400,
        f"got {r.status_code}",
    )


def test_list_happy():
    print("\n[test_list_happy]")
    service = AsyncMock()
    service.list_images = AsyncMock(
        return_value=[_sample_image("a.png"), _sample_image("b.png")]
    )
    app = _make_app(service)
    client = TestClient(app)
    r = client.get(
        "/v1/scenarios/foo/revisions/1/images",
        headers={"X-User-Id": "hiepqn"},
    )
    _check("list → 200", r.status_code == 200, r.text[:200])
    if r.status_code == 200:
        body = r.json()
        _check("list returns 2 items", len(body.get("images", [])) == 2)


def test_list_not_found():
    print("\n[test_list_not_found]")
    service = AsyncMock()
    service.list_images = AsyncMock(
        side_effect=ScenarioImageNotFound("nope")
    )
    app = _make_app(service)
    client = TestClient(app)
    r = client.get(
        "/v1/scenarios/foo/revisions/1/images",
        headers={"X-User-Id": "hiepqn"},
    )
    _check("list not found → 404", r.status_code == 404)


def test_delete_happy():
    print("\n[test_delete_happy]")
    service = AsyncMock()
    service.delete_image = AsyncMock(return_value=True)
    app = _make_app(service)
    client = TestClient(app)
    r = client.delete(
        "/v1/scenarios/foo/revisions/1/images/x.png",
        headers={"X-User-Id": "hiepqn"},
    )
    _check("delete → 200", r.status_code == 200, r.text[:200])
    if r.status_code == 200:
        _check("delete returns deleted=true", r.json().get("deleted") is True)


def test_delete_not_found():
    print("\n[test_delete_not_found]")
    service = AsyncMock()
    service.delete_image = AsyncMock(return_value=False)
    app = _make_app(service)
    client = TestClient(app)
    r = client.delete(
        "/v1/scenarios/foo/revisions/1/images/x.png",
        headers={"X-User-Id": "hiepqn"},
    )
    # Service trả False — route trả 200 với deleted=false (idempotent semantics)
    _check("delete missing → 200 deleted=false", r.status_code == 200)
    if r.status_code == 200:
        _check(
            "delete missing returns deleted=false",
            r.json().get("deleted") is False,
        )


# ── Runner ───────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_upload_no_auth,
        test_upload_happy_path,
        test_upload_bad_request,
        test_upload_not_found,
        test_upload_forbidden,
        test_upload_minio_failure,
        test_upload_step_note_too_long,
        test_list_happy,
        test_list_not_found,
        test_delete_happy,
        test_delete_not_found,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            _FAIL.append((t.__name__, f"crash: {e}"))
            traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
    if _FAIL:
        print("\nFailures:")
        for label, detail in _FAIL:
            print(f"  - {label}: {detail}")
        sys.exit(1)


if __name__ == "__main__":
    main()
