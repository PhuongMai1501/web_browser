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

Provider: dùng client OpenAI-compatible. Mặc định gọi OpenAI; có thể TRỎ sang
Qwen-VL LOCAL (vLLM/Ollama/SGLang), Gemini, hoặc provider khác MÀ KHÔNG đổi code,
chỉ qua env (xem dưới). OpenAI ngày càng TỪ CHỐI giải captcha → chuyển sang server
Qwen local (không policy từ chối, không phí, dữ liệu không ra ngoài).

Env config:
- CAPTCHA_MODEL    — tên model (đúng --served-model-name). MẶC ĐỊNH
                     "Qwen/Qwen3.5-27B-FP8". YAML KHÔNG đè được — model CHỈ từ env này.
- CAPTCHA_BASE_URL — endpoint OpenAI-compatible. MẶC ĐỊNH Qwen vLLM nội bộ
                     "http://124.197.18.58/vllm/v1". Đặt "" để dùng OpenAI chính chủ.
- CAPTCHA_API_KEY  — API key RIÊNG cho captcha (Gemini/OpenAI). ƯU TIÊN hơn
                     OPENAI_API_KEY. Server LOCAL thường KHÔNG cần (code tự dùng
                     placeholder khi có CAPTCHA_BASE_URL). ⚠️ key THẬT chỉ đặt qua
                     K8s Secret → env; KHÔNG hardcode code/YAML, KHÔNG log ra ngoài.
- CAPTCHA_TIMEOUT  — default 30 (giây)
- CAPTCHA_MAX_TOKENS — default 24 (chỉ cần trả vài ký tự)
- CAPTCHA_MAX_LEN  — default 12; kết quả dài hơn = model giải thích/từ chối, loại.

Sampling (CHỈ áp dụng khi CAPTCHA_BASE_URL set = self-host vLLM/Qwen; OpenAI chính
chủ luôn dùng temperature=0). Mặc định = preset Qwen "non-thinking/general":
- CAPTCHA_THINKING — "1/true" bật suy luận; MẶC ĐỊNH TẮT cho OCR (bật phải tăng
                     CAPTCHA_MAX_TOKENS, nếu không <think> ăn hết → trả rỗng).
- CAPTCHA_TEMPERATURE (0.7) / CAPTCHA_TOP_P (0.8) / CAPTCHA_TOP_K (20) /
  CAPTCHA_MIN_P (0.0) / CAPTCHA_PRESENCE_PENALTY (0.0) / CAPTCHA_REPETITION_PENALTY (1.0).
  (presence_penalty để 0.0 thay vì 1.5 của preset gốc — captcha có thể lặp ký tự.)
