# Chang Browser API — Hướng dẫn tích hợp cho team Sup Agent

> Tài liệu tóm tắt flow truyền message giữa Sup Agent và Chang Browser API,
> trọng tâm là chu trình **chạy → user sửa kịch bản → chạy lại**.
>
> Đọc song song với [`GUIDE_API_INTEGRATION.md`](GUIDE_API_INTEGRATION.md) (full reference).

**Cập nhật**: 2026-05-18 — task-centric API (Phương án B Dual API).

---

## 1. Concept gốc — `task_id` vs `session_id`

| Khái niệm | Ai tạo | Vai trò |
|-----------|--------|---------|
| `task_id` | **Sup Agent** | ID logic của 1 conversation. Tạo 1 lần, dùng suốt vòng đời chat. |
| `session_id` | API auto-gen | ID của 1 lần worker chạy YAML (1 iteration). API tự sinh UUID. |
| `iteration` | API auto-gen | Số thứ tự lần thử trong task (1, 2, 3...). API tự INCR. |

**Mental model**:
```
1 conversation = 1 task_id
   └─ iter 1 (session "s-111") → bị cancel vì user sửa YAML
   └─ iter 2 (session "s-222") → bị cancel vì user sửa tiếp
   └─ iter 3 (session "s-333") → done ✅
```

**Sup Agent chỉ cần lưu `task_id`** trong DB/Redis của mình. Mọi endpoint đều route qua task_id, API tự pick iteration latest.

---

## 2. Endpoint map — Sup Agent dùng đâu?

| User action trong UI | Endpoint Chang Browser API | Body chính |
|----------------------|----------------------------|------------|
| Tạo conversation mới + gửi yêu cầu lần đầu | `POST /v1/tasks/{task_id}/run` | `{query}` hoặc `{scenario_yaml}` |
| Sửa YAML rồi thử lại | **`POST /v1/tasks/{task_id}/run`** | `{scenario_yaml: yaml_moi}` |
| Trả lời câu hỏi của bot (ask_user) | `POST /v1/tasks/{task_id}/resume` | `{answer: "..."}` |
| User gõ "stop"/"hủy" để dừng | `POST /v1/tasks/{task_id}/cancel` | (không cần body) |
| Confirm task xong sớm | `POST /v1/tasks/{task_id}/resume` | `{answer: "confirm_done"}` |
| Listen events realtime | `GET /v1/tasks/{task_id}/stream` (SSE) | — |
| Lấy kết quả final | `GET /v1/tasks/{task_id}/result` | — |
| Check task đang ở đâu (sau crash) | `GET /v1/tasks/{task_id}/status` | — |

