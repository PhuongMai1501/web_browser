# Tests — Phase 1 Verification

## Yêu cầu chung

- Docker Desktop đang chạy
- Stack đang up: `docker compose -f docker_build/docker-compose.yml up -d`
- API healthy: `curl http://localhost:8000/v1/health`
- Worker alive: response có `workers_alive >= 1`

---

## Chạy toàn bộ Phase 1 test suite

```bash
bash product_build/run_phase1_tests.sh
```

Tùy chọn:
```bash
# Bỏ qua build (image đã có)
bash product_build/run_phase1_tests.sh --skip-build

# Chỉ chạy 1 test cụ thể
bash product_build/run_phase1_tests.sh --only T1
bash product_build/run_phase1_tests.sh --only T3

# Custom API URL
bash product_build/run_phase1_tests.sh --base-url http://192.168.1.100:8000
```

---

## T1 — Smoke Test

**Mục tiêu:** Xác nhận pipeline API → Redis → Worker → SSE thông suốt.

**Kiểm tra:**
1. `GET /v1/health` → status=ok, workers_alive >= 1
2. `POST /v1/sessions` → 201, nhận session_id hợp lệ
3. `GET /v1/sessions/{id}` → status hợp lệ
4. SSE stream → nhận ít nhất 1 event (không phải heartbeat)
5. `POST /v1/sessions/{id}/cancel` → session kết thúc

```bash
cd deploy_server/ai_tool_web
PYTHONIOENCODING=utf-8 python tests/test_smoke.py
```

**Thời gian:** ~1–3 phút (chờ worker pick up + emit event đầu tiên)

---

## T3 — Ask / Resume Flow

**Mục tiêu:** Xác nhận luồng agent hỏi → user trả lời → worker tiếp tục.

**Kịch bản:** Tạo session không có credentials → agent emit `ask` → gọi `/resume` → worker tiếp tục

```bash
cd deploy_server/ai_tool_web
PYTHONIOENCODING=utf-8 python tests/test_ask_resume_flow.py

# Resume với password thật (nếu muốn test tiếp đến done)
PYTHONIOENCODING=utf-8 python tests/test_ask_resume_flow.py \
  --resume-answer "actual_password_here"
```

**PASS khi thấy:**
```
PASS: Ask / Resume flow hoạt động đúng
```

**Thời gian:** ~3–5 phút (chờ agent navigate đến bước ask)

---

## T4 — Cancel during waiting_for_user

**Mục tiêu:** Agent reach `ask` → cancel → SSE nhận `cancelled` → status = `cancelled`

```bash
cd deploy_server/ai_tool_web
PYTHONIOENCODING=utf-8 python tests/test_cancel_waiting.py
```

**PASS khi thấy:**
```
PASS: Cancel during waiting_for_user hoạt động đúng!
```

**Thời gian:** ~2–4 phút

---

## T5 — Worker Crash Recovery

**Mục tiêu:** Kill worker giữa session → recovery loop detect → session marked failed

```bash
cd deploy_server/ai_tool_web
PYTHONIOENCODING=utf-8 python tests/test_worker_crash_recovery.py \
  --compose-file ../docker_build/docker-compose.yml

# Chỉ sub-case A (kill khi running)
python tests/test_worker_crash_recovery.py \
  --compose-file ../docker_build/docker-compose.yml --case A

# Chỉ sub-case B (kill khi waiting_for_user)
python tests/test_worker_crash_recovery.py \
  --compose-file ../docker_build/docker-compose.yml --case B
```

**PASS khi thấy:**
```
Sub-case A: PASS
Sub-case B: PASS
```

**Thời gian:** ~4–6 phút (recovery_loop chạy mỗi 30s, worker TTL 30s)

Script tự động restart worker sau test.

---

## Concurrent Sessions (load test, không bắt buộc Phase 1)

```bash
# Scale worker trước
docker compose -f docker_build/docker-compose.yml up -d --scale browser-worker=3

cd deploy_server/ai_tool_web
PYTHONIOENCODING=utf-8 python tests/test_concurrent_sessions.py --n 3
```

---

## Troubleshooting

### API không phản hồi
```bash
docker compose -f docker_build/docker-compose.yml ps
docker compose -f docker_build/docker-compose.yml logs api --tail=30
```

### Worker không pick up job
```bash
curl http://localhost:8000/v1/health
# workers_alive phải >= 1

docker compose -f docker_build/docker-compose.yml logs browser-worker --tail=50
```

### Không nhận ask event
```bash
# Xem worker đang làm gì
docker compose -f docker_build/docker-compose.yml logs browser-worker --tail=100

# Xem session state trong Redis
docker exec docker_build-redis-1 redis-cli HGETALL session:<SESSION_ID>
```

### Cleanup Redis sau test
```bash
docker exec docker_build-redis-1 redis-cli FLUSHDB
```
