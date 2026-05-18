"""
api/dependencies.py — FastAPI dependency providers.

3 depen chính:
- `get_repo(request)` → ScenarioRepository (từ app.state, init 1 lần lúc startup)
- `get_auth_provider(request)` → AuthProvider (từ app.state)
- `get_current_user(request, provider)` → AuthenticatedUser hoặc 401
- `get_scenario_service(repo)` → UserScenarioService per request

Wire vào app.py qua startup hook:
    app.state.scenario_repo = SqliteScenarioRepo(...)
    await app.state.scenario_repo.init()
    app.state.auth_provider = MockAuthProvider()
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request

from auth.providers import AuthenticatedUser, AuthProvider
from services.scenario_image_service import ScenarioImageService
from services.user_scenario_service import UserScenarioService
from store.mysql_scenario_image_repo import MysqlScenarioImageRepo
from store.mysql_scenario_input_field_repo import MysqlScenarioInputFieldRepo
from store.scenario_repo import ScenarioRepository


async def get_repo(request: Request) -> ScenarioRepository:
    repo = getattr(request.app.state, "scenario_repo", None)
    if repo is None:
        raise HTTPException(503, "Scenario repository chưa init")
    return repo


async def get_auth_provider(request: Request) -> AuthProvider:
    provider = getattr(request.app.state, "auth_provider", None)
    if provider is None:
        raise HTTPException(503, "Auth provider chưa init")
    return provider


async def get_current_user(
    request: Request,
    provider: AuthProvider = Depends(get_auth_provider),
) -> AuthenticatedUser:
    """Extract authenticated user. Raise 401 nếu không có/invalid."""
    # Normalize headers to lowercase dict (HTTP header case-insensitive)
    headers = {k.lower(): v for k, v in request.headers.items()}
    user = await provider.authenticate(headers)
    if user is None:
        raise HTTPException(401, "Unauthenticated. Gửi header X-User-Id.")
    return user


async def get_input_field_repo_optional(
    request: Request,
) -> Optional[MysqlScenarioInputFieldRepo]:
    """Lấy MysqlScenarioInputFieldRepo nếu đã init, None nếu chưa.

    Optional vì SQLite backend skip wiring. UserScenarioService accept None
    để giữ backward compat với test legacy.
    """
    return getattr(request.app.state, "input_field_repo", None)


async def get_scenario_service(
    repo: ScenarioRepository = Depends(get_repo),
    input_field_repo: Optional[MysqlScenarioInputFieldRepo] = Depends(
        get_input_field_repo_optional
    ),
) -> UserScenarioService:
    """UserScenarioService với optional input_field_repo (Phase 1 Input Fields).

    Khi backend=mysql + có input_field_repo: tạo revision mới sẽ auto-sync
    inputs[] từ YAML vào scenario_input_fields. Clone scenario cũng copy
    fields metadata từ source rev.

    Khi backend=sqlite hoặc input_field_repo=None: chỉ làm CRUD legacy, không sync.
    """
    return UserScenarioService(repo, input_field_repo=input_field_repo)


async def get_image_repo(request: Request) -> MysqlScenarioImageRepo:
    """Lấy MysqlScenarioImageRepo singleton (init 1 lần ở startup)."""
    repo = getattr(request.app.state, "scenario_image_repo", None)
    if repo is None:
        raise HTTPException(503, "Scenario image repository chưa init")
    return repo


async def get_image_service(
    scenario_repo: ScenarioRepository = Depends(get_repo),
    image_repo: MysqlScenarioImageRepo = Depends(get_image_repo),
) -> ScenarioImageService:
    return ScenarioImageService(scenario_repo, image_repo)
