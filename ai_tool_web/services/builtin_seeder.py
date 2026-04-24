"""
services/builtin_seeder.py — Auto-seed builtin scenarios vào SQLite khi DB rỗng (G2).

Đọc tất cả `LLM_base/scenarios/builtin/*.yaml` → create ScenarioDefinition
(source_type='builtin', owner_id=None) + revision 1 cho mỗi file.

Builtin scenarios luôn đã published (set published_revision_id ngay lúc seed).

Gọi từ api/app.py startup hook:
    if await repo.count_builtin() == 0:
        await seed_builtin_from_yaml(repo)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.yaml_normalizer import normalize_yaml
from store.scenario_repo import ScenarioDefinition, ScenarioRepository, ScenarioRevision


_log = logging.getLogger(__name__)


def _default_builtin_dir() -> Path:
    # dev/deploy_server/ai_tool_web/services/builtin_seeder.py
    # → parent.parent.parent = dev/deploy_server
    return (
        Path(__file__).resolve().parent.parent.parent
        / "LLM_base" / "scenarios" / "builtin"
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def seed_builtin_from_yaml(
    repo: ScenarioRepository,
    directory: Optional[Path] = None,
    system_user_id: str = "system",
) -> int:
    """Seed builtin scenarios từ thư mục YAML. Idempotent — skip nếu id đã tồn tại.

    Args:
        repo: ScenarioRepository đã init.
        directory: override thư mục YAML; default `LLM_base/scenarios/builtin/`.
        system_user_id: value cho `created_by` của revision (không phải owner_id —
                        owner_id=None cho builtin).

    Returns:
        Số scenario mới tạo. 0 nếu tất cả đã tồn tại.
    """
    directory = directory or _default_builtin_dir()
    if not directory.exists():
        _log.warning("Builtin dir không tồn tại: %s", directory)
        return 0

    yaml_files = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
    if not yaml_files:
        _log.info("Không có file YAML trong %s", directory)
        return 0

    created = 0
    for path in yaml_files:
        try:
            n = await _seed_one(repo, path, system_user_id)
            created += n
        except Exception as e:
            _log.error("Seed fail cho %s: %s", path.name, e)
    if created:
        _log.info("Seeded %d builtin scenarios từ %s", created, directory)
    return created


async def _seed_one(
    repo: ScenarioRepository,
    path: Path,
    system_user_id: str,
) -> int:
    """Seed 1 file. Trả 1 nếu insert, 0 nếu đã tồn tại."""
    raw_yaml = path.read_text(encoding="utf-8")

    # Lấy id từ YAML (trước khi normalize)
    # normalize_yaml sẽ dùng id trong YAML vì không truyền force_id
    # nhưng force_builtin=True để skip hook whitelist check
    result = normalize_yaml(raw_yaml, force_builtin=True)

    if not result.parse_ok:
        _log.error("Parse fail %s: %s",
                   path.name,
                   [e.message for e in result.errors])
        return 0

    if not result.validation_ok or result.spec is None:
        _log.error("Validate fail %s: %s",
                   path.name,
                   [e.message for e in result.errors])
        return 0

    scenario_id = result.spec.id

    # Idempotent: skip nếu đã có
    existing = await repo.get_definition(scenario_id)
    if existing is not None:
        _log.debug("Builtin %s đã tồn tại, skip", scenario_id)
        return 0

    # Create definition
    now = _now()
    defn = ScenarioDefinition(
        id=scenario_id,
        name=result.spec.display_name,
        owner_id=None,
        source_type="builtin",
        visibility="public",
        created_at=now,
        updated_at=now,
    )
    await repo.create_definition(defn)

    # Append revision 1
    rev = ScenarioRevision(
        scenario_id=scenario_id,
        version_no=0,  # repo sẽ tự set
        raw_yaml=raw_yaml,
        normalized_spec_json=result.normalized_json,
        yaml_hash=result.yaml_hash,
        parent_revision_id=None,
        clone_source_revision_id=None,
        schema_version=1,
        static_validation_status="passed",
        static_validation_errors=None,
        created_by=system_user_id,
        created_at=now,
    )
    rev_id = await repo.append_revision(rev)

    # Builtin luôn published ngay
    await repo.set_published_revision(scenario_id, rev_id)

    _log.info("Seeded builtin: %s (rev=%d)", scenario_id, rev_id)
    return 1
