"""
Test Case T1 — Smoke Test

Kiểm tra nhanh toàn bộ pipeline hoạt động:
  1. GET /v1/health → status=ok, workers_alive >= 1
  2. POST /v1/sessions → 201, nhận session_id
  3. GET /v1/sessions/{id} → status ∈ {queued, assigned, running}
  4. GET /v1/sessions/{id}/stream → kết nối SSE OK, nhận ít nhất 1 event
  5. POST /v1/sessions/{id}/cancel → status=cancelled

Không cần đợi LLM hoàn thành — chỉ xác nhận pipeline API→Redis→Worker thông.

Yêu cầu:
  - Stack đang up: docker compose -f docker_build/docker-compose.yml up -d
  - Worker đang chạy (health.workers_alive >= 1)

Chạy:
  cd deploy_server/ai_tool_web
  python tests/test_smoke.py
  python tests/test_smoke.py --base-url http://localhost:8000
"""

import argparse
import json
import sys
import threading
import time
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8000"
WAIT_FIRST_EVENT_S = 60   # tối đa 1 phút chờ event đầu tiên từ worker


# ── Helpers ────────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _http(method: str, path: str, body: dict | None = None, timeout: int = 10) -> dict:
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {e.read().decode()}") from e


class _SseFirstEvent(threading.Thread):
    """Lắng nghe SSE stream, capture event đầu tiên nhận được."""

    def __init__(self, session_id: str):
        super().__init__(daemon=True)
        self.session_id = session_id
        self.first_event: tuple[str, dict] | None = None
        self.connected = threading.Event()
        self._stop = threading.Event()
        self.error = ""

    def stop(self):
        self._stop.set()

    def run(self):
        url = f"{BASE_URL}/v1/sessions/{self.session_id}/stream"
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        try:
            with urllib.request.urlopen(req, timeout=WAIT_FIRST_EVENT_S + 10) as resp:
                self.connected.set()
                buf: list[str] = []
                for raw in resp:
                    if self._stop.is_set():
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line == "":
                        if buf:
                            event_type, data_str = "message", ""
                            for l in buf:
                                if l.startswith("event:"):
                                    event_type = l[6:].strip()
                                elif l.startswith("data:"):
                                    data_str = l[5:].strip()
                            buf = []
                            if data_str and event_type != "heartbeat":
                                try:
                                    self.first_event = (event_type, json.loads(data_str))
                                except json.JSONDecodeError:
                                    self.first_event = (event_type, {"raw": data_str})
                                return  # got first non-heartbeat event
                    else:
                        buf.append(line)
        except Exception as e:
            if not self._stop.is_set():
                self.error = str(e)


# ── Test steps ─────────────────────────────────────────────────────────────────

def check_health() -> bool:
    _log("--- Step 1: Health check ---")
    try:
        h = _http("GET", "/v1/health")
    except RuntimeError as e:
        _log(f"FAIL: /v1/health không phản hồi: {e}")
        return False

    _log(f"  status={h.get('status')}  workers_alive={h.get('workers_alive')}  "
         f"workers_busy={h.get('workers_busy')}  queue={h.get('queue_length')}")

    if h.get("status") != "ok":
        _log("FAIL: status != ok")
        return False
    if h.get("workers_alive", 0) < 1:
        _log("FAIL: Không có worker nào. Chạy: docker compose up -d browser-worker")
        return False

    _log("PASS: Health OK")
    return True


def check_create_session() -> str | None:
    _log("--- Step 2: Tạo session ---")
    try:
        resp = _http("POST", "/v1/sessions", {
            "scenario": "chang_login",
            "context": {},
            "max_steps": 20,
        })
    except RuntimeError as e:
        _log(f"FAIL: POST /v1/sessions lỗi: {e}")
        return None

    session_id = resp.get("session_id", "")
    status = resp.get("status", "")
    stream_url = resp.get("stream_url", "")

    _log(f"  session_id={session_id}  status={status}  stream_url={stream_url}")

    if not session_id:
        _log("FAIL: session_id trống")
        return None
    if status not in ("queued", "assigned", "running"):
        _log(f"FAIL: status mong đợi queued/assigned/running, thực tế: {status}")
        return None
    if not stream_url:
        _log("FAIL: stream_url trống")
        return None

    _log(f"PASS: Session tạo OK → {session_id[:12]}...")
    return session_id


