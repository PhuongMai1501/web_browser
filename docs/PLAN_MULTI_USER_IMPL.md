# Multi-User Implementation Plan (v2)

> Quyết định đã chốt:
> - 1 worker = 1 session (1 Chrome process)
> - Artifact storage: shared Docker volume
> - Redis: self-host
> - Phase 0 đã hoàn thành (1-user ổn định)
>
> v2: tích hợp corrections từ issue_plan_multi_user.md (16 issues)

---

## Kiến trúc đích

```
React UI
   │  HTTP/SSE
FastAPI API  (routes/, sse_stream, recovery — không chạy browser)
   │  Redis Pub/Sub  → stream events về SSE
   │  Redis List     → job queue (RPUSH/BLPOP FIFO) + resume signal
   │
Redis ─── session state / event buffer / job queue / worker registry
   │
Browser Worker × N  (1 process = 1 Chrome = 1 session)
   │  subprocess
agent-browser CLI + LLM_base engine
   │
Shared Volume  (artifacts: path tương đối từ ARTIFACTS_ROOT)
```

---

## Cấu trúc thư mục đích

```
ai_tool_web/
├── config.py                   constants (Redis URL, TTL, paths)
├── models.py                   Pydantic schemas (giữ, bổ sung)
│
├── api/                        MỚI Phase 1b — tách từ api.py
│   ├── __init__.py
│   ├── app.py                  FastAPI setup, middleware, startup tasks
│   ├── routes/
│   │   ├── sessions.py         POST /v1/sessions, GET /v1/sessions/{id}
│   │   ├── stream.py           GET /v1/sessions/{id}/stream (SSE)
│   │   ├── resume.py           POST /v1/sessions/{id}/resume
│   │   ├── cancel.py           POST /v1/sessions/{id}/cancel
│   │   ├── browser.py          POST /v1/browser/reset
│   │   └── health.py           GET /v1/health
│   ├── sse_stream.py           SSE generator (đọc Redis Pub/Sub)
│   ├── artifact_service.py     Serve screenshot từ shared volume
│   └── recovery.py             Background: detect dead workers, requeue

├── state/                      MỚI Phase 1b
│   ├── __init__.py
│   ├── redis_client.py         Redis connection pool (asyncio)
│   ├── session_store.py        CRUD session state trong Redis
│   ├── event_store.py          PUBLISH event / SUBSCRIBE channel / buffer
│   ├── job_queue.py            RPUSH/BLPOP pending_jobs (FIFO)
│   └── worker_registry.py     Heartbeat, ownership, slot tracking

├── worker/                     MỚI Phase 1a skeleton → Phase 1b live
│   ├── __init__.py
│   ├── browser_worker.py       Entry: python -m worker.browser_worker --id w1
│   ├── job_handler.py          Chạy agent loop 1 session (tách từ api.py)
│   └── heartbeat.py            Gửi heartbeat + renew session lock định kỳ

└── api.py                      Phase 1a: giữ nguyên
                                Phase 1b: thay bằng api/app.py, file này deprecated
```

**Lưu ý `session_manager.py`:** Phase 1a giữ nguyên. Phase 1b: đánh dấu `# DEPRECATED — replaced by state/session_store.py` và không gọi thêm trong code mới. Xóa sau khi migration xong.

---

## Convention thống nhất cho Redis queue

**Tất cả queue dùng RPUSH + BLPOP (FIFO — đến trước xử lý trước):**

```
# Job queue:
API   → RPUSH pending_jobs {session_id}
Worker ← BLPOP pending_jobs timeout=30s

# Resume signal:
API   → RPUSH resume:{session_id} {json}
Worker ← BLPOP resume:{session_id} timeout=300s

# Cancel signal (gửi vào resume queue với type=cancel):
API   → RPUSH resume:{session_id} {"type": "cancel"}
Worker đọc được → cleanup → emit cancelled
```

---

## Session State Schema (Redis Hash)

