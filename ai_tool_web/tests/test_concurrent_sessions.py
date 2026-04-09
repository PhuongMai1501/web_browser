"""
Test: N sessions chạy song song — đo throughput, queue wait, session duration.

Kịch bản:
  - Scale workers trước khi chạy: docker compose up --scale browser-worker=N
  - Tạo N sessions cùng lúc (context={} → agent sẽ reach ask, ta cancel để kết thúc)
  - Poll health + status từng session mỗi 2s
  - Sau khi tất cả terminal → in report

Metrics đo được:
  - queue_wait_s    : thời gian từ created → assigned/running
  - session_total_s : thời gian từ created → terminal status
  - peak_busy       : số worker busy cao nhất cùng lúc
  - peak_queue      : queue length cao nhất
  - status breakdown: done/failed/cancelled/timed_out

Chạy:
  # Bước 1: scale workers
  docker compose -f docker_build/docker-compose.yml up -d --scale browser-worker=5

  # Bước 2: chạy test
  cd ai_tool_web
  PYTHONIOENCODING=utf-8 python tests/test_concurrent_sessions.py --n 5

  # Tùy chọn
  python tests/test_concurrent_sessions.py --n 5 --cancel-on-ask   (default: True)
  python tests/test_concurrent_sessions.py --n 5 --base-url http://localhost:8000
"""

import argparse
import json
import sys
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime

BASE_URL = "http://localhost:8000"
POLL_INTERVAL_S = 2
SESSION_TIMEOUT_S = 300   # timeout chờ 1 session reach terminal
TERMINAL = frozenset({"done", "failed", "cancelled", "timed_out"})


# ── Helpers ────────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _http(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {e.read().decode()}") from e


def _ts() -> float:
    return time.time()


def _fmt(seconds: float) -> str:
    return f"{seconds:.1f}s"


# ── Dữ liệu đo cho 1 session ──────────────────────────────────────────────────

@dataclass
class SessionMetrics:
    session_id: str
    created_at: float = 0.0       # wall time khi tạo
    running_at: float = 0.0       # wall time khi status chuyển sang running/assigned
    terminal_at: float = 0.0      # wall time khi đạt terminal status
    final_status: str = ""
    final_step: int = 0
    ask_received: bool = False
    cancel_sent: bool = False

    @property
    def queue_wait_s(self) -> float:
        if self.running_at and self.created_at:
            return self.running_at - self.created_at
        return 0.0

    @property
    def session_total_s(self) -> float:
        if self.terminal_at and self.created_at:
            return self.terminal_at - self.created_at
        return 0.0


# ── Health polling chạy trong background ──────────────────────────────────────

@dataclass
class HealthSnapshot:
    ts: float
    workers_alive: int
    workers_busy: int
    queue_length: int