"""

from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path

from openai import OpenAI

_log = logging.getLogger(__name__)

# Model + endpoint MẶC ĐỊNH = Qwen self-host nội bộ. YAML KHÔNG đè model nữa
# (solve_captcha bỏ qua step.vision_model) — model chỉ từ env CAPTCHA_MODEL này.
_CAPTCHA_MODEL = os.getenv("CAPTCHA_MODEL", "Qwen/Qwen3.5-27B-FP8")
_CAPTCHA_TIMEOUT = int(os.getenv("CAPTCHA_TIMEOUT", "30"))
_CAPTCHA_MAX_TOKENS = int(os.getenv("CAPTCHA_MAX_TOKENS", "24"))
# Endpoint OpenAI-compatible. Default = Qwen vLLM nội bộ; env override khi đổi server.
# Endpoint KHÔNG phải secret; KEY thì vẫn chỉ từ env (không hardcode, không log).
# Đặt CAPTCHA_BASE_URL="" để quay lại OpenAI chính chủ.
_CAPTCHA_BASE_URL = os.getenv("CAPTCHA_BASE_URL", "http://124.197.18.58/vllm/v1").strip()
_CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "").strip()
# Captcha thực ~4-8 ký tự; quá dài = câu giải thích/từ chối → loại.
_CAPTCHA_MAX_LEN = int(os.getenv("CAPTCHA_MAX_LEN", "12"))


def _envf(name: str, default: float) -> float:
    """Đọc env float; trống/sai → default."""
    v = os.getenv(name, "").strip()
    try:
        return float(v) if v else default
    except ValueError:
        return default


# Sampling cho server SELF-HOST (vLLM/Qwen) — chỉ áp dụng khi CAPTCHA_BASE_URL set
# (OpenAI chính chủ giữ greedy temperature=0). Mặc định = preset Qwen "non-thinking,
# general" NHƯNG presence_penalty=0.0 (khác gốc 1.5) vì captcha có thể LẶP ký tự,
# không nên phạt lặp. top_k/min_p/repetition_penalty không phải param chuẩn OpenAI
# → gửi qua extra_body của vLLM.
_CAPTCHA_TEMPERATURE = _envf("CAPTCHA_TEMPERATURE", 0.7)
_CAPTCHA_TOP_P = _envf("CAPTCHA_TOP_P", 0.8)
_CAPTCHA_TOP_K = int(_envf("CAPTCHA_TOP_K", 20))
_CAPTCHA_MIN_P = _envf("CAPTCHA_MIN_P", 0.0)
_CAPTCHA_PRESENCE_PENALTY = _envf("CAPTCHA_PRESENCE_PENALTY", 0.0)
_CAPTCHA_REPETITION_PENALTY = _envf("CAPTCHA_REPETITION_PENALTY", 1.0)
# Suy luận (thinking): MẶC ĐỊNH TẮT cho OCR. OCR là nhận dạng, không cần reasoning;
# bật thinking sẽ khiến <think> ăn hết CAPTCHA_MAX_TOKENS (24) → trả rỗng. Muốn bật:
# CAPTCHA_THINKING=1 VÀ tăng CAPTCHA_MAX_TOKENS lớn (vài trăm) + chấp nhận chậm/tốn.
_CAPTCHA_THINKING = os.getenv("CAPTCHA_THINKING", "").strip().lower() in ("1", "true", "yes", "on")

# Captcha alphanumeric — loại bỏ mọi ký tự không phải [A-Za-z0-9] khỏi câu trả
# lời (model đôi khi kèm dấu cách, dấu nháy, hoặc "Đáp án:"). KHÔNG lowercase —
# captcha thường phân biệt HOA/thường.
_CLEAN_RE = re.compile(r"[^A-Za-z0-9]")
# Gỡ block suy luận <think>...</think> phòng khi server vẫn phát (vd thinking bật,
# hoặc model phát think rỗng) — tránh lọt "think" vào mã.
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)

# Mẫu câu TỪ CHỐI / GIẢI THÍCH của model (OpenAI siết policy giải captcha; Gemini
# hiếm hơn nhưng vẫn có thể). Nếu answer chứa các cụm này → KHÔNG phải mã, trả ""
# để caller reroll/ask-user thay vì điền nhầm câu từ chối (vd "Xin lỗi tôi không
# thể giúp với việc này" → "Xinlitikhngthgipvivicny") vào ô captcha.
_REFUSAL_MARKERS = (
    "xin lỗi", "tôi không thể", "mình không thể", "không thể giúp",
    "không thể hỗ trợ", "không hỗ trợ", "i'm sorry", "i am sorry",
    "i cannot", "i can't", "i can not", "i'm not able", "i am not able",
    "unable to", "cannot assist", "can't help", "as an ai", "i won't",
)


def _looks_like_refusal(answer: str) -> bool:
    """True nếu answer là câu từ chối/giải thích (không phải mã captcha)."""
    low = answer.lower()
    return any(m in low for m in _REFUSAL_MARKERS)


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
    # Ưu tiên key RIÊNG cho captcha (CAPTCHA_API_KEY, vd Gemini); fallback key
    # OpenAI caller truyền vào. Cả 2 đều từ env — không hardcode, không log.
    use_key = _CAPTCHA_API_KEY or api_key
    # Server LOCAL (vLLM/Ollama Qwen-VL) trỏ qua CAPTCHA_BASE_URL thường KHÔNG cần
    # key, nhưng OpenAI client bắt buộc api_key non-empty → đặt placeholder.
    # (Nếu server bật --api-key thì vẫn set CAPTCHA_API_KEY như bình thường.)
    if not use_key and _CAPTCHA_BASE_URL:
        use_key = "EMPTY"
    if not use_key:
        _log.warning("captcha_solver: thiếu API key/endpoint "
                     "(CAPTCHA_API_KEY/CAPTCHA_BASE_URL/OPENAI_API_KEY) — skip")
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
        # base_url chỉ thêm khi có (Qwen self-host/Gemini); trống = OpenAI mặc định.
        client_kwargs = {"api_key": use_key, "timeout": _CAPTCHA_TIMEOUT}
        if _CAPTCHA_BASE_URL:
            client_kwargs["base_url"] = _CAPTCHA_BASE_URL
        client = OpenAI(**client_kwargs)

        if _CAPTCHA_BASE_URL:
            # SELF-HOST (vLLM/Qwen): sampling đầy đủ theo khuyến nghị Qwen.
            # top_k/min_p/repetition_penalty + enable_thinking KHÔNG phải param
            # chuẩn OpenAI → đưa qua extra_body (vLLM nhận). Tắt thinking cho OCR.
            extra_body = {
                "top_k": _CAPTCHA_TOP_K,
                "min_p": _CAPTCHA_MIN_P,
                "repetition_penalty": _CAPTCHA_REPETITION_PENALTY,
                "chat_template_kwargs": {"enable_thinking": _CAPTCHA_THINKING},
            }
            resp = client.chat.completions.create(
                model=use_model,
                messages=[{"role": "user", "content": content}],
                max_tokens=_CAPTCHA_MAX_TOKENS,
                temperature=_CAPTCHA_TEMPERATURE,
                top_p=_CAPTCHA_TOP_P,
                presence_penalty=_CAPTCHA_PRESENCE_PENALTY,
                extra_body=extra_body,
            )
        else:
            # OpenAI chính chủ: greedy đơn giản (gpt-4o ổn với temperature=0).
            resp = client.chat.completions.create(
                model=use_model,
                messages=[{"role": "user", "content": content}],
                max_tokens=_CAPTCHA_MAX_TOKENS,
                temperature=0,
            )
        answer = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        # KHÔNG log key/base_url — chỉ loại lỗi + message SDK.
        _log.warning("captcha_solver: API call fail (%s): %s",
                     type(e).__name__, e)
        return ""

    # Gỡ block <think> nếu server phát (thinking bật, hoặc think rỗng).
    answer = _THINK_RE.sub("", answer).strip()

    # Model từ chối/giải thích → KHÔNG nhận làm mã (tránh false-positive như
    # "Xinlitikhngthgipvivicny"). Caller sẽ reroll/escalate/ask-user.
    if _looks_like_refusal(answer):
        _log.info("captcha_solver: model TỪ CHỐI/giải thích (model=%s, raw=%r) "
                  "— coi là fail", use_model, answer[:80])
        return ""

    code = _CLEAN_RE.sub("", answer)
    if not code:
        _log.info("captcha_solver: model trả rỗng/không đọc được (raw=%r)",
                  answer[:60])
        return ""
    # Quá dài = câu giải thích/từ chối lọt lưới marker → loại.
    if len(code) > _CAPTCHA_MAX_LEN:
        _log.info("captcha_solver: kết quả dài bất thường %d ký tự (>%d) — nghi "
                  "câu giải thích/từ chối, coi là fail (raw=%r)",
                  len(code), _CAPTCHA_MAX_LEN, answer[:80])
        return ""

    _log.info("captcha_solver: đọc được %d ký tự (model=%s)",
              len(code), use_model)
    return code