**Quan trọng**: YAML mới luôn đi qua `/run`, **không bao giờ** qua `/resume`. Xem mục [§5](#5-tại-sao-yaml-mới-không-đi-qua-resume).

---

## 3. Timeline đầy đủ — Case A: Chạy bình thường, user trả lời ask_user

### Setup (1 lần khi user mở conversation)

```python
import uuid
task_id = f"t-{uuid.uuid4().hex[:12]}"   # vd: t-a1b2c3d4e5f6
# Lưu vào Sup Agent's Redis/DB theo conversation_id
```

### Sequence

```
T+0.0s  [User → Sup Agent UI]
        "Tìm giá iPhone 15 trên FPT Shop"

T+0.1s  [Sup Agent → Chang API]
        POST /v1/tasks/t-a1b2c3d4e5f6/run
        Headers: X-User-Id: sup-agent-1
        Body: {"query": "Tìm giá iPhone 15", "max_steps": 10}

T+3.5s  [Chang API ←→ OpenAI]
        gpt-4o-mini gen YAML từ query (~3s, ~$0.0003)

T+3.7s  [Chang API → Sup Agent]   201 Created
        {
          "session_id": "s-111",
          "task_id": "t-a1b2c3d4e5f6",
          "iteration": 1,
          "cancelled_prev_count": 0,
          "scenario_yaml": "id: _q_xxx\nstart_url: ...",   ← Sup Agent NÊN CACHE
          "model_used": "gpt-4o-mini",
          "tokens_in": 612, "tokens_out": 287
        }

T+3.8s  [Sup Agent → Chang API]
        GET /v1/tasks/t-a1b2c3d4e5f6/stream   ← Mở SSE
        Response headers: X-Session-Id: s-111, X-Iteration: 1

T+4.5s  [Worker → SSE → Sup Agent]
        event: step
        data: {step: 1, action: "goto", url_after: "https://fptshop.com.vn"}

T+6.0s  [Worker → SSE]
        event: step
        data: {step: 2, action: "fill", ref: "search-box", text_typed: "iPhone 15"}

T+8.0s  [Worker hits ask_user step]
        status → "waiting_for_user"
        BLPOP resume:s-111 (timeout=310s)

T+8.1s  [Worker → SSE → Sup Agent]
        event: ask
        data: {step: 3, message: "Sản phẩm nào trong list?", screenshot_url: "..."}

T+8.2s  [Sup Agent → User UI]
        Render câu hỏi "Sản phẩm nào trong list?"

T+15s   [User → Sup Agent]
        "iPhone 15 Pro Max 256GB"

T+15.1s [Sup Agent → Chang API]
        POST /v1/tasks/t-a1b2c3d4e5f6/resume
        Body: {"answer": "iPhone 15 Pro Max 256GB"}

T+15.2s [Chang API → Sup Agent]   200
        {"status": "resumed", "session_id": "s-111"}

T+15.3s [Worker BLPOP unblocks]
        msg = {"type": "answer", "answer": "iPhone 15 Pro Max 256GB"}
        - Check _CANCEL_KEYWORDS → không match
        - Check "confirm_done" → không match
        - status → "running"
        - Feed answer vào LLM context → tiếp step 4

T+18s   [Worker → SSE]
        event: step (step 4: click "iPhone 15 Pro Max 256GB")

T+24s   [Worker → SSE]
        event: done
        data: {step: 6, message: "Hoàn thành", total_steps: 6, duration_seconds: 20.3}

T+24.1s [Sup Agent → Chang API]
        GET /v1/tasks/t-a1b2c3d4e5f6/result
        Response: {
          "session_id": "s-111", "status": "done",
          "data": {"name": "iPhone 15 Pro Max 256GB", "price": "30.990.000₫", ...}
        }
```

---

## 4. Timeline đầy đủ — Case B: User sửa YAML giữa chừng

Tiếp diễn từ Case A đến T+8.1s (worker đang waiting_for_user, Sup Agent đã render câu hỏi).

User KHÔNG trả lời câu hỏi mà mở YAML editor sửa kịch bản.

### Sequence

```
T+8.2s  [User → Sup Agent UI]
        "Stop, để tôi sửa YAML" (hoặc bấm nút "Edit scenario")

T+8.3s  [Sup Agent UI]
        Mở YAML editor với scenario_yaml (đã cache từ T+3.7s)

T+30s   [User → Sup Agent]
        Submit YAML đã sửa
        yaml_modified = "id: _custom\nstart_url: ...\nsteps: [<sửa step 3>...]"

T+30.1s [Sup Agent → Chang API]
        POST /v1/tasks/t-a1b2c3d4e5f6/run   ← CÙNG task_id, dùng /run
        Body: {
          "scenario_yaml": yaml_modified,
          "max_steps": 10
          // cancel_prev_iterations=true là DEFAULT, không cần truyền
        }

T+30.2s [Chang API → Redis] (logic _cancel_prev_iterations)
        Tìm s-111 đang waiting_for_user (cùng task_id):
        ┌── update s-111: cancel_requested=1
        └── RPUSH resume:s-111 '{"type":"cancel"}'   ← unblock worker

T+30.3s [Worker BLPOP của s-111 unblocks]
        msg = {"type": "cancel"}
        - push SSE event "cancelled" {reason: "Cancelled while waiting for user"}
        - status → "cancelled", finished_at=...
        - Browser cleanup, worker free

T+30.3s [Sup Agent SSE stream của iter 1]
        event: cancelled
        data: {reason: "Cancelled while waiting for user"}
        → Stream close
        ⚠️ Sup Agent nhận event này → BIẾT là auto-cancel (do vừa POST /run)

T+30.3s [Chang API → Redis] (song song với cancel)
        - INCR task:t-a1b2c3d4e5f6:counter → iteration=2
        - Tạo session_id="s-222", status="queued"
        - Push job vào queue worker

T+30.4s [Chang API → Sup Agent]   201 Created
        {
          "session_id": "s-222",
          "task_id": "t-a1b2c3d4e5f6",
          "iteration": 2,
          "cancelled_prev_count": 1,    ← iter 1 vừa bị cancel
          "scenario_yaml": null,         ← mode YAML inline, không gen lại
          "queue_position": 1
        }

T+30.5s [Sup Agent → Chang API]
        GET /v1/tasks/t-a1b2c3d4e5f6/stream    ← MỞ LẠI cùng URL
        Response headers: X-Session-Id: s-222, X-Iteration: 2
        Auto-attach iter 2 (vì stream lookup latest iteration của task)

T+31s   [Worker pod picks job s-222]
        Browser launch (fresh, KHÔNG reuse browser của iter 1)
        status → "running"

T+32s   [Worker → SSE → Sup Agent]
        event: step (iter 2 từ step 1)
        ...

T+50s   [Worker → SSE]
        event: done

T+50.1s [Sup Agent → Chang API]
        GET /v1/tasks/t-a1b2c3d4e5f6/result?strategy=latest_done
        Response: result của s-222 (iter 2)
```

### Điểm chốt Case B

1. **1 request duy nhất** (`POST /run` với YAML mới) đủ để:
   - Cancel iter cũ
   - Tạo iter mới
   - Atomic (không có race condition)
2. **Sup Agent KHÔNG cần** gọi `/cancel` trước rồi `/run` sau
3. **Stream URL không đổi** — Sup Agent mở lại cùng URL `/v1/tasks/{task_id}/stream` → tự attach iter mới
4. Event `cancelled` đến trên stream cũ là **dấu hiệu bình thường** khi vừa POST /run, không phải lỗi

---

## 5. Tại sao YAML mới KHÔNG đi qua `/resume`?

`/resume` được thiết kế CHỈ cho việc **trả lời `ask_user`**. Worker BLPOP queue `resume:{session_id}` và parse 1 trong 3 loại message:

```json
{ "type": "cancel" }                   // hủy session
{ "type": "answer", "answer": "..." }  // trả lời ask_user
```

Với keyword đặc biệt trong `answer`:
- `"_CANCEL_KEYWORDS"` (vd "tạm dừng", "stop", "hủy") → tự cancel
- `"confirm_done"` → mark done ngay không gửi LLM

**Nếu nhét YAML vào `/resume`**, sẽ gặp 4 vấn đề:
1. **Semantic**: `/resume` = "trả lời câu hỏi", không phải "thay kịch bản"
2. **State conflict**: Worker đang mid-run với browser context + current_step + cookies. Swap YAML giữa chừng phải discard browser, reset step, reload spec — bằng với cancel + restart, nhưng phức tạp hơn
3. **Race condition**: Worker có thể vừa nhận YAML mới vừa đang execute step cũ → state corruption
4. **Stream gắn cứng vào session_id**: events nào thuộc iter cũ vs mới?

→ Dùng `/run` là design ĐÚNG: API atomic xử lý cancel + spawn iter mới với browser fresh.

---

## 6. State machine — 1 iteration

```
queued ─→ assigned ─→ running ─┬─→ done ✅
                                ├─→ failed ❌
                                ├─→ cancelled ⛔
                                ├─→ timed_out ⏱
                                └─→ waiting_for_user
                                     ├─→ running (sau /resume answer)
                                     ├─→ cancelled (sau /cancel hoặc cancel keyword)
                                     └─→ timed_out (5 phút no answer)
```

Terminal states: `done`, `failed`, `cancelled`, `timed_out`. Iter đã terminal → không cancel/resume được nữa.

---

## 7. Decision matrix — User action → Endpoint

| Tình huống | Endpoint | Lưu ý |
|-----------|----------|-------|
| User gửi yêu cầu lần đầu | `POST /run` query/yaml | Tạo iter 1 |
| User sửa YAML rồi thử lại | `POST /run` scenario_yaml | API auto-cancel iter trước |
| User đổi query NL khác | `POST /run` query | Như trên, LLM gen YAML mới |
| User trả lời câu hỏi của bot | `POST /resume` answer | Worker đang waiting_for_user |
| User gõ "stop"/"hủy" khi đang chạy | `POST /cancel` | Hoặc `/resume` với answer="stop" |
| User confirm task xong | `POST /resume` answer="confirm_done" | Bot skip step còn lại, mark done |
| Sup Agent crash → restart | `GET /status` rồi quyết định | Xem mục 9 |
| Stream rớt → reconnect | `GET /stream?lastEventId=N` | Resume từ event N |

---

## 8. Code mẫu Python — Loop sửa YAML đầy đủ

```python
import uuid, json, hashlib, requests, time
from sseclient import SSEClient

API = "http://chang-browser-api.dscapp.com"
HEADERS = {"X-User-Id": "sup-agent-1", "Content-Type": "application/json"}

class TaskClient:
    def __init__(self, task_id: str | None = None):
        self.task_id = task_id or f"t-{uuid.uuid4().hex[:12]}"
        self.yaml_cache: str | None = None
        self.iteration: int = 0

    def _yaml_fingerprint(self, query: str) -> str:
        return hashlib.sha256(query.lower().strip().encode()).hexdigest()[:16]

    def run_query(self, query: str, max_steps: int = 10) -> dict:
        """Lần đầu — LLM gen YAML từ query NL."""
        body = {"query": query, "max_steps": max_steps}
        resp = requests.post(f"{API}/v1/tasks/{self.task_id}/run",
                             headers=HEADERS, json=body, timeout=15).json()
        if resp.get("scenario_yaml"):
            self.yaml_cache = resp["scenario_yaml"]
        self.iteration = resp["iteration"]
        return resp

    def run_yaml(self, yaml_text: str, max_steps: int = 10) -> dict:
        """Sửa YAML rồi thử lại — auto-cancel iter cũ."""
        body = {"scenario_yaml": yaml_text, "max_steps": max_steps}
        resp = requests.post(f"{API}/v1/tasks/{self.task_id}/run",
                             headers=HEADERS, json=body, timeout=5).json()
        self.yaml_cache = yaml_text
        self.iteration = resp["iteration"]
        return resp

    def stream(self):
        """SSE stream events của iter latest. Yield (event_name, data_dict)."""
        url = f"{API}/v1/tasks/{self.task_id}/stream"
        client = SSEClient(url, headers=HEADERS)
        for ev in client.events():
            yield ev.event, json.loads(ev.data) if ev.data else {}

    def answer(self, text: str) -> dict:
        """Trả lời ask_user."""
        body = {"answer": text}
        return requests.post(f"{API}/v1/tasks/{self.task_id}/resume",
                             headers=HEADERS, json=body, timeout=3).json()

    def cancel(self, all_iters: bool = False) -> dict:
        """Cancel iter latest (default) hoặc tất cả."""
        params = {"all": "true"} if all_iters else {}
        return requests.post(f"{API}/v1/tasks/{self.task_id}/cancel",
                             headers=HEADERS, params=params, timeout=3).json()

    def result(self, strategy: str = "latest_done") -> dict:
        """Lấy result của iter (default latest done)."""
        params = {"strategy": strategy}
        return requests.get(f"{API}/v1/tasks/{self.task_id}/result",
                            headers=HEADERS, params=params, timeout=5).json()

    def status(self) -> dict:
        """Check task đang ở đâu (cho recovery sau crash)."""
        return requests.get(f"{API}/v1/tasks/{self.task_id}/status",
                            headers=HEADERS, timeout=3).json()


# ─── Use case chính: Loop sửa YAML ───────────────────────────────────────────

def conversation_flow(user_query: str):
    task = TaskClient()

    # Iter 1: query mode
    resp = task.run_query(user_query, max_steps=10)
    print(f"Iter 1 started, session={resp['session_id']}")

    while True:
        # Listen stream của iter hiện tại
        outcome = None
        for event, data in task.stream():
            if event == "step":
                forward_to_user_ui(data)
            elif event == "ask":
                # Bot hỏi user
                answer_or_action = ask_user_blocking(data["message"], task_id=task.task_id)
                if answer_or_action.kind == "answer":
                    task.answer(answer_or_action.text)
                elif answer_or_action.kind == "edit_yaml":
                    # User sửa YAML → break loop ngay, gọi run_yaml ở outer
                    new_yaml = answer_or_action.yaml_text
                    task.run_yaml(new_yaml, max_steps=10)
                    break   # break inner stream loop → outer while sẽ mở stream mới
            elif event == "done":
                outcome = ("done", data)
                break
            elif event == "failed":
                outcome = ("failed", data)
                break
            elif event == "cancelled":
                # Có thể là user cancel hoặc auto-cancel do POST /run mới
                outcome = ("cancelled", data)
                break
            elif event == "timed_out":
                outcome = ("timed_out", data)
                break

        if outcome and outcome[0] == "done":
            result = task.result()
            return result.get("data")
        elif outcome and outcome[0] in ("failed", "timed_out"):
            return None
        # Cancelled → có thể là auto-cancel do user sửa YAML
        # → vòng while tiếp, stream lại sẽ attach iter mới

# ─── Pattern recovery sau Sup Agent crash ────────────────────────────────────

def recover_conversation(saved_task_id: str):
    task = TaskClient(task_id=saved_task_id)
    status = task.status()

    if not status["has_iterations"]:
        return None   # Task chưa được tạo

    if status["has_running"]:
        # Iter đang chạy → reconnect stream
        for event, data in task.stream():
            ...
    elif status["latest_done_iteration"] > 0:
        # Đã có result → fetch
        return task.result()
```

---

## 9. Sup Agent's storage requirement

Sup Agent CẦN persist các thông tin sau theo conversation_id:

| Field | Type | Lifetime | Purpose |
|-------|------|----------|---------|
| `task_id` | str | Suốt conversation | Identify task trên Chang API |
| `yaml_cache` | str \| null | Suốt conversation hoặc 24h | Tránh LLM gen lại khi user re-run |
| `last_session_id` | str | Optional (chỉ cho debug) | Trace lỗi |

**KHÔNG cần lưu**:
- ❌ `iteration` number — API tự INCR
- ❌ Toàn bộ event history — đã có ở Chang API artifact CDN

**Recommendation**: lưu vào Redis với key pattern `sup-agent:conv:{conversation_id}` TTL 7 ngày.

---

## 10. Error handling Sup Agent cần xử lý

| HTTP code | Detail | Action |
|-----------|--------|--------|
| 409 | `SESSION_NOT_WAITING` | Iter latest không waiting → user chưa hỏi mà gọi /resume. Check status trước |
| 409 | `MULTIPLE_WAITING` | Chỉ xảy ra với `cancel_prev_iterations=false`. Dùng session-level `/v1/sessions/{sid}/resume` |
| 409 | `SESSION_FINISHED` | Cancel iter đã terminal → bỏ qua |
| 422 | `Chỉ được set 1 trong 2 field` | Body có cả `scenario_yaml` lẫn `query` → fix body |
| 422 | `Thiếu field context bắt buộc: [...]` | YAML có inputs required nhưng context không có → thêm context hoặc set `ask_missing_inputs=true` |
| 422 | `YAML gen từ query parse fail` | LLM trả YAML invalid → retry với query rõ hơn, hoặc fallback yaml inline |
| 502 | `LLM generate fail` | OpenAI timeout/error → retry sau 5-10s |
| 503 | `Queue is full` | >100 jobs pending → retry với exponential backoff |
| 404 | `Task không có iteration nào` | Task chưa được POST /run lần nào → check task_id đúng chưa |

---

## 11. Backward compatibility — Endpoint cũ

Endpoint cũ `/v1/sessions/*` **vẫn hoạt động**, Sup Agent migrate dần theo 3 phase:

| Phase | Đổi gì |
|-------|--------|
| 1 | Đổi POST `/v1/sessions` → `/v1/tasks/{task_id}/run`. Vẫn dùng session_id từ response cho stream/resume/cancel |
| 2 | Đổi stream/resume/cancel/result sang task-centric URL |
| 3 | Cleanup — remove session_id khỏi Sup Agent's storage |

---

## 12. Checklist cho team Sup Agent backend

```
□ Schema conversation: thêm field task_id (string, max 128 chars)
□ Generate task_id khi user mở chat (UUID hoặc cross-trace với conversation_id)
□ Đổi endpoint POST: /v1/sessions → /v1/tasks/{task_id}/run
□ Đổi endpoint SSE: /v1/sessions/{sid}/stream → /v1/tasks/{task_id}/stream
□ Đổi endpoint resume/cancel: dùng /v1/tasks/{task_id}/*
□ BỎ logic cancel-then-run khi user sửa YAML (API tự cancel)
□ Khi nhận event "cancelled" sau khi vừa POST /run: hiểu là auto-cancel, mở stream mới
□ Cache scenario_yaml LLM-gen theo SHA256 query (TTL 24h)
□ Implement aggregate downloadedFiles[] từ step events có downloaded_cdn_url
□ Decision matrix /run vs /resume vs /cancel — không nhầm endpoint
□ Error handling 4 mã 409/422/502/503 ở mục 10
□ Pattern recovery: dùng GET /status sau Sup Agent restart
```

---

## 13. Liên hệ + Resource

- **Full API reference**: [`GUIDE_API_INTEGRATION.md`](GUIDE_API_INTEGRATION.md)
- **YAML scenario schema**: mục 7 trong file trên
- **Repo API**: `gitlab.dsc.com/fpl/aichatbot/chang-browser-api`
- **Repo Worker**: `gitlab.dsc.com/fpl/aichatbot/chang-browser-worker`
- **Owner**: hiepqn@fpt.com
- **Báo lỗi**: GitLab issue hoặc Slack `#chang-browser-api`
