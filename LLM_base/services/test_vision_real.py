"""
test_vision_real.py — Smoke test gpt-4o vision matcher với ảnh thật.

Không cần MySQL/MinIO/API server — chỉ gọi OpenAI API trực tiếp.

Usage:
    set OPENAI_API_KEY=sk-...
    python test_vision_real.py --hint hint.png --current current.png \
        [--description "Icon menu hamburger"] [--snapshot snapshot.txt]

Nếu --snapshot không cung cấp, dùng dummy snapshot với refs e1..e30 để
LLM có range trả về. Khi đó test chỉ verify "LLM đọc được ảnh + trả ref
hợp lý" — không validate ref click được thật.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Path setup — script nằm trong services/, parent là LLM_base/
_THIS = Path(__file__).resolve().parent
_LLM_BASE = _THIS.parent
sys.path.insert(0, str(_LLM_BASE))

from services.vision_matcher import find_ref_by_image  # noqa: E402


_DUMMY_SNAPSHOT = """
- generic [ref=e1]
- link "Trang chủ" [ref=e2]
- link "Tra cứu" [ref=e3]
- link "Tra cứu tiền điện" [ref=e4]
- link "Hóa đơn tiền điện" [ref=e5]
- button [ref=e6]
- generic [ref=e7] (icon=menu)
- link "Đăng nhập" [ref=e8]
- textbox "Tên đăng nhập" [ref=e9]
- textbox "Mật Khẩu" [ref=e10]
- button "Đăng nhập" [ref=e11]
- link [ref=e12] (icon=hamburger)
- generic [ref=e13]
- listitem [ref=e14]
- listitem [ref=e15]
- link "Mã Khách hàng sử dụng điện" [ref=e16]
- button "Tìm kiếm" [ref=e17]
- link "Đăng xuất" [ref=e18]
- generic [ref=e19]
- generic [ref=e20]
- dialog "Thông báo" [ref=e21]
- button "X" [ref=e22] (aria-label=Close)
- button "Đóng" [ref=e23]
- button "Bỏ qua" [ref=e24]
- generic [ref=e25] (icon=close)
""".strip()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--hint", required=True, help="Path ảnh khoanh đỏ")
    p.add_argument("--current", required=True, help="Path screenshot hiện tại")
    p.add_argument(
        "--snapshot", default=None,
        help="Path file txt accessibility snapshot. Nếu thiếu → dummy.",
    )
    p.add_argument(
        "--description", default="",
        help="Mô tả phụ trợ (image_hint_desc)",
    )
    args = p.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("ERROR: OPENAI_API_KEY env chưa set", file=sys.stderr)
        return 1

    for path in (args.hint, args.current):
        if not Path(path).exists():
            print(f"ERROR: file không tồn tại: {path}", file=sys.stderr)
            return 1

    if args.snapshot:
        snapshot_text = Path(args.snapshot).read_text(encoding="utf-8")
    else:
        snapshot_text = _DUMMY_SNAPSHOT
        print("[INFO] Dùng dummy snapshot (refs e1..e20)")

    print(f"[INFO] Hint    : {args.hint}")
    print(f"[INFO] Current : {args.current}")
    print(f"[INFO] Desc    : {args.description or '(không có)'}")
    print(f"[INFO] Snapshot: {len(snapshot_text)} chars")
    print(f"[INFO] API key : {api_key[:7]}...{api_key[-4:]}")
    print(f"[INFO] Calling gpt-4o (timeout 30s)...")

    import time
    t0 = time.time()
    ref = find_ref_by_image(
        api_key=api_key,
        current_screenshot_path=args.current,
        hint_image_url=args.hint,        # local path cũng được (không HTTP prefix)
        snapshot_text=snapshot_text,
        description=args.description,
    )
    elapsed_ms = int((time.time() - t0) * 1000)

    print(f"\n{'=' * 60}")
    print(f"Latency: {elapsed_ms} ms")
    if ref:
        print(f"RESULT : ref={ref}  (LLM tìm thấy element trong snapshot)")
        return 0
    else:
        print("RESULT : None")
        print("Nguyên nhân (xem log warnings phía trên):")
        print("  - LLM trả NOT_FOUND (không thấy element tương đương)")
        print("  - LLM trả ref hallucinated (không có trong snapshot)")
        print("  - Network/API error (timeout, rate limit, auth)")
        print("  - Load ảnh fail")
        return 2


if __name__ == "__main__":
    # Bật log để thấy warnings từ vision_matcher
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s [%(name)s] %(message)s",
    )
    sys.exit(main())