```
Key: session:{session_id}
TTL: 1800s (30 phút từ lần update cuối)

Fields:
  session_id          string
  status              xem bảng trạng thái bên dưới
  scenario            chang_login | custom
  current_step        int
  max_steps           int
  created_at          ISO timestamp
  updated_at          ISO timestamp (refresh mỗi khi update)
  started_at          ISO timestamp (khi worker bắt đầu chạy)
  finished_at         ISO timestamp (khi done/failed/cancelled)
  assigned_worker     worker-id (rỗng khi queued)
  last_event_id       int (event_id của event cuối cùng đã push)
  ask_deadline_at     ISO timestamp (set khi status=waiting_for_user)
  cancel_requested    0 | 1
  artifact_root       relative path từ ARTIFACTS_ROOT, ví dụ: 2026/04/08/sess-001
  scenario_config     JSON string (goal, url, max_steps — snapshot lúc tạo)
  error_msg           string (khi status=failed/timed_out)
  client_id           string (IP hoặc user identifier, Phase 3 auth)
```

---

## Session Status — State Machine

```
queued
  ↓ worker lấy job
assigned
  ↓ worker bắt đầu chạy browser
running
  ↓ LLM cần hỏi user
waiting_for_user ──→ (user trả lời) ──→ running
  ↓ timeout 300s
timed_out

running ──→ done
running ──→ failed
running ──→ cancelled  (cancel_requested=1)
waiting_for_user ──→ cancelled  (API RPUSH cancel signal)
assigned ──→ cancelled  (worker chưa start, cancel được)
queued ──→ cancelled  (chưa assign, cancel được)
```

**Bỏ `blocked`** (mơ hồ). Dùng `waiting_for_user` cho ask-resume. Các lý do block khác (CAPTCHA, website chặn) → `failed` với `error_msg` rõ ràng.

---

## Event Schema — Chuẩn hóa

Tất cả events có chung envelope:

```json
{
  "event_id": 12,
  "session_id": "sess-001",
  "type": "step_completed",
  "ts": "2026-04-08T10:30:15Z",
  "payload": {}
}
```

`event_id`: tăng dần theo session (bắt đầu từ 1), **không phải step number**. Dùng cho SSE `id:` và Last-Event-ID reconnect.

| Type | Khi nào | Payload chính |
|------|---------|--------------|
| `session_queued` | Session vừa tạo | scenario, max_steps |
| `session_assigned` | Worker nhận job | worker_id |
| `step` | Mỗi bước agent | step, action, ref, reason, screenshot_url |
| `ask` | LLM cần hỏi | message, ask_type, ask_deadline_at |
| `user_resumed` | User gửi answer | answer (masked nếu password) |
| `done` | Hoàn thành | total_steps, duration_seconds |
| `failed` | Lỗi không recover | code, message |
| `cancelled` | Bị huỷ | reason |
| `timed_out` | Ask timeout | elapsed_seconds |
| `heartbeat` | Keep-alive SSE | — (không lưu buffer) |

**SSE reconnect buffer**: `session:{id}:buffer` lưu tối đa 50 events cuối (trừ heartbeat). Replay theo `event_id > last_event_id` từ `Last-Event-ID` header.

---

## Session Lock — TTL ngắn + Renewal

```python
# Worker lock session khi nhận job:
SET lock:session:{id}  {worker_id}  NX  EX 60

# heartbeat.py renew lock mỗi 15s:
while session đang chạy:
    redis.set(f"lock:session:{id}", worker_id, ex=60, xx=True)  # xx=chỉ update nếu tồn tại
    await asyncio.sleep(15)

# Nếu renewal fail (lock đã mất → không set được):
→ worker dừng session, emit failed
```

**Không dùng TTL 600s.** Lock TTL = 60s, renewal 15s. Session tự bảo vệ chính mình.

---

## Worker Registry Schema

```
Key: worker:{worker_id}
TTL: 30s (refresh mỗi 10s bởi heartbeat.py)

Value (JSON):
{
  "worker_id": "worker-1",
  "status": "idle" | "busy",
  "current_session": "sess-001" | null,   ← THÊM: để recovery biết session nào bị orphan
  "started_at": "...",
  "last_heartbeat": "..."
}
```

---

## Cancel Semantics

