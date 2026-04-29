"""
auth/mock_provider.py — Mock auth provider cho laptop dev.

Đọc header `X-User-Id` và tin tưởng luôn (KHÔNG verify).
Admin thông qua 1 trong 2 cách:
  - user_id == 'admin' (legacy convention)
  - Header `X-Admin-Token` khớp với env ADMIN_TOKEN (Phase 1+ cho UI admin code)

Safety: must_fail_production() = True. api/app.py phải check env `ENV=production`
trước khi instantiate provider này — nếu production + mock → refuse khởi động.
"""

from __future__ import annotations

import logging
import os
from typing import Mapping, Optional

from auth.providers import AuthenticatedUser, AuthProvider


_log = logging.getLogger(__name__)

_ADMIN_USER_ID = "admin"
_USER_HEADER = "x-user-id"
_ADMIN_TOKEN_HEADER = "x-admin-token"


class MockAuthProvider(AuthProvider):
    """Trust X-User-Id header blindly. Không verify signature/token.

    Admin elevation:
      - user_id == 'admin' (legacy)
      - X-Admin-Token header == env ADMIN_TOKEN (UI admin code field)
    """

    @property
    def name(self) -> str:
        return "mock"

    async def authenticate(
        self, headers: Mapping[str, str]
    ) -> Optional[AuthenticatedUser]:
        user_id = headers.get(_USER_HEADER)
        if not user_id:
            return None

        user_id = user_id.strip()
        if not user_id:
            return None

        is_admin = user_id == _ADMIN_USER_ID
        if not is_admin:
            token = headers.get(_ADMIN_TOKEN_HEADER, "").strip()
            # Phase 1 mock auth — fallback default để K8s không cần DevOps add env.
            # Phase 2 JWT sẽ thay thế hoàn toàn (xoá fallback này).
            expected = os.getenv("ADMIN_TOKEN", "hiepqn-2026-admin").strip()
            if token and expected and token == expected:
                is_admin = True

        return AuthenticatedUser(
            user_id=user_id,
            is_admin=is_admin,
        )

    def must_fail_production(self) -> bool:
        return True
