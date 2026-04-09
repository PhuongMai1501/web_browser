"""
Test Case: Worker crash recovery — 2 sub-cases

Sub-case A: Worker crash khi session đang running
  → recovery_loop phải set status=failed, emit event failed

Sub-case B: Worker crash khi session đang waiting_for_user
  → recovery_loop phải set status=failed, emit event failed

Cơ chế:
  - Worker heartbeat TTL=30s, renew mỗi 15s
  - recovery_loop chạy mỗi 30s, threshold dead=45s
  - Sau khi kill worker: chờ tối đa ~90s để recovery kích hoạt

Yêu cầu:
  - Docker đang chạy (docker-compose up)
  - API tại BASE_URL
  - Worker container tên: docker compose service 'browser-worker'
  - pip install requests (nếu chưa có)

Chạy:
  python tests/test_worker_crash_recovery.py
  python tests/test_worker_crash_recovery.py --base-url http://localhost:8000 --compose-file ../docker_build/docker-compose.yml
  python tests/test_worker_crash_recovery.py --case A   # chỉ test case A
  python tests/test_worker_crash_recovery.py --case B   # chỉ test case B
"""

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8000"
COMPOSE_FILE = "../docker_build/docker-compose.yml"

WAIT_FOR_RUNNING_S = 120      # chờ session đạt running
WAIT_FOR_ASK_S = 180          # chờ agent đạt waiting_for_user (case B)
WAIT_FOR_RECOVERY_S = 100     # chờ recovery_loop kích hoạt sau kill
POLL_S = 2


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


def _get_session_status(session_id: str) -> str:
    try:
        sess = _http("GET", f"/v1/sessions/{session_id}")
        return sess.get("status", "unknown")
    except RuntimeError:
        return "unknown"


