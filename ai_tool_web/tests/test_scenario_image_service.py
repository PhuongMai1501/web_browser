"""
Test — ScenarioImageService

Unit tests dùng mock scenario_repo + image_repo. Không cần MySQL thực tế —
mock ScenarioRepository.get_definition / get_revision_by_version + monkeypatch
_resolve_scenario_pk + _upload_bytes_to_minio.

Cover:
- Validation (filename, mime, size)
- Permission check (owner / stranger / admin / builtin)
- Dedup hit reuse cdn_url
- Overwrite cùng filename trong cùng revision
- Not-found errors (scenario, revision)
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

from auth.providers import AuthenticatedUser  # noqa: E402
from services.scenario_image_service import (  # noqa: E402
    MAX_SIZE_BYTES,
    ScenarioImageBadRequest,
    ScenarioImageNotFound,
    ScenarioImageService,
)
from services.user_scenario_service import ScenarioForbidden  # noqa: E402
from store.mysql_scenario_image_repo import ScenarioImage  # noqa: E402
from store.scenario_repo import (  # noqa: E402
    ScenarioDefinition,
    ScenarioRevision,
)


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


# ── Fixtures helpers ─────────────────────────────────────────────────────────

def _make_defn(
    code: str = "user_hiepqn_test",
    owner_id: str = "hiepqn",
    source_type: str = "user",
    visibility: str = "private",
) -> ScenarioDefinition:
    now = datetime.now(timezone.utc)
    return ScenarioDefinition(
        id=code, name="Test", owner_id=owner_id,
        source_type=source_type, visibility=visibility,
        published_revision_id=None, is_archived=False,
        created_at=now, updated_at=now,
    )


def _make_rev(rev_id: int = 100, version_no: int = 1) -> ScenarioRevision:
    return ScenarioRevision(
        id=rev_id, scenario_id="user_hiepqn_test", version_no=version_no,
        raw_yaml="x: 1", normalized_spec_json={}, yaml_hash="h",
        static_validation_status="passed",
        created_by="hiepqn", created_at=datetime.now(timezone.utc),
    )


def _make_service(
    *,
    defn: Optional[ScenarioDefinition] = None,
    rev: Optional[ScenarioRevision] = None,
    scenario_pk: int = 42,
    sha256_existing: Optional[ScenarioImage] = None,
    rev_filename_existing: Optional[ScenarioImage] = None,
):
    scenario_repo = MagicMock()
    scenario_repo.get_definition = AsyncMock(return_value=defn)
    scenario_repo.get_revision_by_version = AsyncMock(return_value=rev)

    image_repo = MagicMock()
    image_repo.find_by_sha256 = AsyncMock(return_value=sha256_existing)
    image_repo.get_image_by_filename = AsyncMock(
        return_value=rev_filename_existing
    )
    image_repo.delete_image = AsyncMock(return_value=True)
    image_repo.create_image = AsyncMock(return_value=999)
    image_repo.list_by_revision = AsyncMock(return_value=[])

    service = ScenarioImageService(scenario_repo, image_repo)
    # Patch _resolve_scenario_pk để bypass MysqlScenarioRepo check
    service._resolve_scenario_pk = AsyncMock(return_value=scenario_pk)
    return service, scenario_repo, image_repo


# ── Validation tests ─────────────────────────────────────────────────────────

async def test_filename_validation():
    print("\n[test_filename_validation]")
    service, _, _ = _make_service(defn=_make_defn(), rev=_make_rev())
    bad_names = ["", "has space.png", "../etc.png", "no_ext", "x.gif",
                 "dir/file.png"]
    for bad in bad_names:
        try:
            await service.upload_image(
                "user_hiepqn_test", 1, bad, b"x" * 10, "image/png", USER,
            )
            _check(f"reject filename {bad!r}", False, "did not raise")
        except ScenarioImageBadRequest:
            _check(f"reject filename {bad!r}", True)


async def test_mime_validation():
    print("\n[test_mime_validation]")
    service, _, _ = _make_service(defn=_make_defn(), rev=_make_rev())
    for bad in ["image/gif", "application/pdf", ""]:
        try:
            await service.upload_image(
                "user_hiepqn_test", 1, "x.png", b"x" * 10, bad, USER,
            )
            _check(f"reject mime {bad!r}", False, "did not raise")
        except ScenarioImageBadRequest:
            _check(f"reject mime {bad!r}", True)


async def test_size_validation():
    print("\n[test_size_validation]")
    service, _, _ = _make_service(defn=_make_defn(), rev=_make_rev())
    # Empty
    try:
        await service.upload_image(
            "user_hiepqn_test", 1, "x.png", b"", "image/png", USER,
        )
        _check("reject empty file", False)
    except ScenarioImageBadRequest:
        _check("reject empty file", True)

    # Too big
    try:
        await service.upload_image(
            "user_hiepqn_test", 1, "x.png",
            b"x" * (MAX_SIZE_BYTES + 1), "image/png", USER,
        )
        _check("reject >5MB file", False)
    except ScenarioImageBadRequest:
        _check("reject >5MB file", True)


# ── Not-found tests ──────────────────────────────────────────────────────────

async def test_scenario_not_found():
    print("\n[test_scenario_not_found]")
    service, _, _ = _make_service(defn=None)
    try:
        await service.upload_image(
            "missing", 1, "x.png", b"data", "image/png", USER,
        )
        _check("scenario_not_found raises", False)
    except ScenarioImageNotFound:
        _check("scenario_not_found raises", True)


async def test_revision_not_found():
    print("\n[test_revision_not_found]")
    service, _, _ = _make_service(defn=_make_defn(), rev=None)
    try:
        await service.upload_image(
            "user_hiepqn_test", 99, "x.png", b"data", "image/png", USER,
        )
        _check("revision_not_found raises", False)
    except ScenarioImageNotFound:
        _check("revision_not_found raises", True)


# ── Permission tests ────────────────────────────────────────────────────────

async def test_permission_owner_ok():
    print("\n[test_permission_owner_ok]")
    service, _, image_repo = _make_service(
        defn=_make_defn(owner_id="hiepqn"),
        rev=_make_rev(),
    )
    with patch(
        "services.scenario_image_service._upload_bytes_to_minio",
        return_value="https://cdn/foo.png",
    ):
        result = await service.upload_image(
            "user_hiepqn_test", 1, "x.png",
            b"\x89PNG\r\n\x1a\n" + b"x" * 100, "image/png", USER,
        )
    _check("owner can upload", result.id == 999)
    _check(
        "create_image called",
        image_repo.create_image.await_count == 1,
    )


async def test_permission_stranger_forbidden():
    print("\n[test_permission_stranger_forbidden]")
    service, _, _ = _make_service(
        defn=_make_defn(owner_id="hiepqn"),
        rev=_make_rev(),
    )
    try:
        await service.upload_image(
            "user_hiepqn_test", 1, "x.png", b"data" * 100,
            "image/png", STRANGER,
        )
        _check("stranger forbidden raises", False)
    except ScenarioForbidden:
        _check("stranger forbidden raises", True)


async def test_permission_admin_can_upload_builtin():
    print("\n[test_permission_admin_can_upload_builtin]")
    service, _, _ = _make_service(
        defn=_make_defn(source_type="builtin", owner_id=None),
        rev=_make_rev(),
    )
    with patch(
        "services.scenario_image_service._upload_bytes_to_minio",
        return_value="https://cdn/foo.png",
    ):
        result = await service.upload_image(
            "builtin_x", 1, "x.png", b"data" * 100, "image/png", ADMIN,
        )
    _check("admin uploads builtin", result.id == 999)


async def test_permission_user_cannot_upload_builtin():
    print("\n[test_permission_user_cannot_upload_builtin]")
    service, _, _ = _make_service(
        defn=_make_defn(source_type="builtin", owner_id=None),
        rev=_make_rev(),
    )
    try:
        await service.upload_image(
            "builtin_x", 1, "x.png", b"data" * 100, "image/png", USER,
        )
        _check("non-admin builtin forbidden", False)
    except ScenarioForbidden:
        _check("non-admin builtin forbidden", True)


# ── Dedup + overwrite tests ─────────────────────────────────────────────────

async def test_dedup_reuses_cdn_url():
    print("\n[test_dedup_reuses_cdn_url]")
    existing = ScenarioImage(
        id=1, revision_id=999, scenario_id=42,
        filename="other.png", cdn_url="https://cdn/dedup.png",
        mime_type="image/png", size_bytes=10, sha256="x",
    )
    service, _, image_repo = _make_service(
        defn=_make_defn(),
        rev=_make_rev(),
        sha256_existing=existing,
    )
    upload_mock = MagicMock()
    with patch(
        "services.scenario_image_service._upload_bytes_to_minio",
        upload_mock,
    ):
        result = await service.upload_image(
            "user_hiepqn_test", 1, "new.png",
            b"data" * 100, "image/png", USER,
        )
    _check(
        "dedup skips upload",
        upload_mock.call_count == 0,
        f"got {upload_mock.call_count} calls",
    )
    _check(
        "dedup reuses cdn_url",
        result.cdn_url == "https://cdn/dedup.png",
    )


async def test_overwrite_deletes_old_row():
    print("\n[test_overwrite_deletes_old_row]")
    old = ScenarioImage(
        id=77, revision_id=100, scenario_id=42,
        filename="x.png", cdn_url="https://cdn/old.png",
        mime_type="image/png", size_bytes=10,
    )
    service, _, image_repo = _make_service(
        defn=_make_defn(),
        rev=_make_rev(rev_id=100),
        rev_filename_existing=old,
    )
    with patch(
        "services.scenario_image_service._upload_bytes_to_minio",
        return_value="https://cdn/new.png",
    ):
        await service.upload_image(
            "user_hiepqn_test", 1, "x.png",
            b"newdata" * 100, "image/png", USER,
        )
    _check(
        "overwrite calls delete_image",
        image_repo.delete_image.await_count == 1,
    )
    _check(
        "overwrite calls create_image",
        image_repo.create_image.await_count == 1,
    )


# ── Runner ───────────────────────────────────────────────────────────────────

async def main():
    tests = [
        test_filename_validation,
        test_mime_validation,
        test_size_validation,
        test_scenario_not_found,
        test_revision_not_found,
        test_permission_owner_ok,
        test_permission_stranger_forbidden,
        test_permission_admin_can_upload_builtin,
        test_permission_user_cannot_upload_builtin,
        test_dedup_reuses_cdn_url,
        test_overwrite_deletes_old_row,
    ]
    for t in tests:
        try:
            await t()
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
    asyncio.run(main())
