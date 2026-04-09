# Test Guide — Phase 2 Verification

## Yêu cầu

- Docker Desktop đang chạy
- Stack đang up: `docker compose -f docker_build/docker-compose.yml up -d`
- API healthy: `curl http://localhost:8000/v1/health`

---

## Case 1: Cancel khi waiting_for_user

**Mục tiêu:** Agent reach `ask` → gọi cancel → SSE nhận `cancelled` → status = `cancelled`

```bash
cd ai_tool_web
PYTHONIOENCODING=utf-8 python tests/test_cancel_waiting.py
```

**PASS khi thấy:**
```
PASS: Cancel during waiting_for_user hoạt động đúng!
```

**Thời gian:** ~30–60s (chờ agent navigate đến bước ask)

---

## Case 2: Worker crash recovery

**Mục tiêu:**
- Sub-case A: kill worker khi `running` → session thành `failed` trong ~45s
- Sub-case B: kill worker khi `waiting_for_user` → session thành `failed` trong ~45s

```bash
cd ai_tool_web
PYTHONIOENCODING=utf-8 python tests/test_worker_crash_recovery.py \
  --compose-file ../docker_build/docker-compose.yml
```

Chạy từng sub-case riêng:
```bash
# Chỉ case A
PYTHONIOENCODING=utf-8 python tests/test_worker_crash_recovery.py \
  --compose-file ../docker_build/docker-compose.yml --case A

# Chỉ case B
PYTHONIOENCODING=utf-8 python tests/test_worker_crash_recovery.py \
  --compose-file ../docker_build/docker-compose.yml --case B
```

**PASS khi thấy:**
```
Sub-case A: PASS
Sub-case B: PASS
```

**Thời gian:** ~3–4 phút (recovery_loop chạy mỗi 30s, worker key TTL 30s)

Script tự động restart worker sau khi test xong.

---

## Nếu test FAIL

### Case 1 không nhận được `ask` event
- Kiểm tra worker đang chạy: `curl http://localhost:8000/v1/health`
- Xem log worker: `docker compose -f docker_build/docker-compose.yml logs browser-worker --tail=50`

### Case 2 không chuyển sang `failed`
- Kiểm tra API log xem recovery_loop có chạy không:
  ```bash
  docker compose -f docker_build/docker-compose.yml logs api --tail=100 | grep -i recovery
  ```
- Kiểm tra worker key trong Redis sau khi kill:
  ```bash
  docker exec docker_build-redis-1 redis-cli KEYS "worker:*"
  # Nếu còn key → worker chưa bị kill hết
  # Nếu không còn key → recovery_loop sẽ detect qua orphaned session scan
  ```

### Xem trạng thái session thủ công
```bash
# Thay SESSION_ID bằng ID thật từ output test
curl http://localhost:8000/v1/sessions/SESSION_ID
```

---

## Cleanup sau test

Worker bị kill sẽ được script tự restart. Nếu cần restart thủ công:
```bash
docker compose -f docker_build/docker-compose.yml up -d browser-worker
```

Xóa session cũ trong Redis (optional):
```bash
docker exec docker_build-redis-1 redis-cli FLUSHDB
```
