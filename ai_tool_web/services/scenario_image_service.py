"""
services/scenario_image_service.py — Orchestrator cho Visual Hint Image
upload/list/delete (Phase 1).

Flow upload:
  1. Validate file (mime, size, filename safe)
  2. Compute sha256 → query repo find_by_sha256 (dedup: reuse cdn_url nếu tồn tại)
  3. Resolve scenario_code → scenario_id BIGINT (qua scenario_repo)
     Resolve (scenario_code, version_no) → revision_id BIGINT
  4. Build MinIO remote_dir:
        public/tool-web/prod/scenarios/{scenario_code}/rev_{version_no}/images
  5. Upload bytes → MinIO (skip nếu dedup cdn_url đã có)
  6. Idempotent overwrite: nếu (revision, filename) đã tồn tại → DELETE row cũ
  7. INSERT row mới
  8. Trả về ScenarioImage

Service KHÔNG biết về HTTP — route layer parse multipart, service nhận
bytes + meta. Auth check + validation diện rộng nằm ở route.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Optional

import requests

from auth.providers import AuthenticatedUser
from services.user_scenario_service import ScenarioForbidden
from store.mysql_scenario_image_repo import (
    MysqlScenarioImageRepo,
    ScenarioImage,
)
from store.scenario_repo import ScenarioDefinition, ScenarioRepository

_log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

MAX_SIZE_BYTES = 5 * 1024 * 1024            # 5 MB
ALLOWED_MIMES = frozenset({"image/png", "image/jpeg"})
ALLOWED_EXTS = frozenset({".png", ".jpg", ".jpeg"})
# Filename: chữ/số/_/-/dot, không slash/backslash, để tránh path traversal
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
_UPLOAD_TIMEOUT_S = 15


# ── Exceptions ───────────────────────────────────────────────────────────────

class ScenarioImageNotFound(Exception):
    """Scenario hoặc revision hoặc image không tồn tại."""


class ScenarioImageBadRequest(Exception):
    """Validation fail (file type, size, filename)."""


class ScenarioImageUploadFailed(Exception):
    """MinIO upload lỗi (network, auth, server)."""


# ── Validation helpers ───────────────────────────────────────────────────────

def _validate_filename(filename: str) -> None:
    if not filename or not _FILENAME_RE.match(filename):
        raise ScenarioImageBadRequest(
            f"Filename không hợp lệ: '{filename}'. "
            f"Chỉ chấp nhận chữ/số/dấu chấm/gạch ngang/gạch dưới."
        )
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise ScenarioImageBadRequest(
            f"Extension '{ext}' không hợp lệ. Cho phép: {sorted(ALLOWED_EXTS)}"
        )


def _validate_mime(mime: str) -> None:
    if mime not in ALLOWED_MIMES:
        raise ScenarioImageBadRequest(
            f"Mime '{mime}' không hợp lệ. Cho phép: {sorted(ALLOWED_MIMES)}"
        )


def _validate_size(size: int) -> None:
    if size <= 0:
        raise ScenarioImageBadRequest("File rỗng.")
    if size > MAX_SIZE_BYTES:
        raise ScenarioImageBadRequest(
            f"File {size} bytes vượt quá giới hạn {MAX_SIZE_BYTES} bytes (5MB)."
        )


# ── Upload helper ────────────────────────────────────────────────────────────

def _upload_bytes_to_minio(
    file_bytes: bytes,
    content_type: str,
    remote_dir: str,
    filename: str,
) -> str:
    """Upload bytes lên MinIO qua upload server. Trả CDN URL.

    Đọc UPLOAD_URL/UPLOAD_KEY/UPLOAD_SECRET/UPLOAD_BUCKET/PUBLIC_CDN_URL
    từ env (giống ArtifactUploader). Raise ScenarioImageUploadFailed
    nếu config thiếu hoặc HTTP fail.
    """
    upload_url = os.getenv("UPLOAD_URL", "")
    bucket = os.getenv("UPLOAD_BUCKET", "changchatbot")
    key = os.getenv("UPLOAD_KEY", "")
    secret = os.getenv("UPLOAD_SECRET", "")
    cdn = os.getenv("PUBLIC_CDN_URL", "").rstrip("/")

    if not (upload_url and key and secret):
        raise ScenarioImageUploadFailed(
            "Upload server chưa config (UPLOAD_URL/UPLOAD_KEY/UPLOAD_SECRET)."
        )

    endpoint = upload_url.rstrip("/") + "/api/v1/file/upload"
    try:
        resp = requests.post(
            endpoint,
            headers={
                "upload-bucket": bucket,
                "upload-key": key,
                "upload-secret": secret,
            },
            files={"file": (filename, file_bytes, content_type)},
            data={"dir": remote_dir, "keepOriginalName": "true"},
            timeout=_UPLOAD_TIMEOUT_S,
            proxies={"http": None, "https": None},
        )
        resp.raise_for_status()
    except requests.Timeout as e:
        raise ScenarioImageUploadFailed(
            f"Upload timeout sau {_UPLOAD_TIMEOUT_S}s"
        ) from e
    except requests.HTTPError as e:
        body_snippet = ""
        try:
            body_snippet = e.response.text[:200]
        except Exception:
            pass
        raise ScenarioImageUploadFailed(
            f"Upload HTTP {e.response.status_code}: {body_snippet}"
        ) from e
    except requests.RequestException as e:
        raise ScenarioImageUploadFailed(
            f"Upload network error: {type(e).__name__}: {e}"
        ) from e

    # Parse CDN URL từ response, fallback build thủ công
    try:
        body = resp.json()
        for field in ("url", "cdnUrl", "cdn_url", "publicUrl", "fileUrl"):
            val = body.get(field)
            if val:
                if val.startswith("http"):
                    return val
                return f"{cdn}/{bucket}/{val.lstrip('/')}"
    except Exception:
        pass
    return f"{cdn}/{bucket}/{remote_dir}/{filename}"


# ── Service ──────────────────────────────────────────────────────────────────

class ScenarioImageService:
    """Orchestrator cho image hint CRUD.

    Dependency:
    - scenario_repo: resolve scenario_code → BIGINT id, version_no → revision_id
    - image_repo: CRUD scenario_images table
    """

    def __init__(
        self,
        scenario_repo: ScenarioRepository,
        image_repo: MysqlScenarioImageRepo,
    ) -> None:
        self._scenario_repo = scenario_repo
        self._image_repo = image_repo

    async def upload_image(
        self,
        scenario_code: str,
        version_no: int,
        filename: str,
        file_bytes: bytes,
        mime_type: str,
        user: AuthenticatedUser,
        step_index: Optional[int] = None,
        step_note: Optional[str] = None,
    ) -> ScenarioImage:
        """Upload + insert image. Idempotent overwrite trong cùng revision.

        Permission: chỉ owner hoặc admin mới upload được. Builtin scenario
        thì chỉ admin được upload.

        Raise:
          ScenarioImageBadRequest — validation fail
          ScenarioImageNotFound  — scenario_code/version_no không tồn tại
          ScenarioForbidden — user không có quyền write scenario này
          ScenarioImageUploadFailed — MinIO upload error
        """
        # 1. Validate input
        _validate_filename(filename)
        _validate_mime(mime_type)
        _validate_size(len(file_bytes))

        # 2. Resolve scenario + revision IDs (BIGINT) + permission check
        defn = await self._scenario_repo.get_definition(scenario_code)
        if defn is None:
            raise ScenarioImageNotFound(
                f"Scenario '{scenario_code}' không tồn tại."
            )
        self._require_writable(defn, user)
        scenario_id, revision_id = await self._resolve_ids(
            scenario_code, version_no
        )

        # 3. Compute sha256 + dedup lookup
        sha256 = hashlib.sha256(file_bytes).hexdigest()
        existing = await self._image_repo.find_by_sha256(sha256)

        # 4. Upload (skip nếu dedup hit — reuse cdn_url cũ)
        if existing is not None:
            cdn_url = existing.cdn_url
            _log.info(
                "Image dedup hit sha256=%s — reuse cdn_url from image_id=%d",
                sha256[:12], existing.id,
            )
        else:
            remote_dir = self._build_remote_dir(scenario_code, version_no)
            cdn_url = _upload_bytes_to_minio(
                file_bytes=file_bytes,
                content_type=mime_type,
                remote_dir=remote_dir,
                filename=filename,
            )
            _log.info(
                "Image uploaded scenario=%s rev=%d filename=%s → %s",
                scenario_code, version_no, filename, cdn_url,
            )

        # 5. Idempotent overwrite — nếu (revision, filename) tồn tại → xóa row cũ
        existing_in_rev = await self._image_repo.get_image_by_filename(
            revision_id, filename
        )
        if existing_in_rev is not None:
            await self._image_repo.delete_image(revision_id, filename)
            _log.info(
                "Overwrite: deleted old image_id=%d (rev=%d, filename=%s)",
                existing_in_rev.id, revision_id, filename,
            )

        # 6. Insert
        new_image = ScenarioImage(
            revision_id=revision_id,
            scenario_id=scenario_id,
            filename=filename,
            cdn_url=cdn_url,
            mime_type=mime_type,
            size_bytes=len(file_bytes),
            sha256=sha256,
            step_index=step_index,
            step_note=step_note,
        )
        new_image.id = await self._image_repo.create_image(new_image)
        return new_image

    async def list_images(
        self,
        scenario_code: str,
        version_no: int,
        user: AuthenticatedUser,
    ) -> list[ScenarioImage]:
        """List ảnh của 1 revision.

        Permission: owner / admin / builtin (everyone read) / public visibility.
        Phase 1: chỉ owner + admin để giữ surface nhỏ — readable cho public
        sẽ mở khi có visibility flag.
        """
        defn = await self._scenario_repo.get_definition(scenario_code)
        if defn is None:
            raise ScenarioImageNotFound(
                f"Scenario '{scenario_code}' không tồn tại."
            )
        self._require_readable(defn, user)
        _, revision_id = await self._resolve_ids(scenario_code, version_no)
        return await self._image_repo.list_by_revision(revision_id)

    async def delete_image(
        self,
        scenario_code: str,
        version_no: int,
        filename: str,
        user: AuthenticatedUser,
    ) -> bool:
        """Xóa 1 ảnh khỏi DB (không xóa MinIO).

        Trả True nếu có row bị xóa, False nếu không tồn tại.
        ⚠️ MinIO file giữ lại — Phase sau có cron GC khi xóa revision.
        """
        _validate_filename(filename)
        defn = await self._scenario_repo.get_definition(scenario_code)
        if defn is None:
            raise ScenarioImageNotFound(
                f"Scenario '{scenario_code}' không tồn tại."
            )
        self._require_writable(defn, user)
        _, revision_id = await self._resolve_ids(scenario_code, version_no)
        return await self._image_repo.delete_image(revision_id, filename)

    # ── Internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _require_writable(
        defn: ScenarioDefinition, user: AuthenticatedUser
    ) -> None:
        """Mirror UserScenarioService._require_writable — owner hoặc admin.
        Builtin scenarios chỉ admin được sửa."""
        if user.is_admin:
            return
        if defn.source_type == "builtin":
            raise ScenarioForbidden(
                "Builtin scenario không sửa được qua API."
            )
        if defn.owner_id != user.user_id:
            raise ScenarioForbidden(
                f"User '{user.user_id}' không phải owner của '{defn.id}'."
            )

    @staticmethod
    def _require_readable(
        defn: ScenarioDefinition, user: AuthenticatedUser
    ) -> None:
        """Builtin + own scenarios. Phase 2 mở public/org."""
        if user.is_admin:
            return
        if defn.source_type == "builtin":
            return
        if defn.visibility == "public":
            return
        if defn.owner_id == user.user_id:
            return
        raise ScenarioForbidden(
            f"User '{user.user_id}' không có quyền xem '{defn.id}'."
        )

    async def _resolve_ids(
        self, scenario_code: str, version_no: int
    ) -> tuple[int, int]:
        """Resolve (scenario_code, version_no) → (scenario_id, revision_id) BIGINT.

        Raise ScenarioImageNotFound nếu scenario hoặc revision không tồn tại.
        """
        defn = await self._scenario_repo.get_definition(scenario_code)
        if defn is None:
            raise ScenarioImageNotFound(
                f"Scenario '{scenario_code}' không tồn tại."
            )
        rev = await self._scenario_repo.get_revision_by_version(
            scenario_code, version_no
        )
        if rev is None:
            raise ScenarioImageNotFound(
                f"Revision {version_no} của scenario '{scenario_code}' "
                f"không tồn tại."
            )
        # ScenarioRevision model không expose scenario_id BIGINT — lookup
        # qua repo internal _resolve_scenario_pk thì kín đáo. Cách an toàn:
        # bóc id BIGINT trực tiếp qua MysqlScenarioRepo nếu đó là backend.
        scenario_id = await self._resolve_scenario_pk(scenario_code)
        return scenario_id, rev.id  # type: ignore[return-value]

    async def _resolve_scenario_pk(self, scenario_code: str) -> int:
        """Lookup scenario_definitions.id (BIGINT) từ code.

        Hiện tại chỉ MysqlScenarioRepo implement _resolve_scenario_pk; SQLite
        có id riêng theo schema khác. Service Phase 1 chỉ chạy với MySQL
        backend ở production → expose helper này.
        """
        # Reuse repo's internal helper (same package)
        # Avoid duplicate query logic.
        from store.mysql_scenario_repo import MysqlScenarioRepo

        repo = self._scenario_repo
        if not isinstance(repo, MysqlScenarioRepo):
            raise ScenarioImageNotFound(
                "ScenarioImageService yêu cầu MySQL backend cho scenario_repo."
            )
        pool = repo._get_pool()  # type: ignore[attr-defined]
        import aiomysql

        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT id FROM scenario_definitions WHERE code = %s",
                    (scenario_code,),
                )
                row = await cur.fetchone()
        if row is None:
            raise ScenarioImageNotFound(
                f"Scenario '{scenario_code}' không tồn tại."
            )
        return int(row["id"])

    @staticmethod
    def _build_remote_dir(scenario_code: str, version_no: int) -> str:
        """Build MinIO remote_dir theo layout chốt (PLAN_VISUAL_HINTS Q-A).

        public/tool-web/prod/scenarios/{scenario_code}/rev_{version_no}/images
        """
        return (
            f"public/tool-web/prod/scenarios/{scenario_code}"
            f"/rev_{version_no}/images"
        )
