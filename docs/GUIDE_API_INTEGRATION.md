# Chang Browser API — Integration Guide

> Tài liệu dành cho **backend Sup Agent** tích hợp với Chang Browser API.
> Cập nhật theo Phương án B (Dual API) — task-centric default + session-level cho precision.

**Base URL prod:** `http://chang-browser-api.dscapp.com`

---

## 1. Concept tổng quan

### 1.1 Task vs Session

| Khái niệm | Định nghĩa | Identifier |
|-----------|------------|------------|
| **Task** | 1 yêu cầu logic của end-user (1 conversation thread). Có thể chứa nhiều iterations (lần thử). | `task_id` (Sup Agent tự gen, vd `t-conv-xyz`) |
| **Session / Iteration** | 1 lần worker chạy 1 YAML scenario từ đầu đến cuối. 1 task có thể có nhiều iterations. | `session_id` (API auto-gen UUID) + `iteration` (1, 2, 3...) |

**Ví dụ thực tế**:
```
Task "t-find-iphone-price" (user muốn tìm giá iPhone 15)
├─ Iteration 1, session "abc-111" → status: cancelled (YAML chưa đúng)
├─ Iteration 2, session "abc-222" → status: cancelled (sửa thêm)
└─ Iteration 3, session "abc-333" → status: done ✅ (kết quả OK)
```

### 1.2 Khi nào dùng task-centric vs session-level

| Use case | Endpoint nên dùng |
|----------|-------------------|
| Conversation thông thường (1 user, sửa YAML loop) | **Task-centric** `/v1/tasks/{task_id}/*` |
| A/B benchmark (chạy 2+ iter song song) | **Session-level** `/v1/sessions/{session_id}/*` |
| Admin debug/monitor | **Session-level** |
| Đọc lịch sử iterations | **Task-centric** + endpoint `/iterations` |

**Recommendation cho Sup Agent**: dùng task-centric hết. Chỉ fallback session-level khi `cancel_prev_iterations=false`.

---

## 2. Authentication

Mọi request cần header:

```http
X-User-Id: <sup-agent-or-user-id>
Content-Type: application/json
```

**Admin endpoints** (vd `/v1/admin/*`, `/v1/browser/kill-all`):
```http
X-Admin-Token: <token>
```
Token so với env `ADMIN_TOKEN` (fallback `hiepqn-2026-admin` khi chưa set).

---

## 3. Quick start — Flow tổng thể

```python
import requests, uuid, json
from sseclient import SSEClient

API = "http://chang-browser-api.dscapp.com"
HEADERS = {"X-User-Id": "sup-agent-1", "Content-Type": "application/json"}

# Step 1: Tạo task_id khi user mở conversation
task_id = f"t-{uuid.uuid4().hex[:12]}"

# Step 2: User gửi yêu cầu → run iteration 1
resp = requests.post(
    f"{API}/v1/tasks/{task_id}/run",
    headers=HEADERS,
    json={"query": "Tìm giá iPhone 15", "max_steps": 10},
    timeout=15,
).json()
# resp: { session_id, task_id, iteration: 1, scenario_yaml, ... }

# Step 3: Stream events qua SSE (không cần session_id)
for ev in SSEClient(f"{API}/v1/tasks/{task_id}/stream").events():
    data = json.loads(ev.data)
    if ev.event == "step":
        print(f"Step {data['step']}: {data['action']} {data.get('ref','')}")
    elif ev.event == "ask":
        # Agent cần info từ user
        answer = input(data["message"] + ": ")
        requests.post(f"{API}/v1/tasks/{task_id}/resume",
                      headers=HEADERS, json={"answer": answer})
    elif ev.event == "done":
        break
    elif ev.event in ("failed", "cancelled", "timed_out"):
        break

# Step 4: Lấy result
result = requests.get(f"{API}/v1/tasks/{task_id}/result", headers=HEADERS).json()
print(result.get("data"))   # JSON extracted theo output_schema (nếu có)

# Step 5: User không hài lòng → sửa YAML, chạy lại (CÙNG task_id)
yaml_modified = resp["scenario_yaml"].replace("...", "...")
requests.post(
    f"{API}/v1/tasks/{task_id}/run",
    headers=HEADERS,
    json={"scenario_yaml": yaml_modified, "max_steps": 10},
)
# → API tự cancel iteration 1, tạo iteration 2
# → Sup Agent mở stream mới tại /v1/tasks/{task_id}/stream
```