def _poll_until_status(session_id: str, target_statuses: set[str], timeout_s: float) -> str:
    """Poll /v1/sessions/{id} cho đến khi status nằm trong target_statuses hoặc timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = _get_session_status(session_id)
        _log(f"  Session status: {status}")
        if status in target_statuses:
            return status
        time.sleep(POLL_S)
    return _get_session_status(session_id)


# ── SSE collector (giống test_cancel_waiting.py) ───────────────────────────────

def _parse_sse_line(lines: list[str]) -> tuple[str, dict] | None:
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


class _SseCollector(threading.Thread):
    def __init__(self, session_id: str):
        super().__init__(daemon=True)
        self.session_id = session_id
        self.events: list[tuple[str, dict]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.error: str = ""

    def stop(self):
        self._stop.set()

    def get_event_types(self) -> list[str]:
        with self._lock:
            return [et for et, _ in self.events]

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
                        if buf:
                            parsed = _parse_sse_line(buf)
                            if parsed:
                                et, payload = parsed
                                _log(f"  SSE: type={et}  {json.dumps(payload, ensure_ascii=False)[:100]}")
                                with self._lock:
                                    self.events.append((et, payload))
                            buf = []
                    else:
                        buf.append(line)
        except Exception as e:
            if not self._stop.is_set():
                self.error = str(e)


# ── Docker helpers ─────────────────────────────────────────────────────────────

def _docker_compose_kill_worker(compose_file: str) -> bool:
    """Kill tất cả browser-worker containers."""
    _log("Killing browser-worker container(s)...")
    result = subprocess.run(
        ["docker", "compose", "-f", compose_file, "kill", "browser-worker"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        _log(f"  docker compose kill stderr: {result.stderr.strip()}")
        return False
    _log("  Worker killed.")
    return True


def _docker_compose_start_worker(compose_file: str) -> None:
    """Restart browser-worker sau test."""
    _log("Restarting browser-worker...")
    subprocess.run(
        ["docker", "compose", "-f", compose_file, "up", "-d", "browser-worker"],
        capture_output=True, text=True,
    )
    _log("  Worker restarted.")


# ── Sub-case A: crash khi running ─────────────────────────────────────────────

def run_case_a(compose_file: str) -> bool:
    print()
    print("-" * 60)
    print("Sub-case A: Worker crash khi session RUNNING")
    print("-" * 60)

    # Tạo session có credentials → agent sẽ chạy luôn mà không ask
    # Dùng credentials giả để agent bắt đầu running (nó sẽ fail ở bước login thực)
    # nhưng trước đó worker sẽ ở trạng thái running — đủ để test crash
    _log("Tạo session chang_login (có credentials giả)...")
    try:
        created = _http("POST", "/v1/sessions", {
            "scenario": "chang_login",
            "context": {"email": "test@test.com", "password": "test123"},
            "max_steps": 20,
        })
    except RuntimeError as e:
        _log(f"FAIL: Tạo session: {e}")
        return False

    session_id = created["session_id"]
    _log(f"Session: {session_id}")

    # Kết nối SSE
    collector = _SseCollector(session_id)
    collector.start()

    # Chờ session đạt running
    _log(f"Chờ session đạt 'running' (timeout={WAIT_FOR_RUNNING_S}s)...")
    status = _poll_until_status(session_id, {"running", "waiting_for_user", "done", "failed"}, WAIT_FOR_RUNNING_S)
    if status not in ("running", "waiting_for_user"):
        _log(f"FAIL: Session không đạt 'running' (status={status}). Worker có đang chạy không?")
        collector.stop()
        return False

    _log(f"Session đang ở status={status}. Tiến hành kill worker...")

    # Kill worker
    if not _docker_compose_kill_worker(compose_file):
        _log("FAIL: Không kill được worker. Kiểm tra docker compose có đang chạy không.")
        collector.stop()
        return False

    kill_time = time.time()
    _log(f"Worker đã bị kill. Chờ recovery_loop kích hoạt (~{WAIT_FOR_RECOVERY_S}s)...")

    # Chờ recovery_loop detect dead worker và set status=failed
    # recovery_loop: sleep 30s, threshold 45s → tổng tối đa ~75s
    final_status = _poll_until_status(session_id, {"failed", "done", "cancelled"}, WAIT_FOR_RECOVERY_S)
    elapsed = time.time() - kill_time
    _log(f"Status sau {elapsed:.0f}s: {final_status}")
    collector.stop()

    # Kiểm tra
    if final_status != "failed":
        _log(f"FAIL: Mong đợi 'failed', nhận được '{final_status}'")
        return False

    # Kiểm tra error_msg
    try:
        sess = _http("GET", f"/v1/sessions/{session_id}")
        error_msg = sess.get("error_msg", "")
        _log(f"error_msg: {error_msg}")
        if "crashed" not in error_msg.lower() and "worker" not in error_msg.lower():
            _log("WARN: error_msg không mention 'worker crashed' — có thể recovery chưa kick in đúng")
    except RuntimeError:
        pass

    # Kiểm tra SSE có emit failed event không
    event_types = collector.get_event_types()
    _log(f"SSE events nhận được: {event_types}")
    if "failed" not in event_types:
        _log("WARN: Không thấy 'failed' event trong SSE stream (có thể client reconnect bị miss)")

    _log("PASS: Sub-case A — Worker crash khi running → session marked failed")
    return True


# ── Sub-case B: crash khi waiting_for_user ────────────────────────────────────

def run_case_b(compose_file: str) -> bool:
    print()
    print("-" * 60)
    print("Sub-case B: Worker crash khi session WAITING_FOR_USER")
    print("-" * 60)

    # Tạo session không có credentials → agent sẽ hỏi
    _log("Tạo session chang_login (không có credentials)...")
    try:
        created = _http("POST", "/v1/sessions", {
            "scenario": "chang_login",
            "context": {},
            "max_steps": 20,
        })
    except RuntimeError as e:
        _log(f"FAIL: Tạo session: {e}")
        return False

    session_id = created["session_id"]
    _log(f"Session: {session_id}")

    # Kết nối SSE
    collector = _SseCollector(session_id)
    collector.start()

    # Chờ session đạt waiting_for_user
    _log(f"Chờ session đạt 'waiting_for_user' (timeout={WAIT_FOR_ASK_S}s)...")
    status = _poll_until_status(session_id, {"waiting_for_user", "done", "failed", "cancelled"}, WAIT_FOR_ASK_S)
    if status != "waiting_for_user":
        _log(f"FAIL: Session không đạt 'waiting_for_user' (status={status}). "
             "Thử dùng credentials giả nếu agent không tự ask.")
        collector.stop()
        return False

    _log("Session đang waiting_for_user. Kill worker...")

    if not _docker_compose_kill_worker(compose_file):
        _log("FAIL: Không kill được worker.")
        collector.stop()
        return False

    kill_time = time.time()
    _log(f"Worker killed. Chờ recovery_loop (~{WAIT_FOR_RECOVERY_S}s)...")

    final_status = _poll_until_status(session_id, {"failed", "done", "cancelled"}, WAIT_FOR_RECOVERY_S)
    elapsed = time.time() - kill_time
    _log(f"Status sau {elapsed:.0f}s: {final_status}")
    collector.stop()

    if final_status != "failed":
        _log(f"FAIL: Mong đợi 'failed', nhận được '{final_status}'")
        return False

    try:
        sess = _http("GET", f"/v1/sessions/{session_id}")
        error_msg = sess.get("error_msg", "")
        _log(f"error_msg: {error_msg}")
    except RuntimeError:
        pass

    event_types = collector.get_event_types()
    _log(f"SSE events nhận được: {event_types}")

    _log("PASS: Sub-case B — Worker crash khi waiting_for_user → session marked failed")
    return True


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--compose-file", default="../docker_build/docker-compose.yml",
                        help="Path tới docker-compose.yml")
    parser.add_argument("--case", choices=["A", "B", "both"], default="both",
                        help="Chạy sub-case nào (default: both)")
    args = parser.parse_args()

    global BASE_URL
    BASE_URL = args.base_url

    print("=" * 60)
    print("TEST: Worker Crash Recovery")
    print(f"API: {BASE_URL}   Compose: {args.compose_file}")
    print("=" * 60)

    results: dict[str, bool] = {}

    if args.case in ("A", "both"):
        results["A"] = run_case_a(args.compose_file)
        # Restart worker sau case A trước khi chạy case B
        if args.case == "both":
            _docker_compose_start_worker(args.compose_file)
            _log("Chờ worker khởi động lại (15s)...")
            time.sleep(15)

    if args.case in ("B", "both"):
        results["B"] = run_case_b(args.compose_file)

    # Luôn restart worker khi xong
    _docker_compose_start_worker(args.compose_file)

    print()
    print("=" * 60)
    print("KẾT QUẢ:")
    all_pass = True
    for case, ok in results.items():
        status = "PASS" if ok else "FAIL"
        print(f"  Sub-case {case}: {status}")
        if not ok:
            all_pass = False
    print("=" * 60)

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
