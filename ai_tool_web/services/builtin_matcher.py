"""
services/builtin_matcher.py — Match query → builtin scenario, skip LLM.

Pipeline:
  1. Nhận NL description từ user
  2. Detect keyword intent
  3. Nếu match builtin → load YAML từ disk, trả ngay (0 LLM token)
  4. Nếu không match → caller fallback gọi LLM gen_yaml()

Tiết kiệm token: 80%+ queries phổ biến (login chang, search, open law doc)
không phải gọi LLM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntentPattern:
    """1 rule match query → builtin scenario."""

    scenario_id: str
    keywords_all: tuple[str, ...] = ()      # phải có TẤT CẢ
    keywords_any: tuple[str, ...] = ()      # phải có ÍT NHẤT 1


# Thứ tự ưu tiên — pattern đầu match thì stop.
# Sửa list này khi thêm builtin scenario mới.
_INTENT_PATTERNS: tuple[IntentPattern, ...] = (
    IntentPattern(
        scenario_id="chang_login",
        keywords_all=("chang",),
        keywords_any=(
            "đăng nhập", "đăng nhâp", "dang nhap",
            "login", "sign in", "signin",
            "khởi hành", "khoi hanh",
            "microsoft azure", "microsoft",
        ),
    ),
    IntentPattern(
        scenario_id="search_thuvienphapluat",
        keywords_all=("thuvienphapluat",),
        keywords_any=("tìm", "search", "tra cứu", "tra cuu"),
    ),
    IntentPattern(
        scenario_id="open_law_document",
        keywords_all=("thuvienphapluat",),
        keywords_any=("mở", "open", "tải", "download", "xem", "đọc"),
    ),
)


def _builtin_dir_candidates() -> tuple[Path, ...]:
    """Tìm thư mục builtin YAML — thử nhiều path tương thích cả dev + K8s.

    - K8s/product_build: agent_browser/scenarios/builtin/
    - dev: LLM_base/scenarios/builtin/
    """
    here = Path(__file__).resolve()
    return (
        # Cùng package agent_browser (product_build bundle)
        here.parent.parent / "scenarios" / "builtin",
        # LLM_base sibling (dev layout: ai_tool_web/services/ → LLM_base/scenarios/builtin/)
        here.parent.parent.parent / "LLM_base" / "scenarios" / "builtin",
    )


def match_builtin(description: str) -> Optional[str]:
    """Detect intent từ description, trả scenario_id nếu match.

    Args:
        description: NL query từ user.

    Returns:
        scenario_id (vd "chang_login") nếu match builtin, None nếu không.
    """
    if not description:
        return None

    desc_lower = description.lower().strip()
    for pattern in _INTENT_PATTERNS:
        if pattern.keywords_all and not all(
            kw.lower() in desc_lower for kw in pattern.keywords_all
        ):
            continue
        if pattern.keywords_any and not any(
            kw.lower() in desc_lower for kw in pattern.keywords_any
        ):
            continue
        _log.info("Builtin match: query → scenario_id=%s", pattern.scenario_id)
        return pattern.scenario_id
    return None


def load_builtin_yaml(scenario_id: str) -> Optional[str]:
    """Đọc YAML raw của builtin scenario từ disk.

    Args:
        scenario_id: id builtin (vd "chang_login").

    Returns:
        YAML text nếu file tồn tại, None nếu không tìm thấy.
    """
    if not scenario_id:
        return None
    for d in _builtin_dir_candidates():
        path = d / f"{scenario_id}.yaml"
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except Exception as e:
                _log.error("Đọc builtin YAML %s fail: %s", path, e)
                return None
    _log.warning("Builtin YAML không tìm thấy: %s.yaml", scenario_id)
    return None
