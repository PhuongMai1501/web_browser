"""
LLM_base/services/captcha_solver.py — Đọc text trong ảnh CAPTCHA bằng vision LLM (OCR).

Khác với services/vision_matcher.py:
- vision_matcher TRẢ VỀ `ref` (vị trí element để click).
- captcha_solver TRẢ VỀ `text` (nội dung ký tự trong ảnh) — dùng để điền vào ô
  "mã xác minh".

Đây là OCR xác suất, KHÔNG bao giờ 100% với captcha méo + có gạch chéo. Caller
(action solve_captcha) chịu trách nhiệm vòng retry: nếu sai → reroll captcha
(bấm "Thay đổi") → gọi lại.

Module này chỉ làm 1 call duy nhất, không retry, không raise — fail trả "".

Env config:
- CAPTCHA_MODEL    — default "gpt-4o" (KHÔNG dùng mini: mini yếu hẳn với captcha
                     méo. Override = "gpt-4o-mini" nếu chấp nhận accuracy thấp hơn
                     để tiết kiệm cost.)
- CAPTCHA_TIMEOUT  — default 30 (giây)
- CAPTCHA_MAX_TOKENS — default 24 (chỉ cần trả vài ký tự)
"""

from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path

from openai import OpenAI

_log = logging.getLogger(__name__)

_CAPTCHA_MODEL = os.getenv("CAPTCHA_MODEL", "gpt-4o")
_CAPTCHA_TIMEOUT = int(os.getenv("CAPTCHA_TIMEOUT", "30"))
_CAPTCHA_MAX_TOKENS = int(os.getenv("CAPTCHA_MAX_TOKENS", "24"))

# Captcha alphanumeric — loại bỏ mọi ký tự không phải [A-Za-z0-9] khỏi câu trả
# lời (model đôi khi kèm dấu cách, dấu nháy, hoặc "Đáp án:"). KHÔNG lowercase —
# captcha thường phân biệt HOA/thường.
_CLEAN_RE = re.compile(r"[^A-Za-z0-9]")


def _build_prompt(hint: str, expected_len: int | None, has_hint: bool = False) -> str:
    len_hint = (
        f"Mã thường có khoảng {expected_len} ký tự. "
        if expected_len else ""
    )
    where = (
        "Có 2 ảnh: ẢNH 1 là tham chiếu — vùng CAPTCHA đã được KHOANH ĐỎ để bạn "
        "biết captcha nằm ở đâu trên trang. ẢNH 2 là trang hiện tại. Hãy đọc "
        "captcha ở ĐÚNG vùng tương ứng vùng khoanh đỏ trong ẢNH 2.\n\n"
        if has_hint else
        "Đây là ảnh CAPTCHA (chữ + số bị bóp méo, có thể có gạch chéo, nhiễu, "
        "màu nền).\n\n"
    )
    return (
        where +
        "Nhiệm vụ DUY NHẤT: đọc CHÍNH XÁC chuỗi ký tự captcha.\n\n"
        "QUY TẮC BẮT BUỘC:\n"
        "1. Trả về CHỈ chuỗi ký tự, KHÔNG giải thích, KHÔNG thêm chữ nào khác.\n"
        "2. PHÂN BIỆT chữ HOA và chữ thường — captcha PHÂN BIỆT hoa/thường.\n"
        "3. ⚠️ QUAN TRỌNG về CASE: nhiều chữ có HÌNH DẠNG GIỐNG HỆT nhau ở cả 2 "
        "dạng (c k o p s u v w x z, đôi khi j m n) — chỉ khác CHIỀU CAO. Cách "
        "xác định: SO SÁNH chiều cao chữ đó với các CHỮ SỐ và chữ rõ-ràng-HOA "
        "trong CÙNG ảnh. Chữ HOA cao bằng chữ số (full cap-height). Chữ thường "
        "THẤP HƠN rõ rệt (x-height, ~60% chiều cao). Nếu một chữ thấp hơn hẳn "
        "các chữ số/chữ HOA bên cạnh → đó là chữ THƯỜNG; nếu cao bằng → chữ HOA.\n"
        "4. Bỏ qua đường gạch chéo / nét nhiễu — chúng KHÔNG phải ký tự.\n"
        "5. KHÔNG thêm dấu cách giữa các ký tự.\n"
        f"6. {len_hint}Nếu không đọc được, trả về chuỗi rỗng.\n"
        f"{('GỢI Ý: ' + hint) if hint else ''}"
    ).strip()


def read_captcha_text(
    api_key: str,
    image_path: str,
    model: str = "",
    hint: str = "",
    expected_len: int | None = None,
    hint_image_url: str = "",
) -> str:
    """Đọc text captcha từ file ảnh local → chuỗi đã làm sạch, hoặc "".

    Trả "" khi: api_key trống, đọc file fail, API lỗi, hoặc model không đọc được.
    KHÔNG raise — caller xử lý retry.

    Args:
        api_key: OpenAI key.
        image_path: đường dẫn file ảnh local (PNG) — đã chụp/crop sẵn.
        model: override model; rỗng → dùng env CAPTCHA_MODEL.
        hint: mô tả phụ trợ (vd "captcha 6 ký tự, có gạch chéo").
        expected_len: số ký tự kỳ vọng (nếu biết) — đưa vào prompt.
        hint_image_url: URL ảnh THAM CHIẾU (user khoanh đỏ vùng captcha). Khi có,
            gửi kèm như ẢNH 1 + ảnh hiện tại là ẢNH 2 → GPT-4o biết captcha nằm
            đâu mà đọc (dùng khi không crop được). Lỗi tải ảnh → bỏ qua hint.
    """
    if not api_key:
        _log.warning("captcha_solver: api_key trống — skip")
        return ""

    try:
        img_b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
    except Exception as e:
        _log.warning("captcha_solver: đọc ảnh fail (%s): %s", image_path, e)
        return ""

    use_model = model or _CAPTCHA_MODEL
    has_hint = bool(hint_image_url)

    content = [{"type": "text", "text": _build_prompt(hint, expected_len, has_hint)}]
    # ẢNH 1 = hint khoanh đỏ (nếu có); ẢNH cuối = ảnh cần đọc.
    if has_hint:
        try:
            from services.vision_matcher import _fetch_image_b64
            hint_b64 = _fetch_image_b64(hint_image_url)
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{hint_b64}", "detail": "high"},
            })
        except Exception as e:
            _log.info("captcha_solver: tải hint image fail (%s) — bỏ qua hint", e)
            has_hint = False
    content.append({
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{img_b64}", "detail": "high"},
    })

    try:
        client = OpenAI(api_key=api_key, timeout=_CAPTCHA_TIMEOUT)
        resp = client.chat.completions.create(
            model=use_model,
            messages=[{"role": "user", "content": content}],
            max_tokens=_CAPTCHA_MAX_TOKENS,
            temperature=0,
        )
        answer = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        _log.warning("captcha_solver: API call fail (%s): %s",
                     type(e).__name__, e)
        return ""

    code = _CLEAN_RE.sub("", answer)
    if not code:
        _log.info("captcha_solver: model trả rỗng/không đọc được (raw=%r)",
                  answer[:60])
        return ""

    _log.info("captcha_solver: đọc được %d ký tự (model=%s)",
              len(code), use_model)
    return code
