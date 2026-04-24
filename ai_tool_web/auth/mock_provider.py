"""
auth/mock_provider.py — Mock auth provider cho laptop dev.

Đọc header `X-User-Id` và tin tưởng luôn (KHÔNG verify).
is_admin = (user_id == 'admin') — quy ước đơn giản.

Safety: must_fail_production() = True. api/app.py phải check env `ENV=production`
trước khi instantiate provider này — nếu production + mock → refuse khởi động.
"""

from __future__ import annotations

import logging
from typing import Mapping, Optional

from auth.providers import AuthenticatedUser, AuthProvider


_log = logging.getLogger(__name__)

_ADMIN_USER_ID = "admin"
_HEADER_NAME = "x-user-id"   # lowercase — HTTP headers case-insensitive, normalize caller-side


class MockAuthProvider(AuthProvider):
    """Trust X-User-Id header blindly. Không verify signature/token.

    Usage:
        provider = MockAuthProvider()
        user = await provider.authenticate({'x-user-id': 'hiepqn'})
        # user = AuthenticatedUser(user_id='hiepqn', is_admin=False)

    Caller (middleware) chịu trách nhiệm normalize header key về lowercase
    trước khi pass vào authenticate().
    """

    @property
    def name(self) -> str:
        return "mock"

    async def authenticate(
        self, headers: Mapping[str, str]
    ) -> Optional[AuthenticatedUser]:
        user_id = headers.get(_HEADER_NAME)
        if not user_id:
            return None

        user_id = user_id.strip()
        if not user_id:
            return None

        return AuthenticatedUser(
            user_id=user_id,
            is_admin=(user_id == _ADMIN_USER_ID),
        )

    def must_fail_production(self) -> bool:
        return True
