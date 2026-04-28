"""
services/scenario_service.py — CRUD scenario trong Redis + seed từ YAML builtin.

Redis layout:
  scenario:<id>     STRING (JSON ScenarioSpec)
  scenarios:index   SET các id

Store không TTL. Seed idempotent: chỉ SET nếu key chưa tồn tại, không đè
spec đã bị user chỉnh qua admin API.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Iterable, Optional

import redis as _sync_redis
import yaml
from redis.asyncio import Redis

# LLM_base không nằm trong PYTHONPATH khi API chạy (chỉ worker add) →
# tự append để `scenarios.*` import được cho cả admin route lẫn worker.
# APPEND (không insert 0!) để tránh `LLM_base/api.py` shadow package
# `ai_tool_web/api/` — cùng lý do browser_worker.py dùng append.
_LLM_BASE = Path(__file__).resolve().parent.parent.parent / "LLM_base"
if str(_LLM_BASE) not in sys.path:
    sys.path.append(str(_LLM_BASE))

from scenarios.spec import ScenarioSpec  # noqa: E402
from scenarios.hooks_registry import HOOK_REGISTRY  # noqa: E402
from scenarios.action_registry import ACTION_REGISTRY  # noqa: E402
# Trigger action registration để ACTION_REGISTRY đầy đủ trước khi validate
import scenarios.actions  # noqa: E402,F401


_log = logging.getLogger(__name__)

_SCENARIO_KEY = "scenario:{}"
_INDEX_KEY = "scenarios:index"


# ── Errors ─────────────────────────────────────────────────────────────────────

class ScenarioNotFoundError(KeyError):
    pass


class ScenarioValidationError(ValueError):
    """Raised when spec fails validation (bad hook name, bad schema, etc)."""


class ContextValidationError(ValueError):
    """Raised when request context doesn't match spec.context_schema."""


# ── Validation ─────────────────────────────────────────────────────────────────

def validate_spec(spec: ScenarioSpec) -> None:
    """Check hook names, action names, step refs. Gọi trước khi save."""
    # 1) Hooks
    for field_name in ("pre_check", "post_step", "final_capture"):
        name = getattr(spec.hooks, field_name)
        if name and name not in HOOK_REGISTRY:
            raise ScenarioValidationError(
                f"Hook '{name}' (hooks.{field_name}) chưa register. "
                f"Hook hợp lệ: {sorted(HOOK_REGISTRY)}"
            )

    # 2) mode=flow — cần ít nhất 1 step và action names phải hợp lệ
    if spec.mode == "flow":
        if not spec.steps:
            raise ScenarioValidationError(
                "mode='flow' yêu cầu ít nhất 1 step trong 'steps'"
            )
        input_names = {i.name for i in (spec.inputs or [])}
        _validate_steps(spec.steps, input_names, path="steps")

    # mode=agent không ép goal/start_url ở validation — custom scenario cố ý
    # để rỗng, nhận override từ request. Runner có fallback text mặc định.


def _validate_steps(steps, input_names: set[str], path: str) -> None:
    for idx, step in enumerate(steps or []):
        here = f"{path}[{idx}]"
        if step.action not in ACTION_REGISTRY:
            raise ScenarioValidationError(
                f"{here}: action '{step.action}' chưa register. "
                f"Action hợp lệ: {sorted(ACTION_REGISTRY)}"
            )
        # value_from phải reference tên field trong inputs (hoặc runtime-ask)
        if step.value_from and input_names and step.value_from not in input_names:
            # Chỉ warning-as-error nếu inputs được khai báo; nếu inputs rỗng
            # (back-compat) cho phép value_from tự do.
            raise ScenarioValidationError(
                f"{here}: value_from='{step.value_from}' không có trong inputs "
                f"({sorted(input_names)})"
            )
        # ask_user cần field + prompt
        if step.action == "ask_user":
            if not step.field:
                raise ScenarioValidationError(f"{here}: ask_user thiếu 'field'")
        # goto cần url
        if step.action == "goto" and not step.url:
            raise ScenarioValidationError(f"{here}: goto thiếu 'url'")
        # if_visible cần target + then
        if step.action == "if_visible":
            if step.target is None:
                raise ScenarioValidationError(f"{here}: if_visible thiếu 'target'")
            if not step.then and not step.else_:
                raise ScenarioValidationError(
                    f"{here}: if_visible cần 'then' hoặc 'else' không rỗng"
                )
            _validate_steps(step.then, input_names, f"{here}.then")
            _validate_steps(step.else_, input_names, f"{here}.else")