```
API POST /cancel:
  1. Set session.cancel_requested = 1
  2. RPUSH resume:{session_id} {"type": "cancel"}  ← unblock worker đang chờ ask

Worker — check cancel:
  - Đầu mỗi step: đọc cancel_requested → nếu 1 → dừng, emit cancelled
  - Sau BLPOP resume: kiểm tra {"type": "cancel"} → dừng, emit cancelled
  - Cleanup browser trước khi exit
```

---

## Worker Crash Recovery — Rule cứng

```
Nếu API detect worker mất heartbeat (> 45s không update):

  Session đang queued hoặc assigned-but-not-started:
    → RPUSH pending_jobs {session_id}  (requeue)
    → Xóa assigned_worker, set status=queued

  Session đang running hoặc waiting_for_user:
    → KHÔNG requeue (browser state đã mất)
    → Set status=failed, error_msg="Worker crashed mid-session"
    → Emit failed event → UI hiển thị, user tạo session mới
```

Tránh "zombie session" — trung thực hơn là requeue mù.

---

## Job Dispatch — Always Queue (không reject sớm)

```python
# POST /v1/sessions:
# KHÔNG reject nếu không có worker rảnh ngay
# Luôn tạo session + push queue

sess = create_session(...)
redis.rpush("pending_jobs", session_id)

# Response trả thêm queue_position (advisory, không binding):
return {
  "session_id": ...,
  "status": "queued",
  "queue_position": redis.llen("pending_jobs"),
  "stream_url": ...
}
```

Từ chối chỉ khi vượt ngưỡng cứng (ví dụ: `pending_jobs > 100`).

---

## Artifact Path — Relative từ ARTIFACTS_ROOT

```python
# Worker lưu path tương đối:
artifact_root = f"{YYYY}/{MM}/{DD}/{session_id}"
redis.hset(f"session:{id}:screenshots", step_n, f"{artifact_root}/step_{n:02d}.png")

# API serve:
rel_path = redis.hget(f"session:{id}:screenshots", step_n)
abs_path = ARTIFACTS_ROOT / rel_path
return FileResponse(abs_path)
```

Tránh absolute path khác nhau giữa container API và worker.

---

## Redis Key Cleanup Policy

| Key | TTL | Xóa khi |
|-----|-----|---------|
| `session:{id}` | 1800s (reset mỗi update) | TTL expire |
| `session:{id}:buffer` | 1800s | TTL expire |
| `session:{id}:screenshots` | 1800s | TTL expire |
| `lock:session:{id}` | 60s | renewal dừng khi session xong |
| `resume:{session_id}` | 310s (ask timeout + buffer) | sau BLPOP hoặc TTL |
| `worker:{id}` | 30s | TTL expire nếu heartbeat dừng |

Worker tự xóa `lock` và `resume` khi session kết thúc. Background task trong API quét keys mồ côi mỗi 60s.

---

## Phase 1a — Chuẩn hóa interface (không thay đổi behavior)

**Files tạo mới:**

`config.py`:
```python
REDIS_URL         = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SESSION_TTL_S     = 1800
ASK_TIMEOUT_S     = 300
LOCK_TTL_S        = 60
LOCK_RENEW_S      = 15
MAX_STEPS_CAP     = 30
ARTIFACTS_ROOT    = Path(os.getenv("ARTIFACTS_ROOT", "/app/LLM_base/artifacts"))
```

`worker/job_handler.py`:
- Move toàn bộ `_run_agent_sync()` từ `api.py`
- Interface: `run_job(sess, req, api_key, loop) -> None`
- Không thay đổi logic

**Files sửa:**

`api.py`: import `job_handler.run_job` thay inline function

**Checklist Phase 1a:**
- [x] Tạo `config.py`
- [x] Tạo `worker/__init__.py`, `worker/job_handler.py`
- [x] Sửa `api.py` import từ job_handler
- [ ] Test: behavior y hệt, không có Redis

---

## Phase 1b — Redis + Worker Process Riêng

**Files tạo mới:**

`state/redis_client.py` — connection pool:
```python
from redis.asyncio import Redis, ConnectionPool

_pool: ConnectionPool | None = None

def get_redis() -> Redis:
    global _pool
    if _pool is None:
        _pool = ConnectionPool.from_url(config.REDIS_URL, decode_responses=True)
    return Redis(connection_pool=_pool)
```

