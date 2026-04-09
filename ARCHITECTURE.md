# AI Tool Web — Kiến Trúc Hệ Thống

## Sơ đồ tổng quan

```
Local UI / API Caller
        │
        │ HTTP (port 9000)
        ▼
┌──────────────────────────────────────────────────────┐
│  FastAPI  (ai_tool_web/)                             │
│                                                      │
│  api/routes/                                         │
│    sessions.py    → POST /v1/sessions                │
│    stream.py      → GET  /v1/sessions/{id}/stream    │
│    resume.py      → POST /v1/sessions/{id}/resume    │
│    cancel.py      → POST /v1/sessions/{id}/cancel    │
│    screenshots.py → GET  /v1/sessions/{id}/steps/…   │
│    health.py      → GET  /v1/health                  │
│    browser.py     → POST /v1/browser/reset           │
│                                                      │
│  api/recovery.py  → background: detect dead worker,  │
│                     requeue orphaned session         │
└──────────────┬───────────────────────────────────────┘
               │  Pub/Sub + List + Hash
               ▼
┌──────────────────────────────────────────────────────┐
│  Redis  :6379  (Community Edition 7.0.15)            │
│                                                      │
│  pending_jobs           List     job queue FIFO      │
│  session:{id}           Hash     trạng thái session  │
│  session:{id}:buffer    List     50 events gần nhất  │
│  session:{id}:events    Pub/Sub  stream live events  │
│  session:{id}:event_seq Counter  event_id tăng dần   │
│  worker:{id}            String   heartbeat TTL=30s   │
│  lock:session:{id}      String   mutex 1 worker/sess │
│  resume:{id}            List     answer từ user      │
└──────────────┬───────────────────────────────────────┘
               │  BLPOP (block wait job)
               ▼
┌──────────────────────────────────────────────────────┐
│  Browser Worker × N  (ai_tool_web/worker/)           │
│                                                      │
│  browser_worker.py                                   │
│    AGENT_BROWSER_SESSION = worker-N  (isolated)      │
│    heartbeat.py   → refresh worker:{id} mỗi 15s     │
│    job_handler.py → chạy 1 session đến khi xong     │
│           │                                          │
│           ▼                                          │
│  LLM_base/                                           │
│    runner.py          → vòng lặp agent               │
│    llm_planner.py     → gọi OpenAI API               │
│    prompts.py         → system + user prompt         │
│    browser_adapter.py → gọi agent-browser CLI        │
│    scenarios/         → kịch bản (chang_login, ...)  │
│    state.py           → StepRecord, SessionState     │
└──────────────┬───────────────────────────────────────┘
               │  subprocess per worker
               ▼
┌──────────────────────────────────────────────────────┐
│  agent-browser CLI  (isolated session per worker)    │
│  Chrome headless                                     │
└──────────────────────────────────────────────────────┘
```

---

## Cấu trúc thư mục

```
deploy_server/
├── ai_tool_web/                   # API + Worker layer
│   ├── api/
│   │   ├── app.py                 # FastAPI entrypoint, CORS, startup hook
│   │   ├── recovery.py            # Background: detect dead workers
│   │   ├── sse_stream.py          # SSE generator từ Redis Pub/Sub
│   │   ├── artifact_service.py    # Serve file ảnh screenshot
│   │   └── routes/
│   │       ├── health.py          # GET  /v1/health
│   │       ├── sessions.py        # POST /v1/sessions, GET /v1/sessions/{id}
│   │       ├── stream.py          # GET  /v1/sessions/{id}/stream
│   │       ├── resume.py          # POST /v1/sessions/{id}/resume
│   │       ├── cancel.py          # POST /v1/sessions/{id}/cancel
│   │       ├── screenshots.py     # GET  /v1/sessions/{id}/steps/{n}/screenshot
│   │       └── browser.py         # POST /v1/browser/reset
│   ├── worker/
│   │   ├── browser_worker.py      # Entry point worker, manager mode (--count N)
│   │   ├── job_handler.py         # Chạy 1 session: loop LLM → browser → event
│   │   └── heartbeat.py           # Refresh worker key mỗi 15s
│   ├── store/
│   │   ├── redis_client.py        # Singleton async/sync Redis connection
│   │   ├── session_store.py       # CRUD session:{id} Hash
│   │   ├── job_queue.py           # RPUSH / BLPOP pending_jobs
│   │   ├── event_store.py         # Push event → Pub/Sub + buffer
│   │   └── worker_registry.py     # Register / heartbeat / find dead workers
│   ├── config.py                  # Tất cả constants và env vars
│   └── models.py                  # Pydantic models: request, response, SSE events
│
├── LLM_base/                      # LLM + Browser automation layer
│   ├── runner.py                  # Agent loop: snapshot → LLM → action → repeat
│   ├── llm_planner.py             # Gọi OpenAI API, parse JSON action
│   ├── prompts.py                 # System prompt + user prompt templates
│   ├── browser_adapter.py         # Wrapper agent-browser CLI (subprocess)
│   ├── state.py                   # StepRecord dataclass, SessionState
│   ├── scenarios/
│   │   └── chang_login.py         # Kịch bản đăng nhập chang.fpt.net
│   └── artifacts/                 # Screenshots + session logs (tự tạo)
│
├── start_api.sh                   # Chạy FastAPI (dùng conda env tool_web)
├── start_worker.sh                # Chạy N workers: ./start_worker.sh 50
├── .env                           # OPENAI_API_KEY, REDIS_URL, paths
├── requirements.txt               # fastapi, uvicorn, redis, openai, ...
├── API.md                         # API reference đầy đủ
├── ARCHITECTURE.md                # File này
└── DEPLOY.md                      # Hướng dẫn deploy thủ công
```

