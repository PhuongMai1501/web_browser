"""
auth/providers.py — Authentication provider interface.

Phase 1: MockAuthProvider (X-User-Id header, chỉ dùng laptop/dev).
Phase 2: SharedSecretAuthProvider (HMAC), JwtAuthProvider (SSO).

Middleware trong api/app.py sẽ inject provider theo env AUTH_PROVIDER.
Routes depend vào interface qua FastAPI dependency, không biết impl cụ thể.

Safety rule: provider nào return must_fail_production()=True thì app REFUSE
khởi động khi ENV=production. Tránh accidentally expose mock auth lên prod.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping, Optional


# ── Domain model ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AuthenticatedUser:
    """Thông tin user đã xác thực. Immutable.

    Phase 1: chỉ có user_id + is_admin.
    Phase 2: thêm org_id, roles, email.
    """
    user_id: str
    is_admin: bool = False


# ── Provider interface ───────────────────────────────────────────────────────

class AuthProvider(ABC):
    """Verify request headers và trả về authenticated user, hoặc None nếu invalid."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name cho logging/telemetry. Ví dụ 'mock', 'shared_secret', 'jwt'."""

    @abstractmethod
    async def authenticate(
        self, headers: Mapping[str, str]
    ) -> Optional[AuthenticatedUser]:
        """Verify credentials trong headers.

        Args:
            headers: Request headers (case-insensitive lookup khuyến nghị trong impl).

        Returns:
            AuthenticatedUser nếu xác thực OK.
            None nếu anonymous hoặc credentials invalid.

        Raises:
            Không raise — lỗi là trả None để middleware decide (401 vs allow anon).
        """

    @abstractmethod
    def must_fail_production(self) -> bool:
        """Nếu True, app từ chối khởi động khi ENV=production sử dụng provider này.

        MockAuthProvider: True (không an toàn cho production).
        SharedSecret/JWT: False.
        """