`state/session_store.py` — CRUD session:
```python
async def create(session_id, scenario, max_steps, scenario_config) -> None
async def get(session_id) -> dict | None
async def update(session_id, **fields) -> None   # refresh TTL
async def get_by_worker(worker_id) -> list[dict]
```

`state/event_store.py` — push/subscribe:
```python
async def push_event(session_id, event: dict) -> int:
    # assign event_id (INCR session:{id}:event_seq)
    # PUBLISH session:{id}:events {json}
    # RPUSH session:{id}:buffer {json}
    # LTRIM session:{id}:buffer 0 49
    # EXPIRE session:{id}:buffer 1800
    return event_id

async def subscribe(session_id) -> AsyncIterator[dict]:
    # async generator đọc từ SUBSCRIBE channel
```

`state/job_queue.py`:
```python
async def push_job(session_id: str) -> int:     # RPUSH pending_jobs
async def pop_job(timeout=30) -> str | None:    # BLPOP pending_jobs
async def queue_length() -> int:               # LLEN pending_jobs
```

`state/worker_registry.py`:
```python
async def register(worker_id: str, info: dict) -> None   # SETEX worker:{id} 30 json
async def get_all() -> list[dict]
async def get_worker_for_session(session_id) -> str | None
async def mark_dead(worker_id: str) -> None
```

`api/sse_stream.py` — SSE generator mới:
```python
async def sse_generator(session_id, last_event_id):
    # 1. Replay buffer: lrange session:{id}:buffer → filter event_id > last_event_id
    # 2. Subscribe live: async for event in event_store.subscribe(session_id)
    #    → yield SSE, break khi done/failed/cancelled
    #    → heartbeat mỗi 15s nếu không có event
```

`api/recovery.py` — background task:
```python
async def recovery_loop():
    while True:
        await asyncio.sleep(30)
        dead = await worker_registry.find_dead(threshold_s=45)
        for worker_id in dead:
            sessions = await session_store.get_by_worker(worker_id)
            for sess in sessions:
                if sess["status"] in ("queued", "assigned"):
                    await job_queue.push_job(sess["session_id"])
                    await session_store.update(sess["session_id"], status="queued", assigned_worker="")
                elif sess["status"] in ("running", "waiting_for_user"):
                    await session_store.update(sess["session_id"], status="failed",
                                               error_msg="Worker crashed mid-session")
                    await event_store.push_event(sess["session_id"],
                                                 {"type": "failed", "payload": {"message": "Worker crashed"}})
            await worker_registry.mark_dead(worker_id)
```

`worker/browser_worker.py`:
```python
async def main(worker_id: str):
    redis = get_redis()
    asyncio.create_task(heartbeat.run(worker_id, redis))

    while True:
        result = await redis.blpop("pending_jobs", timeout=30)
        if result is None:
            continue
        session_id = result[1]

        # Lock session
        locked = await redis.set(f"lock:session:{session_id}", worker_id, nx=True, ex=60)
        if not locked:
            continue

        # Update registry
        await worker_registry.update(worker_id, status="busy", current_session=session_id)
        await session_store.update(session_id, status="running", assigned_worker=worker_id, started_at=now())

        # Chạy job (blocking)
        await asyncio.to_thread(job_handler.run_job, session_id, redis, worker_id)

        # Cleanup
        await redis.delete(f"lock:session:{session_id}")
        await worker_registry.update(worker_id, status="idle", current_session=None)
```

`worker/heartbeat.py`:
```python
async def run(worker_id: str, redis):
    while True:
        # Refresh worker registry
        await worker_registry.register(worker_id, {...})
        # Renew lock của session đang giữ (nếu có)
        current = await worker_registry.get_current_session(worker_id)
        if current:
            await redis.set(f"lock:session:{current}", worker_id, ex=60, xx=True)
        await asyncio.sleep(LOCK_RENEW_S)  # 15s
```

`Dockerfile.worker`:
```dockerfile
FROM python:3.11-slim
# + Node.js + Chrome (same as main Dockerfile)
# CMD: python -m worker.browser_worker --id ${WORKER_ID}
```

