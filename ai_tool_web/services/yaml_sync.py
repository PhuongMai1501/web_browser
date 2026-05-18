"""
services/yaml_sync.py — Sync giữa scenario_input_fields (DB) và YAML
(scenario_revisions.raw_yaml + normalized_spec_json).

Phase 1 (2026-05-11): DB là source of truth cho inputs[]. UI form mutate DB,
service này regenerate YAML inputs block với markers AUTO-GENERATED, ghép với
phần user-editable (steps/hooks) của YAML cũ → update revision.

Hybrid revision model:
- DRAFT revision (rev.id != scenario_definitions.published_revision_id):
  mutable, yaml_sync update in-place.
- PUBLISHED revision (rev.id == published_revision_id): immutable, yaml_sync
  từ chối (raise YamlSyncForbidden). Caller phải clone rev mới.

Public API:
- regenerate_revision_yaml(rev_id) → update raw_yaml + normalized_spec_json
- import_inputs_from_yaml(rev_id, scenario_id, raw_yaml) → backfill khi tạo
  revision mới từ YAML user upload
- check_revision_is_draft(rev_id) → bool, dùng để guard mutate endpoints

Markers:
    # ╔══════════ AUTO-GENERATED — DO NOT EDIT MANUALLY ══════════╗
    # ║  Sync from DB: scenario_input_fields (revision_id=42)     ║
    # ║  Edit via: Inputs tab → Add/Edit Field                    ║
    # ╚══════════════════════════════════════════════════════════╝
    inputs:
      - name: ...
        ...
    # ╔══════════ END AUTO-GENERATED ══════════════════════════════╗
    # ╚══════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Optional

import yaml as pyyaml

from store.mysql_scenario_input_field_repo import (
    MysqlScenarioInputFieldRepo,
    ScenarioInputField,
)
from store.scenario_repo import ScenarioRepository

_log = logging.getLogger(__name__)


# ── Markers ──────────────────────────────────────────────────────────────────

AUTO_GEN_START = "# ╔══════════ AUTO-GENERATED — DO NOT EDIT MANUALLY ══════════╗"
AUTO_GEN_END = "# ╚══════════ END AUTO-GENERATED ══════════════════════════════╝"

# Regex pattern matching toàn bộ block markers + nội dung giữa (inclusive)
# DOTALL để . match newline. Non-greedy *? để không vượt block.
_AUTO_GEN_BLOCK_RE = re.compile(
    re.escape(AUTO_GEN_START)
    + r".*?"
    + re.escape(AUTO_GEN_END),
    re.DOTALL,
)


# ── Exceptions ───────────────────────────────────────────────────────────────

class YamlSyncError(Exception):
    """Base exception cho yaml_sync."""


class YamlSyncForbidden(YamlSyncError):
    """Cố sync trên PUBLISHED revision (immutable)."""


class YamlSyncNotFound(YamlSyncError):
    """Revision hoặc scenario không tồn tại."""


# ── Service ──────────────────────────────────────────────────────────────────

class YamlSyncService:
    """Sync logic giữa DB fields và YAML raw_yaml.

    Dependencies:
    - scenario_repo: read scenario_definitions.published_revision_id,
      get_revision, update_revision_yaml
    - input_field_repo: list_fields_by_revision

    KHÔNG cần Redis ở đây — caller (route handler) gọi
    scenario_service.save_async() sau khi yaml_sync xong để invalidate cache.
    """

    def __init__(
        self,
        scenario_repo: ScenarioRepository,
        input_field_repo: MysqlScenarioInputFieldRepo,
    ) -> None:
        self._scenario_repo = scenario_repo
        self._input_field_repo = input_field_repo

    # ── Public: regenerate sau khi DB fields mutate ──────────────────────────

    async def regenerate_revision_yaml(self, rev_id: int) -> None:
        """Đọc DB fields → rebuild raw_yaml + normalized_spec_json → update revision.

        Gọi sau MỖI lần mutate input fields qua API (create/update/delete/reorder/bulk).

        Raise:
            YamlSyncNotFound: revision không tồn tại
            YamlSyncForbidden: revision đã published
        """
        rev = await self._scenario_repo.get_revision(rev_id)
        if rev is None:
            raise YamlSyncNotFound(f"Revision {rev_id} không tồn tại")

        # Hybrid model: chỉ cho update DRAFT
        is_draft = await self.check_revision_is_draft(rev_id, rev.scenario_id)
        if not is_draft:
            raise YamlSyncForbidden(
                f"Revision {rev_id} đã PUBLISHED, không sửa được. "
                f"Tạo revision mới để chỉnh sửa inputs."
            )

        # Load DB fields
        fields = await self._input_field_repo.list_fields_by_revision(rev_id)

        # Rebuild raw_yaml = auto-gen block + user-editable section
        new_raw_yaml = self._build_raw_yaml(
            old_raw_yaml=rev.raw_yaml,
            rev_id=rev_id,
            fields=fields,
        )

        # Rebuild normalized_spec_json — replace inputs[] key
        new_normalized = dict(rev.normalized_spec_json)
        new_normalized["inputs"] = [self._field_to_spec_input(f) for f in fields]

        # New yaml_hash
        new_hash = hashlib.sha256(new_raw_yaml.encode("utf-8")).hexdigest()

        await self._scenario_repo.update_revision_yaml(
            rev_id=rev_id,
            raw_yaml=new_raw_yaml,
            normalized_spec_json=new_normalized,
            yaml_hash=new_hash,
        )

        _log.info(
            "Regenerated YAML for rev_id=%d: %d fields, hash=%s",
            rev_id, len(fields), new_hash[:12],
        )

    # ── Public: backfill khi tạo revision mới ────────────────────────────────

    async def import_inputs_from_yaml(
        self,
        rev_id: int,
        scenario_id: int,
        raw_yaml: str,
    ) -> int:
        """Parse YAML inputs[] → INSERT vào scenario_input_fields.

        Gọi khi tạo revision mới (qua POST /v1/user-scenarios hoặc PUT update).
        Idempotent: skip nếu rev_id đã có fields.

        Return: số fields đã insert. 0 nếu rev đã có fields hoặc YAML không
        có inputs[].
        """
        existing_count = await self._input_field_repo.count_by_revision(rev_id)
        if existing_count > 0:
            _log.info(
                "Skip import for rev_id=%d: already has %d fields",
                rev_id, existing_count,
            )
            return 0

        inputs = self._parse_yaml_inputs(raw_yaml)
        if not inputs:
            return 0

        inserted = 0
        for order, inp in enumerate(inputs):
            field = self._spec_input_to_field(inp, rev_id, scenario_id, order)
            try:
                await self._input_field_repo.create_field(field)
                inserted += 1
            except ValueError as e:
                _log.warning(
                    "Skip duplicate field name=%s in rev_id=%d: %s",
                    inp.get("name"), rev_id, e,
                )

        _log.info(
            "Imported %d fields for rev_id=%d from YAML inputs[]",
            inserted, rev_id,
        )
        return inserted

    # ── Public: check DRAFT status ───────────────────────────────────────────

    async def check_revision_is_draft(
        self, rev_id: int, scenario_id: str
    ) -> bool:
        """Return True nếu revision là DRAFT (chưa được publish).

        Definition: DRAFT = rev.id != scenario_definitions.published_revision_id.

        Note: Nếu scenario chưa publish gì cả (published_revision_id IS NULL),
        TẤT CẢ revisions của scenario đó đều là DRAFT.
        """
        defn = await self._scenario_repo.get_definition(scenario_id)
        if defn is None:
            raise YamlSyncNotFound(
                f"Scenario '{scenario_id}' không tồn tại"
            )
        return defn.published_revision_id != rev_id

    # ── Internal: YAML build/parse ───────────────────────────────────────────

    @classmethod
    def _build_raw_yaml(
        cls,
        old_raw_yaml: str,
        rev_id: int,
        fields: list[ScenarioInputField],
    ) -> str:
        """Build raw_yaml mới:
          1. Strip auto-gen block (nếu có) hoặc top-level `inputs:` (legacy)
          2. Prepend auto-gen block mới với inputs từ DB

        Vùng user-editable (allowed_domains, steps, success, hooks, ...) giữ
        nguyên.
        """
        user_section = cls._strip_auto_gen_and_inputs_block(old_raw_yaml)

        # Build inputs YAML từ DB
        inputs_list = [cls._field_to_spec_input(f) for f in fields]
        if not inputs_list:
            # Không có field nào — vẫn emit block trống để UI biết section
            # được managed
            inputs_yaml = "inputs: []\n"
        else:
            inputs_yaml = pyyaml.dump(
                {"inputs": inputs_list},
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
                width=120,
            )

        auto_gen_block = (
            f"{AUTO_GEN_START}\n"
            f"# ║  Sync from DB: scenario_input_fields (revision_id={rev_id})\n"
            f"# ║  Edit via: Inputs tab → Add/Edit Field\n"
            f"{inputs_yaml}"
            f"{AUTO_GEN_END}\n"
        )

        # Đảm bảo có newline giữa auto-gen và user section
        if user_section and not user_section.startswith("\n"):
            user_section = "\n" + user_section

        return auto_gen_block + user_section

    @classmethod
    def _strip_auto_gen_and_inputs_block(cls, raw_yaml: str) -> str:
        """Strip:
          1. Vùng giữa AUTO_GEN_START và AUTO_GEN_END (nếu có)
          2. Top-level `inputs:` key (nếu YAML legacy chưa có markers)

        Trả về phần còn lại (header comment + allowed_domains + steps + ...).
        """
        # Pattern 1: có markers (post-Phase 1 YAML)
        stripped = _AUTO_GEN_BLOCK_RE.sub("", raw_yaml)
        if stripped != raw_yaml:
            # Đã strip markers — clean leading blank lines
            return stripped.lstrip("\n")

        # Pattern 2: YAML legacy có top-level `inputs:` — strip qua parse + rebuild
        try:
            parsed = pyyaml.safe_load(raw_yaml)
        except pyyaml.YAMLError:
            _log.warning(
                "Failed parse YAML để strip inputs (legacy), giữ nguyên"
            )
            return raw_yaml

        if isinstance(parsed, dict) and "inputs" in parsed:
            new_parsed = {k: v for k, v in parsed.items() if k != "inputs"}
            return pyyaml.dump(
                new_parsed,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
                width=120,
            )

        # YAML không có inputs section, giữ nguyên
        return raw_yaml

    @staticmethod
    def _parse_yaml_inputs(raw_yaml: str) -> list[dict]:
        """Parse raw_yaml → trả về list inputs[]. Tolerant: trả [] nếu parse fail."""
        if not raw_yaml:
            return []
        try:
            spec = pyyaml.safe_load(raw_yaml)
        except pyyaml.YAMLError as e:
            _log.warning("Failed parse YAML inputs: %s", e)
            return []
        if not isinstance(spec, dict):
            return []
        inputs = spec.get("inputs") or []
        if not isinstance(inputs, list):
            return []
        return [i for i in inputs if isinstance(i, dict) and i.get("name")]

    # ── Internal: convert DB ↔ spec format ───────────────────────────────────

    @staticmethod
    def _field_to_spec_input(field: ScenarioInputField) -> dict:
        """ScenarioInputField (DB row) → dict cho YAML inputs[] / normalized_spec_json.

        Chỉ emit các field thuộc Pydantic ScenarioInputField (spec.py):
        name, type, required, source, default, description.

        Phase 2 fields (category, secret_ref, extraction_hint) KHÔNG emit vào
        YAML (không nằm trong Pydantic spec). Phase 2 mở rộng spec → expose sau.

        Security: KHÔNG emit `default` cho field_type='secret' (vi phạm
        validation `_check_security_secrets` trong yaml_normalizer). Default
        cho secret = NULL (user phải nhập runtime, không hardcode trong YAML/DB).
        """
        out: dict = {
            "name": field.name,
            "type": field.field_type,
            "required": field.is_required,
            "source": field.source,
        }
        # Bỏ default cho secret — security rule
        if field.default_value is not None and field.field_type != "secret":
            # Cast theo field_type
            cast_value = YamlSyncService._cast_default(
                field.default_value, field.field_type
            )
            if cast_value is not None:
                out["default"] = cast_value
        if field.description:
            out["description"] = field.description
        return out

    @staticmethod
    def _spec_input_to_field(
        inp: dict,
        rev_id: int,
        scenario_id: int,
        order: int,
    ) -> ScenarioInputField:
        """dict input từ YAML spec → ScenarioInputField cho DB insert.

        Reverse của _field_to_spec_input. Dùng cho import_inputs_from_yaml.
        Các field UI/Phase2 chưa có trong spec → default value.
        """
        name = inp.get("name") or ""
        if not name:
            raise ValueError(f"Input thiếu field 'name': {inp}")

        raw_default = inp.get("default")
        if raw_default is None:
            default_value = None
        elif isinstance(raw_default, bool):
            default_value = "true" if raw_default else "false"
        else:
            default_value = str(raw_default)

        return ScenarioInputField(
            revision_id=rev_id,
            scenario_id=scenario_id,
            name=name,
            display_label=name.replace("_", " ").title(),  # heuristic
            field_type=inp.get("type") or "string",
            is_required=bool(inp.get("required", False)),
            source=inp.get("source") or "context",
            default_value=default_value,
            description=inp.get("description") or None,
            validation_rules=None,
            placeholder=None,
            help_text=None,
            display_order=order,
            category="user_input",
            secret_ref=None,
            extraction_hint=None,
            template_id=None,
        )

    @staticmethod
    def _cast_default(value: str, field_type: str):
        """Cast default_value string từ DB → typed value cho YAML emit.

        Return None nếu cast fail (omit field).
        """
        if value is None or value == "":
            return None
        try:
            if field_type == "number":
                # Thử int trước, fallback float
                if "." in value or "e" in value.lower():
                    return float(value)
                return int(value)
            if field_type == "bool":
                return value.lower() in ("true", "1", "yes", "y")
            # string, secret → giữ nguyên
            return value
        except (ValueError, AttributeError):
            _log.warning(
                "Cast default %r → %s fail, omitting from YAML",
                value, field_type,
            )
            return None
