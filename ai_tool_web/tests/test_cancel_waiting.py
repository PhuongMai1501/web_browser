"""
Test Case: Cancel session trong lúc waiting_for_user

Kịch bản:
  1. Tạo session chang_login KHÔNG có credentials → agent sẽ trigger ask
  2. Lắng nghe SSE stream, chờ event type=ask
  3. Ngay khi nhận ask → gọi POST /cancel
  4. Xác nhận SSE nhận event type=cancelled
  5. GET /v1/sessions/{id} → status phải là cancelled

Yêu cầu:
  - API đang chạy tại BASE_URL
  - Worker đang chạy (có thể pick up job)
  - pip install requests (nếu chưa có)

Chạy:
  python tests/test_cancel_waiting.py
  python tests/test_cancel_waiting.py --base-url http://localhost:8000
"""

import argparse
import json
import sys
import threading
import time
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8000"
WAIT_FOR_ASK_TIMEOUT_S = 180  # tối đa 3 phút chờ agent reach ask state
POLL_INTERVAL_S = 0.5


# ── Helpers ────────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _http(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {body_text}") from e


def _parse_sse_line(lines: list[str]) -> tuple[str, dict] | None:
    """Parse 1 SSE block (list of lines) → (event_type, data_dict) hoặc None."""
    event_type = "message"
    data_str = ""
    for line in lines:
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_str = line[5:].strip()
    if not data_str:
        return None
    try:
        return event_type, json.loads(data_str)
    except json.JSONDecodeError:
        return event_type, {"raw": data_str}


# ── SSE consumer chạy trong thread riêng ──────────────────────────────────────

class _SseCollector(threading.Thread):
    """Thu thập SSE events vào list. Thread-safe qua lock."""

    def __init__(self, session_id: str):
        super().__init__(daemon=True)
        self.session_id = session_id
        self.events: list[tuple[str, dict]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.error: str = ""

    def stop(self):
        self._stop.set()

    def get_events(self) -> list[tuple[str, dict]]:
        with self._lock:
            return list(self.events)

    def run(self):
        url = f"{BASE_URL}/v1/sessions/{self.session_id}/stream"
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                buf: list[str] = []
                for raw in resp:
                    if self._stop.is_set():
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line == "":
                        # blank line = end of SSE block
                        if buf:
                            parsed = _parse_sse_line(buf)
                            if parsed:
                                with self._lock:
                                    self.events.append(parsed)
                            buf = []
                    else:
                        buf.append(line)
        except Exception as e:
            if not self._stop.is_set():
                self.error = str(e)


# ── Test logic ─────────────────────────────────────────────────────────────────

def run_test(base_url: str) -> bool:
    global BASE_URL
    BASE_URL = base_url

    print("=" * 60)
    print("TEST: Cancel session trong lúc waiting_for_user")
    print("=" * 60)

    # ── Step 1: Tạo session không có credentials ───────────────────────────────
    _log("Tạo session chang_login (không có credentials)...")
    try:
        created = _http("POST", "/v1/sessions", {
            "scenario": "chang_login",
            "context": {},        # không có email/password → agent phải ask
            "max_steps": 20,
        })
    except RuntimeError as e:
        _log(f"FAIL: Tạo session thất bại: {e}")
        return False

    session_id = created["session_id"]
    _log(f"Session tạo OK: {session_id}  (status={created['status']})")

    # ── Step 2: Kết nối SSE ────────────────────────────────────────────────────
    _log("Kết nối SSE stream...")
    collector = _SseCollector(session_id)
    collector.start()

    # ── Step 3: Chờ event ask ──────────────────────────────────────────────────
    _log(f"Chờ event 'ask' (timeout={WAIT_FOR_ASK_TIMEOUT_S}s)...")
    ask_received = False
    deadline = time.time() + WAIT_FOR_ASK_TIMEOUT_S
    cancelled_received = False

    while time.time() < deadline:
        for event_type, payload in collector.get_events():
            _log(f"  SSE event: type={event_type}  payload={json.dumps(payload, ensure_ascii=False)[:120]}")
            if event_type == "ask" and not ask_received:
                ask_received = True
                _log(">>> ask event nhận được. Gọi CANCEL ngay...")

                # ── Step 4: Cancel ─────────────────────────────────────────────
                try:
                    cancel_resp = _http("POST", f"/v1/sessions/{session_id}/cancel")
                    _log(f"Cancel response: {cancel_resp}")
                except RuntimeError as e:
                    _log(f"FAIL: Cancel thất bại: {e}")
                    collector.stop()
                    return False

            if event_type == "cancelled":
                cancelled_received = True

        if cancelled_received:
            break
        time.sleep(POLL_INTERVAL_S)

    collector.stop()

    if collector.error:
        _log(f"SSE error: {collector.error}")

    # ── Step 5: Kiểm tra kết quả ───────────────────────────────────────────────
    if not ask_received:
        _log("FAIL: Không nhận được event 'ask' trong timeout. Agent có thể chưa reach ask state.")
        return False

    if not cancelled_received:
        _log("FAIL: Không nhận được event 'cancelled' sau khi gọi cancel.")
        return False

    # Double-check qua REST
    try:
        sess_state = _http("GET", f"/v1/sessions/{session_id}")
        final_status = sess_state["status"]
        _log(f"Session status qua REST: {final_status}")
        if final_status != "cancelled":
            _log(f"FAIL: Status mong đợi 'cancelled', thực tế '{final_status}'")
            return False
    except RuntimeError as e:
        _log(f"WARN: Không lấy được session status: {e}")

    _log("PASS: Cancel during waiting_for_user hoạt động đúng!")
    return True


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()

    ok = run_test(args.base_url)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
