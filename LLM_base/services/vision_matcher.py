"""
LLM_base/services/vision_matcher.py — Tìm element ref bằng vision LLM.

Dùng khi text matcher (snapshot_query.find_ref) fail và TargetSpec có
`image_hint`. Gọi vision LLM với:
- ẢNH 1: hint image (user khoanh đỏ element cần click)
- ẢNH 2: current page screenshot
- TEXT:  snapshot accessibility tree (mỗi element có ref dạng eN)
- TEXT:  description bổ trợ (image_hint_desc)

Trả về ref (e.g. "e47") hoặc None.

Cost guard: caller (FlowRuntime) chịu trách nhiệm cap số call/session.
Module này chỉ làm 1 call duy nhất, không retry, timeout 30s.

Env config:
- VISION_MODEL    — default "gpt-4o-mini" (cost-optimized; đổi "gpt-4o"
                    nếu cần accuracy cao hơn ~5-10%)
- VISION_TIMEOUT  — default 30 (giây)
- VISION_MAX_TOKENS — default 20 (chỉ cần "eN" hoặc "NOT_FOUND")
"""

from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path
from typing import Optional

import requests
from openai import OpenAI

_log = logging.getLogger(__name__)

_REF_PATTERN = re.compile(r"\b(e\d+)\b")
_VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4o-mini")
_VISION_TIMEOUT = int(os.getenv("VISION_TIMEOUT", "30"))
_VISION_MAX_TOKENS = int(os.getenv("VISION_MAX_TOKENS", "20"))
_SNAPSHOT_MAX_CHARS = 8000


def _fetch_image_b64(url_or_path: str) -> str:
    """Tải ảnh từ CDN URL hoặc đọc local path → base64.

    Pod K8s KHÔNG có direct egress đến cdn.fstats.ai — phải đi qua
    HTTP_PROXY env. Confirmed via /v1/debug/test-cdn-fetch 2026-05-08:
    direct (trust_env=False) → ConnectTimeout sau 10s; proxy (default)
    → 200 OK 487ms. Dùng default requests.get() để respect env
    HTTP_PROXY/HTTPS_PROXY.

    Raise nếu fail — caller phải bắt.
    """
    if url_or_path.startswith(("http://", "https://")):
        resp = requests.get(url_or_path, timeout=30)
        resp.raise_for_status()
        return base64.b64encode(resp.content).decode()
    return base64.b64encode(Path(url_or_path).read_bytes()).decode()


def _build_prompt(
    snapshot_text: str,
    description: str,
    candidate_refs: Optional[list[str]] = None,
) -> str:
    snapshot_short = snapshot_text[:_SNAPSHOT_MAX_CHARS]
    if candidate_refs:
        # Ambiguous mode: text matcher đã narrow xuống N candidates,
        # LLM CHỈ chọn 1 trong list. Giảm hallucinate + tăng accuracy.
        candidate_str = ", ".join(candidate_refs)
        return (
            "Bạn là vision matcher disambiguate cho automation browser.\n\n"
            "ẢNH 1 (hint): User đã khoanh đỏ element họ muốn click.\n"
            "ẢNH 2 (current): Screenshot trang hiện tại của browser.\n"
            "SNAPSHOT (accessibility tree, mỗi element có ref dạng e<N>):\n"
            f"{snapshot_short}\n\n"
            f"MÔ TẢ BỔ SUNG: {description or '(không có)'}\n\n"
            f"⚠️ CANDIDATES — Text matcher đã narrow xuống {len(candidate_refs)} ref "
            f"khả thi: [{candidate_str}]\n"
            "Bạn PHẢI chọn DUY NHẤT 1 ref TỪ DANH SÁCH TRÊN — element nào "
            "tương đương với vùng đỏ trong ẢNH 1 (cùng vai trò, vị trí, "
            "nội dung). KHÔNG được chọn ref ngoài danh sách.\n\n"
            "Trả về CHÍNH XÁC ref dạng e<số> (1 trong các candidates), "
            "không giải thích. Nếu không có candidate nào match hint → NOT_FOUND."
        )
    # Default mode: tìm trong toàn snapshot (text matcher fail hoàn toàn)
    return (
        "Bạn là vision matcher cho automation browser.\n\n"
        "ẢNH 1 (hint): User đã khoanh đỏ element họ muốn click.\n"
        "ẢNH 2 (current): Screenshot trang hiện tại của browser.\n"
        "SNAPSHOT (accessibility tree, mỗi element có ref dạng e<N>):\n"
        f"{snapshot_short}\n\n"
        f"MÔ TẢ BỔ SUNG: {description or '(không có)'}\n\n"
        "Nhiệm vụ:\n"
        "1. Quan sát vùng đỏ trong ẢNH 1 → xác định loại element "
        "(button/link/input/...) và đặc điểm nhận dạng.\n"
        "2. Tìm element TƯƠNG ĐƯƠNG trong ẢNH 2 (cùng vai trò, vị trí, "
        "nội dung — không cần khoanh đỏ).\n"
        "3. Map sang ref trong SNAPSHOT bằng text/role/vị trí.\n"
        "4. Trả về CHÍNH XÁC ref dạng e<số>, không giải thích.\n"
        "5. Nếu không tìm thấy element tương đương → trả NOT_FOUND.\n\n"
        "Trả lời (chỉ ref hoặc NOT_FOUND):"
    )