---

## 4. Task-centric API (PRIMARY)

### 4.1 `POST /v1/tasks/{task_id}/run`

Tạo iteration mới trong task. Nếu task chưa tồn tại → tạo task + iteration 1.

**Path params:**
- `task_id` (string, 1-128 chars): ID logic của task. Sup Agent gen 1 lần per conversation. Có thể dùng external ID (vd Linear ticket, Sup Agent's conversation_id) để cross-trace.

**Body (chọn 1 trong 3 mode):**

#### Mode 1: Query NL (LLM tự gen YAML)
```json
{
  "query": "Đăng nhập chang.fpt.net, vào lịch sử trò chuyện",
  "query_site_hint": "chang.fpt.net",
  "max_steps": 15
}
```

#### Mode 2: Paste YAML inline
```json
{
  "scenario_yaml": "id: my_scenario\nmode: flow\nstart_url: ...\nsteps:\n  - action: ...",
  "context": {"email": "...", "password": "..."},
  "max_steps": 15
}
```

#### Mode 3: Scenario có sẵn trong DB
```json
{
  "scenario": "chang_login",
  "context": {"email": "...", "password": "..."},
  "max_steps": 15
}
```

**Common fields (cho cả 3 mode):**
| Field | Type | Default | Mục đích |
|-------|------|---------|----------|
| `name` | string \| null | null | Label hiển thị admin monitor (max 120 chars) |
| `goal` | string \| null | null | Override goal của spec |
| `url` | string \| null | null | Override start_url |
| `context` | dict \| null | null | Input values (email, password, keyword...) |
| `max_steps` | int (3-30) | 20 | Số step max trước khi timeout |
| `cancel_prev_iterations` | bool | **true** | Auto-cancel iter cũ cùng task khi tạo iter mới |
| `ask_missing_inputs` | bool \| null | null | Convert missing context → ask_user runtime. null=auto-enable cho mode `query` |
| `callback_url` | string \| null | null | Set → callback mode thay SSE |
| `callback_secret` | string \| null | null | HMAC secret cho callback |

**Response 201:**
```json
{
  "session_id": "abc-333",
  "task_id": "t-conv-xyz",
  "iteration": 3,
  "cancelled_prev_count": 1,
  "status": "queued",
  "mode": "sse",
  "stream_url": "/v1/sessions/abc-333/stream",
  "queue_position": 1,
  "created_at": "",

  "scenario_yaml": "id: _q_xxx\n...",  // Chỉ có khi mode=query
  "scenario_id": "_q_xxx",
  "generated_from_query": true,
  "model_used": "gpt-4o-mini",
  "tokens_in": 612,
  "tokens_out": 287
}
```

**Field response quan trọng:**
- `session_id`: ID của iteration mới (dùng nếu cần session-level operations)
- `iteration`: số thứ tự lần thử trong task (1, 2, 3...)
- `cancelled_prev_count`: số iter cũ bị auto-cancel
- `scenario_yaml`: YAML đã LLM-gen (chỉ mode `query`) — **Sup Agent nên cache** để user re-use không tốn LLM call

**Errors:**
- 422 `"Chỉ được set 1 trong 2 field: 'scenario_yaml' hoặc 'query'."`
- 422 YAML parse/validation fail
- 422 Context validation fail (thiếu required field)
- 502 LLM generate fail (OpenAI timeout/error)
- 503 Queue full (>100 jobs pending)

---

### 4.2 `GET /v1/tasks/{task_id}/stream`

SSE stream events của iteration **latest** trong task.

**Headers response:**
- `X-Session-Id`: session_id đang stream (info cho debug)
- `X-Iteration`: iteration number

**Query params:**
- `lastEventId` (int, optional): reconnect từ event id N (giống standard SSE)

**Event types:**

| Event | Khi nào | Payload |
|-------|---------|---------|
| `heartbeat` | Mỗi 15s | `{}` (giữ kết nối) |
| `step` | Worker chạy 1 step | `{step, action, ref, text_typed, url_before, url_after, screenshot_url, ...}` |
| `ask` | Agent cần info user | `{step, message, screenshot_url, ...}` |
| `done` | Hoàn thành | `{step, message, total_steps, duration_seconds, ...}` |
| `failed` | Lỗi không recover | `{code, message, recoverable}` |
| `cancelled` | Bị huỷ | `{reason}` |
| `timed_out` | Hết giờ chờ user (ask_user) | `{message}` |

**Curl example:**
```bash
curl -N "http://api/v1/tasks/t-xyz/stream" \
  -H "Accept: text/event-stream" \
  -H "X-User-Id: sup-agent"
```

**Quan trọng**: stream gắn cứng vào iteration tại thời điểm connect. Nếu iter bị cancel (do POST run mới) → stream nhận event `cancelled` → close. Sup Agent **mở stream mới** trên cùng URL `/v1/tasks/{task_id}/stream` → tự attach vào iter mới.

**Errors:**
- 404 Task không có iteration nào

---

### 4.3 `POST /v1/tasks/{task_id}/resume`

Trả lời ask_user event. Push answer vào iteration `waiting_for_user`.

**Body:**
```json
{ "answer": "user@fpt.net" }
```

**Response 200:**
```json
{ "status": "resumed", "session_id": "abc-333" }
```

**Errors:**
- 404 Task không có iteration nào
- 409 `SESSION_NOT_WAITING` — iter latest không ở waiting_for_user state
- 409 `MULTIPLE_WAITING` — task có >1 iter waiting (rare, chỉ khi `cancel_prev_iterations=false`). Khi gặp lỗi này, dùng session-level `/v1/sessions/{session_id}/resume` để target cụ thể.

---

### 4.4 `POST /v1/tasks/{task_id}/cancel`

Cancel iteration trong task.

**Query params:**
- `all` (bool, default false): true → cancel TẤT CẢ iter non-terminal. false → chỉ latest.

**Response 200:**
```json
{ "status": "cancelled", "steps_completed": 5 }
```

**Behavior**:
- Iter status=`queued` → mark cancelled, không pop queue (< 1s)
- Iter status=`running` → set flag → worker check ở step tiếp → graceful exit (1-10s)
- Iter status=`waiting_for_user` → push cancel signal qua resume queue (<1s)

**Errors:**
- 404 Task không có iteration nào
- 409 `SESSION_FINISHED` — không có iter nào đang chạy

---

### 4.5 `GET /v1/tasks/{task_id}/result`

Lấy `result.json` của 1 iteration trong task.

**Query params:**
- `strategy` (default `latest_done`):
  - `latest_done`: iter cuối có status=done
  - `latest_terminal`: iter cuối ở terminal state (done/failed/cancelled/timed_out)
  - `latest`: iter cuối bất kỳ (có thể chưa done)
- `iteration` (int, optional): override strategy, lấy iter cụ thể theo số

**Response 200** (nội dung result.json):
```json
{
  "session_id": "abc-333",
  "status": "done",
  "scenario": "_q_xxx",
  "summary": "Hoàn thành",
  "url_after": "https://fptshop.com.vn/...",
  "total_steps": 6,
  "duration_seconds": 24.3,
  "finished_at": "2026-05-15T...",
  "artifacts": {
    "log_path": "/app/.../session.jsonl",
    "log_url": "https://cdn.fstats.ai/.../session.jsonl",
    "session_json_url": "https://cdn.fstats.ai/.../session.json"
  },
  "data": {
    "name": "iPhone 15 Pro Max 256GB",
    "price": "30.990.000₫",
    "seller": "FPT Shop"
  }
}
```

**Field giải thích:**
- `artifacts.log_url`: CDN URL của `session.jsonl` (events log)
- `artifacts.session_json_url`: CDN URL của `session.json` (rich diagnostic — LLM prompt, raw response, action chosen từng step)
- `data`: extracted JSON theo `output_schema` declared trong YAML (nếu có)

**Errors:**
- 404 Task không có iteration nào
- 404 Strategy không match (vd `latest_done` nhưng chưa có iter done)
- 422 Invalid strategy value

---

### 4.6 `GET /v1/tasks/{task_id}/status`

Combo info về task — Sup Agent dùng để check trạng thái nhanh không cần stream.

**Response 200:**
```json
{
  "task_id": "t-conv-xyz",
  "has_iterations": true,
  "current_iteration": 3,
  "current_session_id": "abc-333",
  "current_status": "running",
  "total_iterations": 3,
  "has_running": true,
  "has_waiting_for_user": false,
  "latest_done_iteration": 0,
  "latest_done_session_id": ""
}
```

**Use case**: Sup Agent restart sau crash → query status để biết task đang ở đâu.

---

## 5. Session-level API (cho A/B testing + admin)

Khi `cancel_prev_iterations=false` và Sup Agent muốn target iter cụ thể, dùng session-level endpoints. Lấy `session_id` từ response của `POST /v1/tasks/{task_id}/run`.

### 5.1 Endpoints

| Method | URL | Tương đương task-centric |
|--------|-----|---------------------------|
| GET | `/v1/sessions/{session_id}` | `/v1/tasks/{task_id}/status` (latest) |
| GET | `/v1/sessions/{session_id}/stream` | `/v1/tasks/{task_id}/stream` |
| POST | `/v1/sessions/{session_id}/resume` | `/v1/tasks/{task_id}/resume` |
| POST | `/v1/sessions/{session_id}/cancel` | `/v1/tasks/{task_id}/cancel` |
| GET | `/v1/sessions/{session_id}/result` | `/v1/tasks/{task_id}/result?iteration=N` |
| GET | `/v1/sessions/{session_id}/steps/{n}/screenshot` | (chỉ session-level) |

### 5.2 Khi nào bắt buộc dùng session-level

1. **Parallel iterations** (`cancel_prev_iterations=false`):
```python
# Chạy 3 iter song song để A/B test
ids = []
for _ in range(3):
    r = requests.post(f"{API}/v1/tasks/{task_id}/run", headers=HEADERS,
                      json={"scenario_yaml": "...", "cancel_prev_iterations": False})
    ids.append(r.json()["session_id"])

# Stream từng iter cụ thể (task-centric chỉ stream latest)
for sid in ids:
    SSEClient(f"{API}/v1/sessions/{sid}/stream").events()
```

2. **Cancel iter cụ thể** (không phải latest):
```python
requests.post(f"{API}/v1/sessions/abc-222/cancel", headers=HEADERS)
```

3. **Resume khi có >1 iter waiting** (task-centric trả 409 MULTIPLE_WAITING):
```python
requests.post(f"{API}/v1/sessions/{specific_session_id}/resume",
              headers=HEADERS, json={"answer": "..."})
```

4. **Screenshot step cụ thể**:
```bash
GET /v1/sessions/abc-333/steps/4/screenshot
```

---

## 6. Common patterns

### 6.1 Pattern: Loop sửa YAML (use case chính)

```python
task_id = f"t-{uuid.uuid4().hex[:12]}"
yaml_cache = None

def run_and_wait(body):
    """Run + listen SSE đến terminal event."""
    resp = requests.post(f"{API}/v1/tasks/{task_id}/run",
                         headers=HEADERS, json=body).json()
    if resp.get("scenario_yaml"):
        global yaml_cache
        yaml_cache = resp["scenario_yaml"]   # cache YAML đã gen

    for ev in SSEClient(f"{API}/v1/tasks/{task_id}/stream").events():
        data = json.loads(ev.data)
        if ev.event == "ask":
            answer = ask_user_blocking(data["message"])
            requests.post(f"{API}/v1/tasks/{task_id}/resume",
                          headers=HEADERS, json={"answer": answer})
        elif ev.event in ("done", "failed", "cancelled", "timed_out"):
            return ev.event, data

    return None, None

# Iteration 1: query mode (LLM gen YAML)
status, data = run_and_wait({"query": "Tìm giá iPhone 15", "max_steps": 10})

while status != "done":
    # User cho feedback, sửa YAML
    yaml_modified = let_user_edit(yaml_cache)
    yaml_cache = yaml_modified
    # Run lại — auto-cancel iter trước (đã done/failed nên không có effect)
    status, data = run_and_wait({"scenario_yaml": yaml_modified, "max_steps": 10})

# Done — lấy result
result = requests.get(f"{API}/v1/tasks/{task_id}/result", headers=HEADERS).json()
extracted = result.get("data")
```

### 6.2 Pattern: User abort giữa chừng (cancel)

```python
# User gõ "stop" → cancel iter đang chạy
requests.post(f"{API}/v1/tasks/{task_id}/cancel", headers=HEADERS)
# Stream SSE sẽ nhận event `cancelled` → close
```

### 6.3 Pattern: Recovery sau Sup Agent crash

```python
# Restart Sup Agent, lấy task_id từ persistent storage (Redis/DB của Sup Agent)
task_id = load_task_id_from_storage(conversation_id)

# Check trạng thái task
status = requests.get(f"{API}/v1/tasks/{task_id}/status", headers=HEADERS).json()

if status["has_iterations"]:
    if status["has_running"]:
        # Iter đang chạy → reconnect stream
        SSEClient(f"{API}/v1/tasks/{task_id}/stream").events()
    elif status["latest_done_iteration"] > 0:
        # Đã có result → fetch
        result = requests.get(f"{API}/v1/tasks/{task_id}/result", headers=HEADERS).json()
    # Tiếp tục từ đó
```

### 6.4 Pattern: Batch monitoring nhiều task

```python
# Admin list tất cả active sessions, group theo task
sessions = requests.get(
    f"{API}/v1/admin/sessions?include_finished=false",
    headers={**HEADERS, "X-Admin-Token": "..."}
).json()

by_task = {}
for s in sessions["sessions"]:
    tid = s.get("task_id") or "(no-task)"
    by_task.setdefault(tid, []).append(s)

for tid, iters in by_task.items():
    print(f"Task {tid}: {len(iters)} active iterations")
```

---

## 7. YAML Scenario Schema

Khi gửi `scenario_yaml` hoặc nhận từ mode `query`, YAML có schema:

```yaml
id: my_scenario                 # slug, auto-override _q_xxx hoặc _custom_xxx
display_name: "Tên hiển thị"
description: |
  Mô tả ngắn
enabled: true
mode: flow                      # flow | agent | hybrid (default flow cho gen)
start_url: https://example.com
allowed_domains:
  - example.com
max_steps_default: 15

# Optional — declare output JSON cho action extract_data
output_schema:
  type: object
  properties:
    name:   { type: string, description: "Tên sản phẩm" }
    price:  { type: string, description: "Giá" }
    seller: { type: string, description: "Web bán" }
  required: [name, price, seller]
  additionalProperties: false

# Inputs declaration
inputs:
  - name: email
    type: string                # string | secret | number | bool
    required: true
    source: context             # context (từ body) | ask_user (hỏi runtime)
    description: "Email"
    default: null               # optional, bypass missing context

# Steps (action sequential)
steps:
  - action: wait_for            # goto | wait_for | fill | click | open_link
    target:                     # | if_visible | ask_user | eval_js
      role: textbox             # | upload_download | extract_data
      text_any: ["Email"]
    timeout_ms: 8000

  - action: fill
    target: { role: textbox, label_any: ["Password"] }
    # Chọn 1 trong 2:
    value: "literal text"       # literal string
    # value_from: email         # tên input đã declare

  - action: click
    target: { role: button, text_any: ["Login", "Đăng nhập"] }

  - action: ask_user
    field: otp
    prompt: "Nhập OTP"

  - action: if_visible
    target: { role: link, text_any: ["Logout"] }
    then: []                    # đã login → skip
    else: [<login flow>]

  - action: extract_data        # cần có output_schema ở top-level
    prompt: "Lấy thông tin sản phẩm"
```

### Action reference

| Action | Required fields | Mục đích |
|--------|------------------|----------|
| `goto` | `url` | Navigate URL |
| `wait_for` | `target` + (optional `timeout_ms`) | Đợi element xuất hiện |
| `fill` | `target` + (`value` HOẶC `value_from`) | Nhập text |
| `click` | `target` | Click element |
| `open_link` | `target` | Mở link same-tab (đảm bảo nav) |
| `if_visible` | `target` + `then[]` + `else[]` | Branch theo element có visible |
| `ask_user` | `field` + `prompt` | Hỏi user runtime qua SSE |
| `eval_js` | `script` | Chạy JS (workaround inline onclick) |
| `upload_download` | (optional `extensions[]`, `timeout_ms`) | Đợi file download → upload CDN |
| `extract_data` | (optional `prompt`) | LLM extract JSON theo `output_schema` |

### Role hợp lệ trong target

- `button`, `link`, `image`, `heading`
- `textbox`, `combobox`, `searchbox`, `textarea` (input variants)

**Lưu ý cho Sup Agent**:
- Worker có cross-role fallback (textbox ↔ combobox ↔ searchbox ↔ textarea)
- Google/Bing search box thực tế là `combobox` (có autocomplete)
- HTML5 `<input type="search">` là `searchbox`
- Rich editor (Gmail compose) là `textbox` (contenteditable)

---

## 8. Error reference

### Common HTTP status codes

| Code | Meaning | Typical fix |
|------|---------|-------------|
| 200 | OK | — |
| 201 | Created (POST run thành công) | — |
| 400 | Bad request format | Check body JSON |
| 404 | Resource not found | Task/session/iteration không tồn tại hoặc đã expire |
| 409 | Conflict (state không cho phép) | Check `detail` để biết SESSION_NOT_WAITING / SESSION_FINISHED / MULTIPLE_WAITING |
| 422 | Validation error | Body field invalid |
| 500 | Server error (OPENAI_API_KEY thiếu, internal bug) | Báo admin |
| 502 | LLM call fail | Retry sau hoặc dùng `scenario_yaml` thay query |
| 503 | Queue full | Retry sau, hoặc giảm rate |

### Specific error detail values

| Detail | Endpoint | Nguyên nhân |
|--------|----------|-------------|
| `Chỉ được set 1 trong 2 field: 'scenario_yaml' hoặc 'query'.` | POST run | Body có cả 2 field |
| `OPENAI_API_KEY not set` | POST run | Backend chưa config OpenAI key |
| `LLM generate fail: <error>` | POST run mode query | OpenAI API call lỗi |
| `YAML gen từ query parse fail: ...` | POST run mode query | LLM trả YAML invalid |
| `YAML inline parse fail: ...` | POST run mode yaml | Body `scenario_yaml` malformed |
| `Scenario '<id>' không tồn tại` | POST run mode scenario | scenario id không có trong DB |
| `Thiếu field context bắt buộc: [...]` | POST run | Inputs required missing trong context, không có default, không tự convert ask_user |
| `SESSION_NOT_WAITING` | POST resume | Iter latest không ở waiting_for_user |
| `SESSION_FINISHED` | POST cancel/resume | Iter đã done/failed/cancelled |
| `MULTIPLE_WAITING — task có N iterations đang waiting...` | POST resume | Có >1 iter cùng waiting (parallel mode) |
| `Queue is full. Try again later.` | POST run | >100 jobs pending queue |
| `Task '<id>' không có iteration nào` | GET task-centric endpoints | Task chưa được tạo iter |

---

## 9. Best practices

### 9.1 Caching YAML gen từ query

LLM gen mất ~3-5s và tốn ~$0.0003/call. Sup Agent **nên cache** YAML theo fingerprint query:

```python
import hashlib

def query_fingerprint(query: str) -> str:
    return hashlib.sha256(query.lower().strip().encode()).hexdigest()[:16]

# Lần đầu user query
fp = query_fingerprint(user_query)
yaml_cache = redis_get(f"yaml_cache:{fp}")

if yaml_cache:
    body = {"scenario_yaml": yaml_cache, ...}      # skip LLM call
else:
    body = {"query": user_query, ...}              # gen mới

resp = requests.post(f"{API}/v1/tasks/{task_id}/run", json=body)

if resp.get("scenario_yaml"):
    redis_set(f"yaml_cache:{fp}", resp["scenario_yaml"], ex=86400)   # 24h
```

### 9.2 Timeout cho HTTP request

| Endpoint | Timeout đề xuất | Lý do |
|----------|-----------------|-------|
| `POST /v1/tasks/{id}/run` (query mode) | 15s | LLM gen ~3-5s |
| `POST /v1/tasks/{id}/run` (yaml/scenario) | 5s | Chỉ validate + enqueue |
| `GET /v1/tasks/{id}/stream` | None (SSE, persistent) | — |
| `POST /v1/tasks/{id}/resume` | 3s | Push Redis queue |
| `POST /v1/tasks/{id}/cancel` | 3s | Set flag |
| `GET /v1/tasks/{id}/result` | 5s | Read file local |
| `GET /v1/tasks/{id}/status` | 3s | Read Redis |

### 9.3 task_id naming convention

- Sử dụng prefix để phân loại: `t-`, `chat-`, `linear-`, `slack-`
- Length 8-64 chars (giới hạn API 128 chars)
- Không dùng PII trong task_id (xuất hiện trong log)

**Examples:**
- `t-a1b2c3d4e5f6` (random)
- `linear-PROJ-1234` (cross-system trace với Linear)
- `chat-uuid-here` (Sup Agent conversation id)

### 9.4 Xử lý ask_user timeout

Worker timeout chờ user answer sau ~5 phút (`ask_deadline_at`). Nếu user không respond → event `timed_out`. Sup Agent nên:

```python
import asyncio

async def handle_ask_with_timeout(ask_message, timeout=240):
    """Hỏi user, timeout 4 phút (trước worker timeout 5 phút)."""
    try:
        answer = await asyncio.wait_for(ask_user(ask_message), timeout=timeout)
        return answer
    except asyncio.TimeoutError:
        # User không respond → cancel session để worker free
        requests.post(f"{API}/v1/tasks/{task_id}/cancel", headers=HEADERS)
        return None
```

### 9.5 Idempotency

POST `/v1/tasks/{task_id}/run` **KHÔNG idempotent** — gọi 2 lần → 2 iter mới. Sup Agent retry network fail cần check qua `/status` trước:

```python
def safe_run_task(task_id, body, max_retries=3):
    for attempt in range(max_retries):
        try:
            resp = requests.post(f"{API}/v1/tasks/{task_id}/run",
                                 headers=HEADERS, json=body, timeout=15)
            return resp.json()
        except requests.Timeout:
            # Check xem iter đã được tạo chưa
            status = requests.get(f"{API}/v1/tasks/{task_id}/status",
                                  headers=HEADERS).json()
            if status.get("current_status") in ("queued", "assigned", "running"):
                # Đã tạo, không cần retry
                return {"session_id": status["current_session_id"],
                        "iteration": status["current_iteration"]}
        except requests.ConnectionError:
            time.sleep(2 ** attempt)
    raise RuntimeError("Cannot reach Chang Browser API")
```

---

## 10. Migration từ /v1/sessions cũ

API cũ `POST /v1/sessions` vẫn hoạt động (deprecated). Sup Agent migrate dần:

| Cũ | Mới |
|----|-----|
| `POST /v1/sessions` với body `{task_id, query, ...}` | `POST /v1/tasks/{task_id}/run` với body `{query, ...}` |
| `GET /v1/sessions/{id}/stream` (dùng session_id từ response) | `GET /v1/tasks/{task_id}/stream` |
| `POST /v1/sessions/{id}/resume` | `POST /v1/tasks/{task_id}/resume` |
| `POST /v1/sessions/{id}/cancel` | `POST /v1/tasks/{task_id}/cancel` |
| `GET /v1/sessions/{id}/result` | `GET /v1/tasks/{task_id}/result` |
| `GET /v1/sessions/{id}` | `GET /v1/tasks/{task_id}/status` |

**Migration step-by-step:**

1. **Phase 1**: Đổi POST từ `/v1/sessions` → `/v1/tasks/{task_id}/run`. Vẫn dùng session_id từ response cho stream/resume/cancel.
2. **Phase 2**: Đổi stream/resume/cancel sang task-centric. Bỏ lưu session_id (chỉ lưu task_id).
3. **Phase 3**: Cleanup — remove session_id từ Sup Agent's database/cache.

---

## 11. Admin endpoints (tham khảo)

Cần `X-Admin-Token` header.

| URL | Mục đích |
|-----|----------|
| `GET /v1/admin/sessions?task_id=X&include_finished=false` | List sessions filter theo task |
| `GET /v1/admin/tasks/{task_id}/sessions` | List iterations của task + has_running, latest_iteration |
| `GET /v1/admin/workers` | List worker pool + heartbeat |
| `POST /v1/admin/sessions/{session_id}/cancel` | Admin cancel session bất kỳ |
| `POST /v1/browser/kill-all` | Emergency: cancel all + restart worker pod |

---

## 12. Health & Debug

| URL | Mục đích |
|-----|----------|
| `GET /v1/health` | Workers alive/busy + queue length |
| `GET /v1/auth/me` | Verify auth headers |
| `GET /v1/debug/test-upload` | Test CDN upload connection |
| `GET /v1/debug/runner-logs?session_id=X` | List local artifact files |
| `GET /v1/debug/scenarios?reseed=false` | Inspect Redis scenarios + hooks |

---

## 13. Changelog API

| Date | Change |
|------|--------|
| 2026-05-15 | Add task-centric API (POST/GET/stream/resume/cancel/result/status) |
| 2026-05-15 | Add `output_schema` + action `extract_data` cho JSON output động |
| 2026-05-14 | Add mode `query` trong POST /v1/sessions (LLM gen YAML inline) |
| 2026-05-14 | Add Admin Sessions Monitor endpoints + session `name` field |
| 2026-05-14 | Fix worker scaling — bỏ WORKER_ID hardcode (support 10 replicas) |

---

## 14. Liên hệ

- API repo: `gitlab.dsc.com/fpl/aichatbot/chang-browser-api`
- Worker repo: `gitlab.dsc.com/fpl/aichatbot/chang-browser-worker`
- WebUI test: `github.com/PhuongMai1501/web-UI`
- Owner: hiepqn@fpt.com

Báo lỗi: tạo issue ở GitLab repo hoặc Slack channel `#chang-browser-api`.
