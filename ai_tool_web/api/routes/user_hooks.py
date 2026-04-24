"""
api/routes/user_hooks.py — GET /v1/hooks (G7).

Trả list tên hook trong HOOK_REGISTRY — UI editor dùng cho auto-complete +
validate hook name trước khi submit.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.dependencies import get_current_user
from auth.providers import AuthenticatedUser


# LLM_base không trong PYTHONPATH mặc định
_LLM_BASE = Path(__file__).resolve().parent.parent.parent.parent / "LLM_base"
if str(_LLM_BASE) not in sys.path:
    sys.path.append(str(_LLM_BASE))


router = APIRouter(prefix="/v1/hooks", tags=["hooks"])


class HookInfo(BaseModel):
    name: str
    description: str = ""


@router.get("", response_model=list[HookInfo])
async def list_hooks(
    _user: AuthenticatedUser = Depends(get_current_user),
):
    """List hook names đã register trong HOOK_REGISTRY.

    Lưu ý: hooks được register qua `@hook(name)` decorator khi import module.
    api.app startup phải import `scenarios.hooks` trước khi route này serve.
    """
    from scenarios.hooks_registry import HOOK_REGISTRY  # late import

    return [
        HookInfo(name=name, description=(fn.__doc__ or "").strip().split("\n")[0])
        for name, fn in sorted(HOOK_REGISTRY.items())
    ]