---

## Luồng xử lý 1 request đầy đủ

```
1. POST /v1/sessions
   body: { scenario, context, max_steps }
        │
        ├── tạo session:{id} trong Redis  (status = queued)
        ├── RPUSH pending_jobs → session_id
        └── trả về { session_id, stream_url, queue_position }

2. GET /v1/sessions/{id}/stream
        │
        ├── subscribe Redis Pub/Sub  session:{id}:events
        ├── replay buffer (tối đa 50 events đã qua)
        └── giữ kết nối, stream SSE về client

3. Worker BLPOP pending_jobs → nhận session_id
        │
        ├── set lock:session:{id}  (mutex, TTL 60s)
        ├── update status = assigned → running
        └── agent loop:
              ┌─────────────────────────────────────┐
              │  take_snapshot()  (accessibility tree)
              │        ↓
              │  llm_planner()   → action JSON
              │        ↓
              │  execute action  (click / type / wait)
              │        ↓
              │  push_event()    → Redis Pub/Sub
              │        ↓
              │  (lặp lại đến done / failed / ask)
              └─────────────────────────────────────┘

4. Khi event = ask  (agent thiếu thông tin)
        │
        ├── worker BLPOP resume:{id}  (block chờ)
        ├── UI nhận SSE event ask → hiện dialog
        ├── POST /v1/sessions/{id}/resume  { answer }
        └── worker nhận answer → tiếp tục loop

5. Event terminal (done / failed / cancelled / timed_out)
        │
        ├── worker giải phóng lock, update status
        └── SSE stream đóng kết nối
```

---

## Session lifecycle

```
queued
  └─► assigned    (worker nhận job)
        └─► running    (agent đang chạy)
              ├─► waiting_for_user    (agent hỏi user)
              │         └─► running  (sau khi /resume)
              ├─► done       ✓ thành công
              ├─► failed     ✗ lỗi
              ├─► cancelled  ✗ user huỷ
              └─► timed_out  ✗ quá 5 phút không /resume
```

---

## SSE Event types

| Event | Khi nào | Payload chính |
|-------|---------|---------------|
| `step` | Mỗi action agent thực hiện | `step, action, reason, screenshot_url` |
| `ask` | Agent cần user cung cấp thông tin | `message, ask_type, screenshot_url` |
| `done` | Hoàn thành thành công | `message, total_steps, duration_seconds` |
| `failed` | Thất bại | `code, message` |
| `cancelled` | User huỷ | `reason` |
| `timed_out` | Hết giờ chờ user | `elapsed_seconds` |
| `heartbeat` | Keep-alive mỗi 15s | `{}` (bỏ qua ở client) |

---

## Redis key schema

| Key | Type | TTL | Nội dung |
|-----|------|-----|---------|
| `pending_jobs` | List | — | Queue session_id chờ xử lý |
| `session:{id}` | Hash | 600s | Toàn bộ metadata session |
| `session:{id}:buffer` | List | 600s | 50 events gần nhất (replay khi reconnect) |
| `session:{id}:events` | Pub/Sub | — | Live event channel |
| `session:{id}:event_seq` | String | — | Counter event_id tăng dần |
| `worker:{id}` | String (JSON) | 30s | Status, current_session, last_heartbeat |
| `lock:session:{id}` | String | 60s | Mutex — worker nào đang giữ session |
| `resume:{id}` | List | — | Answer từ user (BLPOP) |

---

## Cấu hình quan trọng

| Setting | Giá trị | File |
|---------|---------|------|
| API port | 9000 | `start_api.sh` |
| Max steps | 3 – 30 (default 20) | `config.py` |
| Session hard timeout | 10 phút | `config.py` `SESSION_HARD_CAP_S` |
| Ask timeout (chờ user) | 5 phút | `config.py` `ASK_TIMEOUT_S` |
| Session TTL Redis | 10 phút | `config.py` `SESSION_TTL_S` |
| Queue hard cap | 100 sessions | `job_queue.py` `_HARD_CAP` |
| Event buffer size | 50 events | `event_store.py` `_BUFFER_MAX` |
| Worker heartbeat | mỗi 15s, TTL 30s | `worker_registry.py` |
| Worker dead threshold | 45s không heartbeat | `worker_registry.py` |
| LLM model | `gpt-5.4-nano` | `llm_planner.py` |
| Browser isolation | `AGENT_BROWSER_SESSION=worker-N` | `browser_worker.py` |

---

## Cách chạy

```bash
cd /mnt/changAI/research/tool_web/deploy_server

# Terminal 1 — API (port 9000)
./start_api.sh

# Terminal 2 — 50 Workers
./start_worker.sh 50

# Kiểm tra
python3 -c "import urllib.request; print(urllib.request.urlopen('http://localhost:9000/v1/health').read().decode())"
# {"status":"ok","workers_alive":50,"workers_busy":0,"queue_length":0}
```

---

## Điểm mở rộng khi phát triển

| Muốn làm | Sửa ở đâu |
|----------|-----------|
| Thêm scenario mới | `LLM_base/scenarios/` + đăng ký trong `job_handler.py` |
| Đổi LLM model | `LLM_base/llm_planner.py` dòng `model=` |
| Sửa prompt agent | `LLM_base/prompts.py` |
| Thêm API endpoint | `ai_tool_web/api/routes/` + import trong `app.py` |
| Thêm field SSE event | `ai_tool_web/models.py` |
| Tăng timeout / queue size | `ai_tool_web/config.py` |
| Tăng số worker | `./start_worker.sh <N>` |
