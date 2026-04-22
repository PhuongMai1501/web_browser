"""
api/routes/scenarios.py — Admin REST API cho scenario.

Auth: header `X-Admin-Token` phải khớp env `ADMIN_TOKEN`. Nếu env không set
→ toàn bộ admin route trả 503 (fail closed) để tránh lỡ expose ra public.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from services import scenario_service
from services.scenario_service import (
    ScenarioValidationError,
    validate_spec,
)
from store.redis_client import get_async_redis

# Resolve ScenarioSpec qua service module (đã xử lý sys.path LLM_base)
from scenarios.spec import ScenarioSpec  # noqa: E402


router = APIRouter(prefix="/v1/scenarios", tags=["scenarios"])


def _require_admin(x_admin_token: Optional[str]) -> None:
    expected = os.environ.get("ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(
            503,
            detail="ADMIN_TOKEN chưa set trên server — admin API đang vô hiệu hoá.",
        )
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(401, detail="X-Admin-Token không hợp lệ")


# ── Response shapes ───────────────────────────────────────────────────────────

class ScenarioSummary(BaseModel):
    id: str
    display_name: str
    enabled: bool
    builtin: bool
    version: int


# ── Routes ────────────────────────────────────────────────────────────────────

# GET endpoints public — UI frontend cần đọc list + inputs để render form động
# mà không lộ admin token. CRUD (POST/PUT/DELETE) vẫn yêu cầu X-Admin-Token.

@router.get("", response_model=list[ScenarioSummary])
async def list_scenarios():
    redis = get_async_redis()
    specs = await scenario_service.list_async(redis)
    return [
        ScenarioSummary(
            id=s.id, display_name=s.display_name,
            enabled=s.enabled, builtin=s.builtin, version=s.version,
        )
        for s in specs
        if s.enabled   # không expose scenario đang disable cho UI
    ]


@router.get("/{scenario_id}", response_model=ScenarioSpec)
async def get_scenario(scenario_id: str):
    redis = get_async_redis()
    spec = await scenario_service.get_async(redis, scenario_id)
    if spec is None:
        raise HTTPException(404, detail=f"Scenario '{scenario_id}' không tồn tại")
    return spec


@router.post("", response_model=ScenarioSpec, status_code=201)
async def create_scenario(
    spec: ScenarioSpec,
    x_admin_token: Optional[str] = Header(default=None),
):
    _require_admin(x_admin_token)
    redis = get_async_redis()

    existing = await scenario_service.get_async(redis, spec.id)
    if existing is not None:
        raise HTTPException(409, detail=f"Scenario '{spec.id}' đã tồn tại. Dùng PUT để update.")

    # User không được tự đặt builtin=True qua API
    spec.builtin = False
    spec.version = 1
    try:
        validate_spec(spec)
    except ScenarioValidationError as e:
        raise HTTPException(422, detail=str(e))
    await scenario_service.save_async(redis, spec)
    return spec


@router.put("/{scenario_id}", response_model=ScenarioSpec)
async def update_scenario(
    scenario_id: str,
    spec: ScenarioSpec,
    x_admin_token: Optional[str] = Header(default=None),
):
    _require_admin(x_admin_token)
    if spec.id != scenario_id:
        raise HTTPException(400, detail=f"Body.id ({spec.id}) ≠ path id ({scenario_id})")

    redis = get_async_redis()
    existing = await scenario_service.get_async(redis, scenario_id)
    if existing is None:
        raise HTTPException(404, detail=f"Scenario '{scenario_id}' không tồn tại")

    # Giữ nguyên cờ builtin (user không tự sửa), bump version
    spec.builtin = existing.builtin
    spec.version = existing.version + 1
    try:
        validate_spec(spec)
    except ScenarioValidationError as e:
        raise HTTPException(422, detail=str(e))
    await scenario_service.save_async(redis, spec)
    return spec


@router.delete("/{scenario_id}", status_code=204)
async def delete_scenario(scenario_id: str, x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    redis = get_async_redis()
    existing = await scenario_service.get_async(redis, scenario_id)
    if existing is None:
        raise HTTPException(404, detail=f"Scenario '{scenario_id}' không tồn tại")
    if existing.builtin:
        raise HTTPException(
            409,
            detail=f"Scenario '{scenario_id}' là builtin, không thể delete. "
                   f"Dùng PUT với enabled=false để disable.",
        )
    await scenario_service.delete_async(redis, scenario_id)


@router.post("/{scenario_id}/dry-run")
async def dry_run_scenario(
    scenario_id: str,
    spec: ScenarioSpec,
    x_admin_token: Optional[str] = Header(default=None),
):
    """Validate spec (schema, hook names) mà không lưu."""
    _require_admin(x_admin_token)
    if spec.id != scenario_id:
        raise HTTPException(400, detail="Body.id ≠ path id")
    try:
        validate_spec(spec)
    except ScenarioValidationError as e:
        raise HTTPException(422, detail=str(e))
    return {"status": "valid", "id": spec.id, "hooks": spec.hooks.model_dump()}