class HealthPoller(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.snapshots: list[HealthSnapshot] = []
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                h = _http("GET", "/v1/health")
                self.snapshots.append(HealthSnapshot(
                    ts=_ts(),
                    workers_alive=h.get("workers_alive", 0),
                    workers_busy=h.get("workers_busy", 0),
                    queue_length=h.get("queue_length", 0),
                ))
            except Exception:
                pass
            self._stop.wait(POLL_INTERVAL_S)

    @property
    def peak_busy(self) -> int:
        return max((s.workers_busy for s in self.snapshots), default=0)

    @property
    def peak_queue(self) -> int:
        return max((s.queue_length for s in self.snapshots), default=0)


# ── Session monitor: theo dõi 1 session ───────────────────────────────────────

class SessionMonitor(threading.Thread):
    def __init__(self, metrics: SessionMetrics, cancel_on_ask: bool):
        super().__init__(daemon=True)
        self.m = metrics
        self.cancel_on_ask = cancel_on_ask
        self.done = threading.Event()

    def run(self):
        sid = self.m.session_id
        deadline = _ts() + SESSION_TIMEOUT_S

        while _ts() < deadline:
            try:
                sess = _http("GET", f"/v1/sessions/{sid}")
                status = sess.get("status", "unknown")
                step = int(sess.get("current_step", 0))
                self.m.final_step = step

                # Ghi thời điểm bắt đầu chạy
                if status in ("running", "assigned") and not self.m.running_at:
                    self.m.running_at = _ts()

                # Nếu đang ask và cần cancel
                if status == "waiting_for_user" and not self.m.running_at:
                    self.m.running_at = _ts()

                if status == "waiting_for_user" and not self.m.cancel_sent and self.cancel_on_ask:
                    self.m.ask_received = True
                    try:
                        _http("POST", f"/v1/sessions/{sid}/cancel")
                        self.m.cancel_sent = True
                        _log(f"  [{sid[:8]}] ask → cancelled (step {step})")
                    except RuntimeError:
                        pass

                if status in TERMINAL:
                    self.m.final_status = status
                    self.m.terminal_at = _ts()
                    self.done.set()
                    return

            except RuntimeError:
                pass

            time.sleep(POLL_INTERVAL_S)

        # Timeout
        self.m.final_status = "timeout_in_test"
        self.m.terminal_at = _ts()
        self.done.set()


# ── Main test logic ────────────────────────────────────────────────────────────

def run_test(n: int, cancel_on_ask: bool) -> bool:
    print("=" * 65)
    print(f"TEST: {n} Concurrent Sessions")
    print(f"API: {BASE_URL}   cancel_on_ask={cancel_on_ask}")
    print("=" * 65)

    # Kiểm tra health trước
    try:
        h = _http("GET", "/v1/health")
        _log(f"Health: workers_alive={h['workers_alive']} busy={h['workers_busy']} queue={h['queue_length']}")
        if h["workers_alive"] == 0:
            _log("FAIL: Không có worker nào đang chạy. Chạy lệnh scale trước:")
            _log(f"  docker compose up -d --scale browser-worker={n}")
            return False
        if h["workers_alive"] < n:
            _log(f"WARN: Chỉ có {h['workers_alive']} worker cho {n} sessions — một số session sẽ phải chờ queue")
    except RuntimeError as e:
        _log(f"FAIL: API không phản hồi: {e}")
        return False

    # Bắt đầu poll health
    health_poller = HealthPoller()
    health_poller.start()

    # Tạo N sessions cùng lúc
    _log(f"Tạo {n} sessions đồng thời...")
    all_metrics: list[SessionMetrics] = []
    create_errors = 0

    t_create_start = _ts()
    for i in range(n):
        try:
            resp = _http("POST", "/v1/sessions", {
                "scenario": "chang_login",
                "context": {},        # không có credentials → agent sẽ ask
                "max_steps": 20,
            })
            m = SessionMetrics(
                session_id=resp["session_id"],
                created_at=_ts(),
            )
            all_metrics.append(m)
            _log(f"  [{i+1}/{n}] Session {m.session_id[:8]}... queued (pos={resp.get('queue_position')})")
        except RuntimeError as e:
            _log(f"  [{i+1}/{n}] FAIL tạo session: {e}")
            create_errors += 1

    t_create_end = _ts()
    _log(f"Đã tạo {len(all_metrics)}/{n} sessions trong {_fmt(t_create_end - t_create_start)}")

    if not all_metrics:
        _log("FAIL: Không tạo được session nào")
        health_poller.stop()
        return False

    # Chạy monitor cho từng session
    monitors = [SessionMonitor(m, cancel_on_ask) for m in all_metrics]
    for mon in monitors:
        mon.start()

    # Chờ tất cả xong — print progress định kỳ
    t_all_start = _ts()
    while True:
        done_count = sum(1 for mon in monitors if mon.done.is_set())
        elapsed = _ts() - t_all_start
        h_now = health_poller.snapshots[-1] if health_poller.snapshots else None
        busy_str = f"workers_busy={h_now.workers_busy}" if h_now else ""
        queue_str = f"queue={h_now.queue_length}" if h_now else ""
        _log(f"Progress: {done_count}/{len(monitors)} done  {busy_str}  {queue_str}  elapsed={_fmt(elapsed)}")

        if done_count == len(monitors):
            break
        if elapsed > SESSION_TIMEOUT_S + 30:
            _log("WARN: Test timeout — một số session chưa kết thúc")
            break
        time.sleep(5)

    health_poller.stop()
    t_all_end = _ts()

    # ── Report ────────────────────────────────────────────────────────────────
    print()
    print("=" * 65)
    print("REPORT")
    print("=" * 65)

    # Per-session
    print(f"\n{'Session':10}  {'Status':18}  {'Steps':5}  {'Queue wait':12}  {'Total':10}  {'Ask→Cancel'}")
    print("-" * 70)
    queue_waits = []
    totals = []
    status_counts: dict[str, int] = {}
    for m in all_metrics:
        sid_short = m.session_id[:8]
        ask_flag = "yes" if m.ask_received else "-"
        qw = _fmt(m.queue_wait_s) if m.queue_wait_s else "-"
        tot = _fmt(m.session_total_s) if m.session_total_s else "-"
        print(f"{sid_short:10}  {m.final_status:18}  {m.final_step:5}  {qw:12}  {tot:10}  {ask_flag}")
        if m.queue_wait_s:
            queue_waits.append(m.queue_wait_s)
        if m.session_total_s:
            totals.append(m.session_total_s)
        status_counts[m.final_status] = status_counts.get(m.final_status, 0) + 1

    # Summary
    print()
    print("SUMMARY")
    print(f"  Sessions created   : {len(all_metrics)}/{n}")
    print(f"  Create errors      : {create_errors}")
    print(f"  Status breakdown   : {status_counts}")
    print(f"  Peak workers busy  : {health_poller.peak_busy}")
    print(f"  Peak queue length  : {health_poller.peak_queue}")
    if queue_waits:
        print(f"  Queue wait         : min={_fmt(min(queue_waits))}  max={_fmt(max(queue_waits))}  avg={_fmt(sum(queue_waits)/len(queue_waits))}")
    if totals:
        print(f"  Session total time : min={_fmt(min(totals))}  max={_fmt(max(totals))}  avg={_fmt(sum(totals)/len(totals))}")
    print(f"  Wall time (all)    : {_fmt(t_all_end - t_all_start)}")
    print()

    # Ghi raw data ra file để phân tích sau
    report = {
        "test_time": datetime.now().isoformat(),
        "n_sessions": n,
        "cancel_on_ask": cancel_on_ask,
        "sessions": [
            {
                "session_id": m.session_id,
                "final_status": m.final_status,
                "final_step": m.final_step,
                "queue_wait_s": round(m.queue_wait_s, 2),
                "session_total_s": round(m.session_total_s, 2),
                "ask_received": m.ask_received,
            }
            for m in all_metrics
        ],
        "health_timeline": [
            {"ts": round(s.ts - t_all_start, 1), "busy": s.workers_busy, "queue": s.queue_length}
            for s in health_poller.snapshots
        ],
        "summary": {
            "peak_busy": health_poller.peak_busy,
            "peak_queue": health_poller.peak_queue,
            "queue_wait_avg_s": round(sum(queue_waits)/len(queue_waits), 2) if queue_waits else 0,
            "total_avg_s": round(sum(totals)/len(totals), 2) if totals else 0,
            "wall_time_s": round(t_all_end - t_all_start, 2),
            "status_counts": status_counts,
        }
    }
    report_path = f"tests/report_concurrent_{n}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        _log(f"Raw report saved: {report_path}")
    except Exception as e:
        _log(f"WARN: Không lưu được report: {e}")

    # Pass/fail: tất cả session phải đạt terminal status (không timeout trong test)
    failed = [m for m in all_metrics if m.final_status == "timeout_in_test"]
    if failed:
        _log(f"FAIL: {len(failed)} session không kết thúc trong timeout")
        return False

    _log("PASS: Tất cả sessions đã kết thúc")
    return True


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=5, help="Số sessions song song")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--cancel-on-ask", action=argparse.BooleanOptionalAction,
                        default=True, help="Tự cancel khi agent ask (default: True)")
    args = parser.parse_args()

    global BASE_URL
    BASE_URL = args.base_url

    ok = run_test(args.n, args.cancel_on_ask)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