**`docker-compose.yml` Phase 1b:**
```yaml
services:
  redis:
    image: redis:7-alpine
    volumes: [redis_data:/data]

  api:
    build: .
    command: uvicorn api.app:app --host 0.0.0.0 --port 8000 --workers 2
    environment:
      REDIS_URL: redis://redis:6379/0
      ARTIFACTS_ROOT: /artifacts
    volumes: [artifacts:/artifacts]
    depends_on: [redis]
    ports: ["8000:8000"]

  browser-worker:
    build:
      context: .
      dockerfile: Dockerfile.worker
    environment:
      REDIS_URL: redis://redis:6379/0
      ARTIFACTS_ROOT: /artifacts
      OPENAI_API_KEY: ${OPENAI_API_KEY}
      WORKER_ID: worker-1
    volumes: [artifacts:/artifacts]
    shm_size: "2gb"
    depends_on: [redis]

volumes:
  redis_data:
  artifacts:
```

**Checklist Phase 1b:**
- [x] `state/redis_client.py`
- [x] `state/session_store.py` (schema đầy đủ 16 fields)
- [x] `state/event_store.py` (event_id tăng dần, buffer, pub/sub)
- [x] `state/job_queue.py` (RPUSH/BLPOP FIFO)
- [x] `state/worker_registry.py` (heartbeat + current_session)
- [x] `api/app.py`, `api/routes/*`, `api/sse_stream.py`
- [x] `api/artifact_service.py`
- [x] `api/recovery.py` (dead worker detection)
- [x] `worker/browser_worker.py` (lock + cancel check)
- [x] `worker/job_handler.py` (cancel_requested check mỗi step)
- [x] `worker/heartbeat.py` (renew lock + worker TTL)
- [x] `Dockerfile.worker`
- [x] `docker-compose.yml` (redis + api + 1 worker)
- [x] Đánh dấu `session_manager.py` DEPRECATED
- [x] Test: full flow ask/resume, cancel, worker restart

---

## Phase 2 — Multiple Workers

**Thêm:**
- Slot check: worker chỉ nhận job nếu `current_session == null`
- Scale: `docker compose up --scale browser-worker=3` (dev/local)
- Production/orchestrator: replica do platform quản lý (Swarm/K8s)
- API `POST /v1/sessions` trả `queue_position` (advisory)

**Checklist Phase 2:**
- [x] Test 3 workers song song, 3 sessions cùng lúc
- [x] Verify không có session bị double-assign
- [ ] Verify cancel trong lúc waiting_for_user hoạt động đúng
- [ ] Verify worker crash recovery đúng 2 cases (queued vs running)

---

## Phase 3 — Production Hardening

**Observability cơ bản (cần có trước benchmark):**

Endpoint `GET /v1/metrics` (hoặc Prometheus endpoint):
```
workers_alive           gauge
workers_busy            gauge
queue_length            gauge
sessions_queued         gauge
sessions_running        gauge
sessions_waiting        gauge  (waiting_for_user)
sessions_done_total     counter
sessions_failed_total   counter
ask_wait_time_seconds   histogram
step_latency_seconds    histogram (p50, p95)
browser_launch_seconds  histogram
```

**Benchmark plan:**
```
5  concurrent → baseline
10 concurrent → stress nhẹ
20 concurrent → sustained
50 concurrent → capacity limit

Đo: RAM/worker, CPU/worker, step latency p50/p95, queue wait, crash rate
```

**Phần còn lại Phase 3:**
- [ ] Observability endpoint
- [ ] Benchmark 5→10→20→50 sessions
- [ ] API key auth (Bearer token, FastAPI Depends)
- [ ] CORS thu hẹp origin
- [ ] Postgres (nếu cần history dài hạn)
- [ ] Artifact cleanup job (xóa files > 7 ngày)

---

## Dependencies bổ sung

```
# Phase 1b — thêm vào requirements.txt:
redis[asyncio]>=5.0
```

---

## Không thay đổi

- `LLM_base/` — toàn bộ engine giữ nguyên
- `web_UI_test/` — React UI giữ nguyên
- API endpoint URLs — giữ nguyên (không breaking change)
- `models.py` Pydantic schemas — giữ, bổ sung thêm nếu cần
