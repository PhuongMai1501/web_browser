"""
services/user_scenario_service.py — Orchestrator for scenario CRUD.

Layer giữa HTTP routes và repo. Trách nhiệm:
- Parse + validate YAML (yaml_normalizer)
- Quyết định id (auto-prefix cho user-created)
- Permission check (owner-only cho write, visibility cho read)
- Append revision với parent linking
- Detect no-op save (hash collision)
- Clone flow (set clone_source_revision_id immutable)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from auth.providers import AuthenticatedUser
from services.yaml_normalizer import NormalizeResult, normalize_yaml
from store.scenario_repo import (
    DefinitionFilters,
    ScenarioDefinition,
    ScenarioRepository,
    ScenarioRevision,
)

# Optional dep — Phase 1 Input Fields integration.
# Import lazy (TYPE_CHECKING) để không break test cũ không config input_field_repo.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from store.mysql_scenario_input_field_repo import MysqlScenarioInputFieldRepo


_log = logging.getLogger(__name__)


# ── Exceptions ───────────────────────────────────────────────────────────────

class ScenarioNotFound(Exception):
    pass


class ScenarioConflict(Exception):
    """id đã tồn tại, hoặc no-op save (hash trùng)."""
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class ScenarioForbidden(Exception):
    """User không có quyền write lên scenario này."""


class ScenarioBadRequest(Exception):
    """YAML parse lỗi (hard reject) hoặc input bất hợp lệ."""
    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__(f"Bad request: {errors}")


class QuotaExceeded(Exception):
    """User vượt quota scenarios."""


# ── Result DTOs ──────────────────────────────────────────────────────────────

@dataclass
class ScenarioDetail:
    """Response shape cho GET /scenarios/{id}."""
    definition: ScenarioDefinition
    latest_revision: Optional[ScenarioRevision]
    published_revision: Optional[ScenarioRevision]


# ── Config ───────────────────────────────────────────────────────────────────

MAX_SCENARIOS_PER_USER = 100

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, max_len: int = 40) -> str:
    """'Tìm kiếm Luật ABC' → 'tim_kiem_luat_abc' (ASCII best-effort)."""
    import unicodedata
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("_", ascii_text.lower()).strip("_")
    return slug[:max_len] or "scenario"


def _user_scenario_id(user_id: str, display_name: str) -> str:
    """Generate id cho user-created scenario: user_<owner>_<slug>."""
    return f"user_{_slugify(user_id, 20)}_{_slugify(display_name)}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Service ──────────────────────────────────────────────────────────────────

class UserScenarioService:
    """CRUD orchestrator. Stateless — dùng via DI.

    Phase 1 Input Fields (2026-05-11): optional `input_field_repo` để auto
    sync inputs[] từ YAML vào bảng scenario_input_fields khi tạo revision mới.
    Nếu None → skip sync (backward compatible cho test legacy).
    """

    def __init__(
        self,
        repo: ScenarioRepository,
        input_field_repo: Optional["MysqlScenarioInputFieldRepo"] = None,
    ):
        self._repo = repo
        self._input_field_repo = input_field_repo

    # ── Validate (dry-run, no DB write) ──────────────────────────────────────

    async def validate(self, raw_yaml: str) -> NormalizeResult:
        """Dry-run validate YAML. Không đụng DB."""
        return normalize_yaml(raw_yaml)

    # ── Create ───────────────────────────────────────────────────────────────

    async def create(
        self,
        raw_yaml: str,
        user: AuthenticatedUser,
        display_name_override: Optional[str] = None,
    ) -> ScenarioDefinition:
        """Tạo scenario mới từ YAML. Revision 1 được tạo cùng.

        Raises:
            ScenarioBadRequest: YAML parse lỗi
            ScenarioConflict: id đã tồn tại
            QuotaExceeded: user vượt quota
        """
        # Quota check
        count = await self._repo.count_by_owner(user.user_id)
        if count >= MAX_SCENARIOS_PER_USER:
            raise QuotaExceeded(
                f"User {user.user_id} đã có {count} scenarios, "
                f"max = {MAX_SCENARIOS_PER_USER}"
            )

        # Parse + validate YAML trước (để có display_name)
        result = normalize_yaml(raw_yaml)
        if not result.parse_ok:
            raise ScenarioBadRequest(result.all_issues)

        # Lấy display_name: ưu tiên override, sau đó từ spec (nếu parse OK),
        # fallback 'Untitled'
        if display_name_override:
            display_name = display_name_override
        elif result.spec:
            display_name = result.spec.display_name
        else:
            display_name = "Untitled"

        # Gen id + re-normalize với force_id (đảm bảo normalized_json khớp DB id)
        scenario_id = _user_scenario_id(user.user_id, display_name)
        result = normalize_yaml(raw_yaml, force_id=scenario_id)

        # Check id conflict
        existing = await self._repo.get_definition(scenario_id)
        if existing is not None:
            raise ScenarioConflict(
                "ID_CONFLICT",
                f"Scenario '{scenario_id}' đã tồn tại. "
                f"Đổi display_name để sinh id khác.",
            )

        now = _now()
        defn = ScenarioDefinition(
            id=scenario_id,
            name=display_name,
            owner_id=user.user_id,
            source_type="user",
            visibility="private",
            created_at=now,
            updated_at=now,
        )
        await self._repo.create_definition(defn)

        # Tạo revision 1
        await self._append_revision_from_result(
            scenario_id=scenario_id,
            result=result,
            raw_yaml=raw_yaml,
            user=user,
            parent_revision_id=None,
            clone_source_revision_id=None,
        )

        return defn

    # ── Update (= append revision) ───────────────────────────────────────────

    async def update(
        self,
        scenario_id: str,
        raw_yaml: str,
        user: AuthenticatedUser,
    ) -> ScenarioRevision:
        """Update = tạo revision mới. Check owner + hash collision.

        Raises:
            ScenarioNotFound, ScenarioForbidden, ScenarioBadRequest, ScenarioConflict
        """
        defn = await self._require_definition(scenario_id)
        self._require_writable(defn, user)

        result = normalize_yaml(raw_yaml, force_id=scenario_id)
        if not result.parse_ok:
            raise ScenarioBadRequest(result.all_issues)

        # Check no-op save
        latest = await self._repo.get_latest_revision(scenario_id)
        if latest and latest.yaml_hash == result.yaml_hash:
            raise ScenarioConflict(
                "NO_CHANGE",
                "YAML không thay đổi so với revision mới nhất. Không tạo revision.",
            )

        return await self._append_revision_from_result(
            scenario_id=scenario_id,
            result=result,
            raw_yaml=raw_yaml,
            user=user,
            parent_revision_id=latest.id if latest else None,
            clone_source_revision_id=None,
        )

    # ── Clone ────────────────────────────────────────────────────────────────

    async def clone(
        self,
        from_scenario_id: str,
        user: AuthenticatedUser,
        new_display_name: Optional[str] = None,
    ) -> ScenarioDefinition:
        """Clone từ scenario khác. Lấy published (hoặc latest) làm rev 1 của clone.

        Set clone_source_revision_id trỏ về rev gốc; parent_revision_id = None
        (rev 1 của clone không có parent trong scenario mới).

        Raises:
            ScenarioNotFound nếu source không tồn tại hoặc không visible.
        """
        source_defn = await self._repo.get_definition(from_scenario_id)
        if source_defn is None or source_defn.is_archived:
            raise ScenarioNotFound(f"Scenario '{from_scenario_id}' không tồn tại")

        # Visibility check: builtin/public OK; user scenario phải owner
        if source_defn.source_type == "user" and source_defn.owner_id != user.user_id and not user.is_admin:
            raise ScenarioForbidden("Không có quyền clone scenario private của user khác")

        # Lấy rev nguồn: ưu tiên published, fallback latest
        source_rev = await self._repo.get_published_revision(from_scenario_id)
        if source_rev is None:
            source_rev = await self._repo.get_latest_revision(from_scenario_id)
        if source_rev is None:
            raise ScenarioNotFound(
                f"Scenario '{from_scenario_id}' chưa có revision nào"
            )

        # Quota check
        count = await self._repo.count_by_owner(user.user_id)
        if count >= MAX_SCENARIOS_PER_USER:
            raise QuotaExceeded(
                f"User {user.user_id} đã có {count} scenarios"
            )

        # Display name mới
        display_name = new_display_name or f"{source_defn.name} (copy)"
        new_id = _user_scenario_id(user.user_id, display_name)

        if await self._repo.get_definition(new_id) is not None:
            raise ScenarioConflict("ID_CONFLICT", f"Scenario '{new_id}' đã tồn tại")

        # Re-normalize source YAML với id mới
        result = normalize_yaml(source_rev.raw_yaml, force_id=new_id)
        # Nếu builtin có trusted hooks, re-validate với source='user' có thể fail.
        # Accept cloned revision có thể failed — user sẽ sửa.

        now = _now()
        defn = ScenarioDefinition(
            id=new_id,
            name=display_name,
            owner_id=user.user_id,
            source_type="cloned",
            visibility="private",
            created_at=now,
            updated_at=now,
        )
        await self._repo.create_definition(defn)

        # Tạo rev 1, skip auto-sync inputs vì sẽ clone fields metadata từ source
        new_rev = await self._append_revision_from_result(
            scenario_id=new_id,
            result=result,
            raw_yaml=source_rev.raw_yaml,
            user=user,
            parent_revision_id=None,                        # rev 1 của clone
            clone_source_revision_id=source_rev.id,         # trỏ về rev gốc
            skip_input_sync=True,
        )

        # Phase 1 Input Fields: copy fields metadata từ source rev (display_label,
        # validation_rules, category, secret_ref, extraction_hint, ... — KHÔNG có
        # trong YAML, chỉ có trong DB). Fallback parse YAML nếu source rev chưa
        # có DB fields (vd source là rev cũ pre-Phase1).
        if self._input_field_repo and new_rev.id:
            from store.mysql_scenario_repo import MysqlScenarioRepo
            import aiomysql
            if isinstance(self._repo, MysqlScenarioRepo):
                try:
                    pool = self._repo._get_pool()
                    async with pool.acquire() as conn:
                        async with conn.cursor(aiomysql.DictCursor) as cur:
                            new_scenario_pk = await self._repo._resolve_scenario_pk(
                                cur, new_id
                            )
                    copied = await self._input_field_repo.clone_fields_to_revision(
                        src_revision_id=source_rev.id,
                        dst_revision_id=new_rev.id,
                        dst_scenario_id=new_scenario_pk,
                    )
                    if copied == 0:
                        # Source ko có DB fields → fallback import từ YAML
                        await self._sync_inputs_from_yaml(
                            new_rev.id, new_id, source_rev.raw_yaml
                        )
                except Exception as e:
                    _log.error(
                        "Failed clone input fields rev %d → %d: %s",
                        source_rev.id, new_rev.id, e, exc_info=True,
                    )

        return defn

    # ── Publish revision (admin) ─────────────────────────────────────────────

    async def publish_revision(
        self,
        scenario_id: str,
        revision_id: int,
        user: AuthenticatedUser,
    ) -> ScenarioDefinition:
        """Set published_revision_id trên scenario_definitions.

        Admin-only. Caller responsibility: sync Redis cache với spec mới
        (route handler làm sau khi service trả OK).

        Raises:
            ScenarioForbidden: non-admin
            ScenarioNotFound: scenario hoặc revision không tồn tại
            ScenarioBadRequest: revision không thuộc scenario này
        """
        if not user.is_admin:
            raise ScenarioForbidden("Chỉ admin được activate revision")

        defn = await self._require_definition(scenario_id)
        rev = await self._repo.get_revision(revision_id)
        if rev is None:
            raise ScenarioNotFound(f"Revision {revision_id} không tồn tại")
        if rev.scenario_id != scenario_id:
            raise ScenarioBadRequest(
                [{"field": "revision_id",
                  "message": f"Revision {revision_id} không thuộc scenario {scenario_id}"}]
            )

        await self._repo.set_published_revision(scenario_id, revision_id)
        defn.published_revision_id = revision_id
        return defn

    # ── Archive ──────────────────────────────────────────────────────────────

    async def archive(self, scenario_id: str, user: AuthenticatedUser) -> None:
        """Soft delete. Builtin không archive được qua API."""
        defn = await self._require_definition(scenario_id)
        if defn.source_type == "builtin":
            raise ScenarioForbidden(
                "Builtin scenario không xoá được qua API. "
                "Admin sửa qua SQL nếu cần."
            )
        self._require_writable(defn, user)
        await self._repo.archive_definition(scenario_id)

    # ── List ─────────────────────────────────────────────────────────────────

    async def list_for_user(
        self,
        user: AuthenticatedUser,
        source_type: Optional[str] = None,
        is_archived: Optional[bool] = False,
        limit: int = 100,
    ) -> list[ScenarioDefinition]:
        """Return scenarios user có thể thấy.

        Permission rules:
        - Builtin (enabled): ai cũng thấy
        - User-owned: chỉ owner
        - Admin: thấy tất
        """
        # Phase 1 đơn giản: list 2 loại rồi merge.
        # Admin → list tất không filter owner.
        if user.is_admin:
            return await self._repo.list_definitions(
                DefinitionFilters(
                    source_type=source_type,
                    is_archived=is_archived,
                    limit=limit,
                )
            )

        # Non-admin: builtin + own
        results: dict[str, ScenarioDefinition] = {}

        if source_type in (None, "builtin"):
            builtins = await self._repo.list_definitions(
                DefinitionFilters(
                    source_type="builtin",
                    is_archived=is_archived,
                    limit=limit,
                )
            )
            for d in builtins:
                results[d.id] = d

        if source_type in (None, "user", "cloned"):
            for st in ("user", "cloned"):
                if source_type and source_type != st:
                    continue
                owned = await self._repo.list_definitions(
                    DefinitionFilters(
                        owner_id=user.user_id,
                        source_type=st,
                        is_archived=is_archived,
                        limit=limit,
                    )
                )
                for d in owned:
                    results[d.id] = d

        # Sort updated_at DESC
        return sorted(results.values(), key=lambda d: d.updated_at, reverse=True)[:limit]

    # ── Get detail ───────────────────────────────────────────────────────────

    async def get_detail(
        self, scenario_id: str, user: AuthenticatedUser
    ) -> ScenarioDetail:
        """Full metadata + latest + published revision. Check read permission."""
        defn = await self._require_definition(scenario_id)
        self._require_readable(defn, user)

        latest = await self._repo.get_latest_revision(scenario_id)
        published = await self._repo.get_published_revision(scenario_id)
        return ScenarioDetail(defn, latest, published)

    async def list_revisions(
        self,
        scenario_id: str,
        user: AuthenticatedUser,
        limit: int = 20,
        before_id: Optional[int] = None,
    ) -> list[ScenarioRevision]:
        defn = await self._require_definition(scenario_id)
        self._require_readable(defn, user)
        return await self._repo.list_revisions(scenario_id, limit, before_id)

    async def get_revision(
        self, scenario_id: str, rev_id: int, user: AuthenticatedUser
    ) -> ScenarioRevision:
        defn = await self._require_definition(scenario_id)
        self._require_readable(defn, user)
        rev = await self._repo.get_revision(rev_id)
        if rev is None or rev.scenario_id != scenario_id:
            raise ScenarioNotFound(f"Revision {rev_id} không thuộc scenario {scenario_id}")
        return rev

    # ── Internal helpers ─────────────────────────────────────────────────────

    async def _require_definition(self, scenario_id: str) -> ScenarioDefinition:
        defn = await self._repo.get_definition(scenario_id)
        if defn is None:
            raise ScenarioNotFound(f"Scenario '{scenario_id}' không tồn tại")
        return defn

    def _require_readable(
        self, defn: ScenarioDefinition, user: AuthenticatedUser
    ) -> None:
        if user.is_admin:
            return
        if defn.source_type == "builtin":
            return
        if defn.visibility == "public":
            return
        if defn.owner_id == user.user_id:
            return
        raise ScenarioForbidden(
            f"User '{user.user_id}' không có quyền xem scenario '{defn.id}'"
        )

    def _require_writable(
        self, defn: ScenarioDefinition, user: AuthenticatedUser
    ) -> None:
        if user.is_admin:
            return
        if defn.source_type == "builtin":
            raise ScenarioForbidden("Builtin không sửa được qua API")
        if defn.owner_id != user.user_id:
            raise ScenarioForbidden(
                f"User '{user.user_id}' không phải owner của '{defn.id}'"
            )

    async def _append_revision_from_result(
        self,
        scenario_id: str,
        result: NormalizeResult,
        raw_yaml: str,
        user: AuthenticatedUser,
        parent_revision_id: Optional[int],
        clone_source_revision_id: Optional[int],
        skip_input_sync: bool = False,
    ) -> ScenarioRevision:
        """Tạo ScenarioRevision từ NormalizeResult và insert.

        Nếu validation fail → vẫn lưu với status='failed' + errors.

        Phase 1 Input Fields: sau khi insert rev, nếu có `input_field_repo`,
        parse YAML inputs[] và backfill vào scenario_input_fields. Skip bằng
        skip_input_sync=True (vd clone flow sẽ clone fields metadata sau).
        """
        status = "passed" if result.validation_ok else "failed"
        # Lưu normalized nếu có, còn không lưu raw_yaml data (không fail hẳn)
        normalized = result.normalized_json or {}
        errors_json = result.all_issues if result.all_issues else None

        rev = ScenarioRevision(
            scenario_id=scenario_id,
            version_no=0,   # repo sẽ tự set
            raw_yaml=raw_yaml,
            normalized_spec_json=normalized,
            yaml_hash=result.yaml_hash,
            parent_revision_id=parent_revision_id,
            clone_source_revision_id=clone_source_revision_id,
            schema_version=1,
            static_validation_status=status,
            static_validation_errors=errors_json,
            created_by=user.user_id,
            created_at=_now(),
        )
        new_id = await self._repo.append_revision(rev)
        # Fetch lại để có version_no chính xác do repo tự assign
        persisted = await self._repo.get_revision(new_id)
        if persisted is None:
            # Shouldn't happen — defensive
            raise RuntimeError(f"Revision {new_id} insert OK nhưng không fetch được")

        # Phase 1 Input Fields: auto-sync inputs từ YAML vào DB
        if self._input_field_repo and not skip_input_sync:
            await self._sync_inputs_from_yaml(new_id, scenario_id, raw_yaml)

        return persisted

    async def _sync_inputs_from_yaml(
        self,
        rev_id: int,
        scenario_code: str,
        raw_yaml: str,
    ) -> int:
        """Parse YAML inputs[] và INSERT vào scenario_input_fields.

        Idempotent: skip nếu rev đã có fields (vd backfill script đã chạy).
        Tolerant: lỗi 1 field không break cả rev (chỉ log warning).

        Return: số fields đã insert.
        """
        if self._input_field_repo is None:
            return 0

        existing = await self._input_field_repo.count_by_revision(rev_id)
        if existing > 0:
            _log.info(
                "Skip input sync for rev_id=%d: already has %d fields",
                rev_id, existing,
            )
            return 0

        # Resolve scenario_pk (BIGINT) qua MysqlScenarioRepo internal helper.
        # Helper signature: _resolve_scenario_pk(cur, code) — cần acquire cursor.
        from store.mysql_scenario_repo import MysqlScenarioRepo
        import aiomysql
        if not isinstance(self._repo, MysqlScenarioRepo):
            _log.warning(
                "Skip input sync: scenario_repo không phải MySQL backend"
            )
            return 0
        try:
            pool = self._repo._get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    scenario_pk = await self._repo._resolve_scenario_pk(
                        cur, scenario_code
                    )
        except Exception as e:
            _log.warning(
                "Failed resolve scenario_pk for '%s': %s",
                scenario_code, e,
            )
            return 0

        # Lazy import để tránh circular dep
        from services.yaml_sync import YamlSyncService

        try:
            yaml_sync = YamlSyncService(self._repo, self._input_field_repo)
            inserted = await yaml_sync.import_inputs_from_yaml(
                rev_id=rev_id,
                scenario_id=scenario_pk,
                raw_yaml=raw_yaml,
            )
            return inserted
        except Exception as e:
            _log.error(
                "Failed sync inputs for rev_id=%d: %s",
                rev_id, e, exc_info=True,
            )
            return 0
