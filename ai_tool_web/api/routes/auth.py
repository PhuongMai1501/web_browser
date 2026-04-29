"""
api/routes/auth.py — Auth introspection endpoint.

Endpoints:
  GET /v1/auth/me  → user info (user_id, is_admin, provider)

UI dùng để check admin status sau khi user nhập admin code.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.dependencies import get_auth_provider, get_current_user
from auth.providers import AuthenticatedUser, AuthProvider


router = APIRouter(prefix="/v1/auth", tags=["auth"])


class MeResponse(BaseModel):
    user_id: str
    is_admin: bool
    provider: str


@router.get("/me", response_model=MeResponse)
async def me(
    user: AuthenticatedUser = Depends(get_current_user),
    provider: AuthProvider = Depends(get_auth_provider),
):
    """Trả về user info từ X-User-Id (+ X-Admin-Token nếu có)."""
    return MeResponse(
        user_id=user.user_id,
        is_admin=user.is_admin,
        provider=provider.name,
    )