def find_ref_by_image(
    api_key: str,
    current_screenshot_path: str,
    hint_image_url: str,
    snapshot_text: str,
    description: str = "",
    candidate_refs: Optional[list[str]] = None,
) -> Optional[str]:
    """Trả ref tìm thấy trong snapshot, hoặc None.

    None khi:
    - Tải ảnh fail (network, không tồn tại)
    - LLM trả NOT_FOUND
    - LLM trả ref hallucinated (không có trong snapshot)
    - API call lỗi (timeout, auth, rate limit)

    KHÔNG raise — return None để runner xử lý fallback (yield error step).
    """
    if not api_key:
        _log.warning("vision_matcher: api_key trống — skip vision")
        return None

    try:
        hint_b64 = _fetch_image_b64(hint_image_url)
    except Exception as e:
        _log.warning("vision_matcher: load hint image fail (%s): %s",
                     hint_image_url, e)
        return None

    try:
        screen_b64 = _fetch_image_b64(current_screenshot_path)
    except Exception as e:
        _log.warning("vision_matcher: load current screenshot fail (%s): %s",
                     current_screenshot_path, e)
        return None

    prompt = _build_prompt(snapshot_text, description, candidate_refs)
    try:
        client = OpenAI(api_key=api_key, timeout=_VISION_TIMEOUT)
        resp = client.chat.completions.create(
            model=_VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{hint_b64}"
                        },
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{screen_b64}"
                        },
                    },
                ],
            }],
            max_tokens=_VISION_MAX_TOKENS,
            temperature=0,
        )
        answer = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        _log.warning("vision_matcher: API call fail (%s): %s",
                     type(e).__name__, e)
        return None

    if "NOT_FOUND" in answer.upper():
        _log.info("vision_matcher: LLM returned NOT_FOUND")
        return None

    match = _REF_PATTERN.search(answer)
    if not match:
        _log.info("vision_matcher: no ref pattern in response: %r",
                  answer[:100])
        return None

    ref = match.group(1)
    # Validate: ref phải tồn tại trong snapshot (chống hallucinate)
    if f"ref={ref}" not in snapshot_text:
        _log.warning(
            "vision_matcher: ref %s hallucinated, not in snapshot", ref,
        )
        return None
    # Validate ambiguous mode: ref phải nằm trong candidates list nếu cung cấp
    if candidate_refs and ref not in candidate_refs:
        _log.warning(
            "vision_matcher: ref %s not in candidates %s — LLM ignored constraint",
            ref, candidate_refs,
        )
        return None

    _log.info("vision_matcher: matched ref=%s", ref)
    return ref