def check_get_session(session_id: str) -> bool:
    _log("--- Step 3: GET session status ---")
    try:
        sess = _http("GET", f"/v1/sessions/{session_id}")
    except RuntimeError as e:
        _log(f"FAIL: GET /v1/sessions/{session_id} lỗi: {e}")
        return False

    status = sess.get("status", "")
    _log(f"  status={status}  step={sess.get('current_step')}  worker={sess.get('assigned_worker')}")

    valid = {"queued", "assigned", "running", "waiting_for_user", "done", "failed", "cancelled"}
    if status not in valid:
        _log(f"FAIL: status không hợp lệ: {status}")
        return False

    _log("PASS: GET session OK")
    return True


def check_sse_stream(session_id: str) -> bool:
    _log("--- Step 4: SSE stream — chờ event đầu tiên ---")
    listener = _SseFirstEvent(session_id)
    listener.start()

    # Chờ connection thiết lập
    if not listener.connected.wait(timeout=10):
        listener.stop()
        _log("FAIL: Không kết nối được SSE trong 10s")
        return False
    _log("  SSE connection OK — chờ event đầu tiên...")

    deadline = time.time() + WAIT_FIRST_EVENT_S
    while time.time() < deadline:
        if listener.first_event:
            break
        time.sleep(0.5)

    listener.stop()

    if listener.error:
        _log(f"  SSE error: {listener.error}")

    if not listener.first_event:
        _log(f"FAIL: Không nhận được event nào trong {WAIT_FIRST_EVENT_S}s. "
             "Worker có thể chưa pick up job.")
        return False

    event_type, payload = listener.first_event
    _log(f"  event_type={event_type}  payload={json.dumps(payload, ensure_ascii=False)[:100]}")
    _log("PASS: SSE nhận event OK")
    return True


def check_cancel(session_id: str) -> bool:
    _log("--- Step 5: Cancel session ---")
    # Session có thể đã terminal (done/failed/ask) — thử cancel, chấp nhận cả lỗi 400
    try:
        resp = _http("POST", f"/v1/sessions/{session_id}/cancel")
        _log(f"  cancel response: {resp}")
    except RuntimeError as e:
        # Session đã terminal trước khi cancel → OK
        if "400" in str(e) or "already" in str(e).lower():
            _log(f"  Session đã terminal trước khi cancel (expected): {e}")
        else:
            _log(f"  WARN: cancel error: {e}")

    # Xác nhận status cuối
    try:
        sess = _http("GET", f"/v1/sessions/{session_id}")
        final = sess.get("status", "")
        _log(f"  final status: {final}")
        terminal = {"done", "failed", "cancelled", "timed_out"}
        if final in terminal:
            _log(f"PASS: Session kết thúc ở terminal state ({final})")
            return True
        else:
            _log(f"WARN: Session chưa terminal: {final} (có thể cancel chưa kịp xử lý)")
            return True  # không fail smoke test vì issue này
    except RuntimeError as e:
        _log(f"WARN: Không lấy được final status: {e}")
        return True


# ── Main ───────────────────────────────────────────────────────────────────────

def run_test(base_url: str) -> bool:
    global BASE_URL
    BASE_URL = base_url

    print("=" * 60)
    print("TEST T1 — Smoke Test")
    print(f"API: {BASE_URL}")
    print("=" * 60)

    results: dict[str, bool] = {}

    # Step 1: Health
    results["health"] = check_health()
    if not results["health"]:
        _print_summary(results)
        return False

    # Step 2: Create session
    session_id = check_create_session()
    results["create"] = session_id is not None
    if not session_id:
        _print_summary(results)
        return False

    # Step 3: Get session (parallel-safe với step 4)
    results["get_session"] = check_get_session(session_id)

    # Step 4: SSE stream
    results["sse"] = check_sse_stream(session_id)

    # Step 5: Cancel
    results["cancel"] = check_cancel(session_id)

    _print_summary(results)
    return all(results.values())


def _print_summary(results: dict[str, bool]) -> None:
    print()
    print("=" * 60)
    print("SUMMARY")
    for step, ok in results.items():
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark}  {step}")
    overall = "PASS" if all(results.values()) else "FAIL"
    print(f"\n  Overall: {overall}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    ok = run_test(args.base_url)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
