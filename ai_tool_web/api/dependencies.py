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

from fastapi import Depends, HTTPException, Request

from auth.providers import AuthenticatedUser, AuthProvider
from services.user_scenario_service import UserScenarioService
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


async def get_scenario_service(
    repo: ScenarioRepository = Depends(get_repo),
) -> UserScenarioService:
    return UserScenarioService(repo)