def validate_context(spec: ScenarioSpec, context: Optional[dict]) -> None:
    """Check request context thoả spec.inputs (ưu tiên v2) hoặc
    context_schema.required (back-compat v1)."""
    ctx = context or {}

    # v2: inputs với source=context + required=true
    if spec.inputs:
        missing = []
        for inp in spec.inputs:
            if inp.source != "context":
                continue  # ask_user runtime không yêu cầu ở request
            if inp.required and (inp.name not in ctx or ctx[inp.name] in (None, "")):
                missing.append(inp.name)
        if missing:
            raise ContextValidationError(
                f"Thiếu field context bắt buộc: {missing} (scenario={spec.id})"
            )
        return

    # v1 fallback
    schema = spec.context_schema or {}
    required = schema.get("required") or []
    missing = [k for k in required if k not in ctx or ctx[k] in (None, "")]
    if missing:
        raise ContextValidationError(
            f"Thiếu field context bắt buộc: {missing} (scenario={spec.id})"
        )


# ── Async (API) ────────────────────────────────────────────────────────────────

async def list_async(redis: Redis) -> list[ScenarioSpec]:
    ids = await redis.smembers(_INDEX_KEY)
    if not ids:
        return []
    pipe = redis.pipeline()
    for sid in ids:
        pipe.get(_SCENARIO_KEY.format(sid))
    raws = await pipe.execute()
    result = []
    for raw in raws:
        if raw:
            result.append(ScenarioSpec.model_validate_json(raw))
    result.sort(key=lambda s: (not s.builtin, s.id))
    return result


async def get_async(redis: Redis, scenario_id: str) -> Optional[ScenarioSpec]:
    raw = await redis.get(_SCENARIO_KEY.format(scenario_id))
    if not raw:
        return None
    return ScenarioSpec.model_validate_json(raw)


async def save_async(redis: Redis, spec: ScenarioSpec) -> None:
    validate_spec(spec)
    raw = spec.model_dump_json()
    pipe = redis.pipeline()
    pipe.set(_SCENARIO_KEY.format(spec.id), raw)
    pipe.sadd(_INDEX_KEY, spec.id)
    await pipe.execute()


async def delete_async(redis: Redis, scenario_id: str) -> bool:
    """Hard delete. Built-in bị chặn ở layer route (để còn khôi phục qua seed)."""
    pipe = redis.pipeline()
    pipe.delete(_SCENARIO_KEY.format(scenario_id))
    pipe.srem(_INDEX_KEY, scenario_id)
    deleted, _ = await pipe.execute()
    return bool(deleted)


# ── Sync (worker) ──────────────────────────────────────────────────────────────

def get_sync(sync_r: _sync_redis.Redis, scenario_id: str) -> Optional[ScenarioSpec]:
    raw = sync_r.get(_SCENARIO_KEY.format(scenario_id))
    if not raw:
        return None
    return ScenarioSpec.model_validate_json(raw)


# ── Seed from YAML ─────────────────────────────────────────────────────────────

def _builtin_dir() -> Path:
    # bundled scenarios/builtin (production: agent_browser/scenarios/builtin)
    # → fallback LLM_base/scenarios/builtin (dev legacy)
    here = Path(__file__).resolve()
    bundled = here.parent.parent / "scenarios" / "builtin"
    if bundled.exists():
        return bundled
    return here.parent.parent.parent / "LLM_base" / "scenarios" / "builtin"


def load_builtin_specs(directory: Optional[Path] = None) -> list[ScenarioSpec]:
    """Parse tất cả *.yaml trong builtin/ thành ScenarioSpec.
    Tự động đánh dấu builtin=True."""
    directory = directory or _builtin_dir()
    if not directory.exists():
        return []
    specs: list[ScenarioSpec] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            _log.error("Builtin scenario %s invalid YAML: %s", path.name, e)
            continue
        if not isinstance(data, dict):
            _log.error("Builtin scenario %s: root phải là mapping", path.name)
            continue
        data["builtin"] = True
        specs.append(ScenarioSpec.model_validate(data))
    return specs


async def seed_async(redis: Redis, specs: Optional[Iterable[ScenarioSpec]] = None) -> int:
    """Seed builtin specs nếu chưa có trong Redis. Trả về số spec đã tạo mới."""
    if specs is None:
        specs = load_builtin_specs()
    created = 0
    for spec in specs:
        exists = await redis.exists(_SCENARIO_KEY.format(spec.id))
        if exists:
            # Không đè spec đã có — admin có thể đã chỉnh qua API
            continue
        try:
            await save_async(redis, spec)
            created += 1
            _log.info("Seeded builtin scenario: %s", spec.id)
        except ScenarioValidationError as e:
            _log.error("Builtin scenario %s failed validation: %s", spec.id, e)
    return created
