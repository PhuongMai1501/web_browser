"""
Test — MockAuthProvider

Chạy:
  cd deploy_server/ai_tool_web
  python tests/test_mock_auth.py

Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

from auth.mock_provider import MockAuthProvider  # noqa: E402
from auth.providers import AuthenticatedUser  # noqa: E402


_PASS: list[str] = []
_FAIL: list[tuple[str, str]] = []


def _check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASS.append(label)
        print(f"  [PASS] {label}")
    else:
        _FAIL.append((label, detail))
        print(f"  [FAIL] {label}{(' -> ' + detail) if detail else ''}")


async def test_mock():
    print("=== MockAuthProvider ===")
    p = MockAuthProvider()

    # Basic properties
    _check("name = 'mock'", p.name == "mock")
    _check("must_fail_production = True", p.must_fail_production() is True)

    # Regular user
    user = await p.authenticate({"x-user-id": "hiepqn"})
    _check("regular user authenticated",
           isinstance(user, AuthenticatedUser) and user.user_id == "hiepqn")
    _check("regular user is_admin=False", user.is_admin is False)

    # Admin user
    admin = await p.authenticate({"x-user-id": "admin"})
    _check("admin user authenticated",
           admin is not None and admin.user_id == "admin")
    _check("admin user is_admin=True", admin.is_admin is True)

    # Missing header → None
    none1 = await p.authenticate({})
    _check("missing header → None", none1 is None)

    # Empty string → None
    none2 = await p.authenticate({"x-user-id": ""})
    _check("empty user_id → None", none2 is None)

    # Whitespace-only → None
    none3 = await p.authenticate({"x-user-id": "   "})
    _check("whitespace-only user_id → None", none3 is None)

    # Leading/trailing whitespace trimmed
    trimmed = await p.authenticate({"x-user-id": "  hiepqn  "})
    _check("whitespace trimmed", trimmed is not None and trimmed.user_id == "hiepqn")

    # Other headers ignored
    user2 = await p.authenticate({
        "x-user-id": "alice",
        "authorization": "Bearer xyz",
        "x-other": "junk",
    })
    _check("extra headers ignored",
           user2 is not None and user2.user_id == "alice" and user2.is_admin is False)

    # AuthenticatedUser immutable (frozen dataclass)
    try:
        user.user_id = "changed"  # type: ignore[misc]
        _check("AuthenticatedUser frozen", False, "mutation allowed")
    except Exception:
        _check("AuthenticatedUser frozen", True)


async def main():
    try:
        await test_mock()
    except Exception:
        traceback.print_exc()
        _FAIL.append(("test_mock", "uncaught exception"))

    print(f"\n{'='*50}")
    print(f"Total: {len(_PASS)} pass, {len(_FAIL)} fail")
    if _FAIL:
        print("\nFailed:")
        for label, detail in _FAIL:
            print(f"  - {label}: {detail}")
        return 1
    print("\n[ALL PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
