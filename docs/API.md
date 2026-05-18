# AI Tool Web — API Reference

Base URL: `http://<SERVER_IP>:8000`

---

## Luồng hoạt động cơ bản

```
1. POST /v1/sessions          → tạo session, nhận session_id
2. GET  /v1/sessions/{id}/stream  → mở SSE stream, lắng nghe events
3.   event: step              → mỗi bước agent làm
4.   event: ask               → agent cần hỏi user
     POST /v1/sessions/{id}/resume  → trả lời cho agent
5.   event: done / failed / cancelled / timed_out  → kết thúc, đóng stream
```

---

## Endpoints

### GET /v1/health

Kiểm tra server và số worker đang chạy.

**Response**
```json
{
  "status": "ok",
  "workers_alive": 50,
  "workers_busy": 3,
  "queue_length": 0
}
```

---

### POST /v1/sessions

Tạo session mới. Server đẩy job vào queue, worker sẽ nhận và chạy.

**Request body**
```json
{
  "scenario": "chang_login",
  "context": {
    "email": "user@fpt.net",
    "password": "secret"
  },
  "max_steps": 20
}
```

| Field | Type | Required | Mô tả |
|-------|------|----------|-------|
| `scenario` | `"chang_login" \| "custom"` | có | Kịch bản chạy |
| `context` | object | không | Dữ liệu đầu vào cho kịch bản (email, password...) |
| `goal` | string | không | Chỉ dùng khi `scenario="custom"` |
| `url` | string | không | Chỉ dùng khi `scenario="custom"` |
| `max_steps` | int (3–30) | không | Số bước tối đa, mặc định 20 |

**Response — 201 Created**
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued",
  "stream_url": "/v1/sessions/550e8400-e29b-41d4-a716-446655440000/stream",
  "created_at": "2024-01-01T00:00:00Z",
  "queue_position": 1
}
```

**Lỗi**
| Code | Detail |
|------|--------|
| 503 | Queue full, thử lại sau |
| 500 | OPENAI_API_KEY chưa set trên server |

---

### GET /v1/sessions/{session_id}

Lấy trạng thái hiện tại của session (polling thay thế khi không dùng SSE).

**Response**
```json
{
  "session_id": "550e8400-...",
  "status": "running",
  "scenario": "chang_login",
  "current_step": 3,
  "max_steps": 20,
  "created_at": "2024-01-01T00:00:00Z",
  "assigned_worker": "worker-5",
  "ask_deadline_at": null,
  "error_msg": null,
  "finished_at": null
}
```

**Các giá trị `status`**
| Status | Ý nghĩa |
|--------|---------|
| `queued` | Đang chờ trong hàng đợi |
| `assigned` | Worker đã nhận, chuẩn bị chạy |
| `running` | Agent đang chạy |
| `waiting_for_user` | Agent bị block, cần user trả lời |
| `done` | Hoàn thành thành công |
| `failed` | Thất bại |
| `cancelled` | Đã huỷ |
| `timed_out` | Hết giờ chờ user trả lời |

**Lỗi**
| Code | Detail |
|------|--------|
| 404 | Session không tồn tại |

---

### GET /v1/sessions/{session_id}/stream

Nhận events realtime qua SSE. **Giữ kết nối mở** cho đến khi nhận event terminal.

**Query params**
| Param | Type | Mô tả |
|-------|------|-------|
| `lastEventId` | int | Reconnect: replay events sau event_id này. Mặc định 0 (replay tất cả) |

**Response headers**
```
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no
```

**Format mỗi event**
```
id: 5
event: step
data: {"step": 3, "action": "click", ...}

```
> Mỗi event kết thúc bằng 2 dòng trống `\n\n`

**Lỗi**
| Code | Detail |
|------|--------|
| 404 | Session không tồn tại |

---

### POST /v1/sessions/{session_id}/resume

Gửi câu trả lời khi agent đang ở trạng thái `waiting_for_user` (sau khi nhận event `ask`).

**Request body**
```json
{
  "answer": "Câu trả lời của user"
}
```

**Response**
```json
{
  "status": "resumed",
  "session_id": "550e8400-..."
}
```

**Lỗi**
| Code | Detail |
|------|--------|
| 404 | Session không tồn tại |
| 409 | `SESSION_FINISHED` — session đã kết thúc |
| 409 | `SESSION_NOT_WAITING` — session không ở trạng thái chờ |

---

### POST /v1/sessions/{session_id}/cancel

Huỷ session đang chạy.

**Request body:** không có

**Response**
```json
{
  "status": "cancelled",
  "steps_completed": 5
}
```

**Lỗi**
| Code | Detail |
|------|--------|
| 404 | Session không tồn tại |
| 409 | `SESSION_FINISHED` — session đã kết thúc |

---

### POST /v1/browser/reset

Huỷ tất cả session đang active. Dùng khi muốn reset toàn bộ.

**Request body:** không có

**Response**
```json
{
  "status": "reset",
  "session_cancelled": "550e8400-..."
}
```
> `session_cancelled` là `null` nếu không có session nào đang chạy.

---

### GET /v1/sessions/{session_id}/steps/{step_number}/screenshot

Lấy ảnh chụp màn hình của 1 step.

**Query params**
| Param | Type | Mô tả |
|-------|------|-------|
| `annotated` | bool | `true` để lấy ảnh có đánh dấu element. Mặc định `false` |

**Response:** file PNG

---

## SSE Event Types

### `step` — Agent vừa thực hiện 1 hành động

```json
{
  "step": 3,
  "action": "click",
  "ref": "e12",
  "text_typed": "",
  "reason": "Nhấn nút đăng nhập",
  "url_before": "https://fpt.net/login",
  "url_after": "https://fpt.net/dashboard",
  "screenshot_url": "/v1/sessions/{id}/steps/3/screenshot",
  "annotated_screenshot_url": "/v1/sessions/{id}/steps/3/screenshot?annotated=true",
  "has_error": false,
  "error": "",
  "visual_fallback_used": false,
  "timestamp": "2024-01-01T00:00:01Z"
}
```

**Các giá trị `action`:** `click`, `type`, `wait`, `ask`, `done`

---

### `ask` — Agent bị block, cần user trả lời

Sau khi nhận event này, gọi `POST /resume` để tiếp tục.

```json
{
  "step": 4,
  "ask_type": "question",
  "message": "Cần nhập mã OTP được gửi đến email của bạn",
  "reason": "Trang yêu cầu xác thực 2 bước",
  "screenshot_url": "/v1/sessions/{id}/steps/4/screenshot",
  "timestamp": "2024-01-01T00:00:05Z"
}
```

**`ask_type`:** `"question"` | `"error"`

---

### `done` — Hoàn thành (terminal)

```json
{
  "step": 8,
  "message": "Đăng nhập thành công",
  "url_after": "https://fpt.net/dashboard",
  "screenshot_url": "/v1/sessions/{id}/steps/8/screenshot",
  "total_steps": 8,
  "duration_seconds": 42.5,
  "timestamp": "2024-01-01T00:00:42Z"
}
```

---

### `failed` — Thất bại (terminal)

```json
{
  "code": "RATE_LIMIT",
  "message": "OpenAI API rate limit. Vui lòng thử lại sau vài phút.",
  "recoverable": false,
  "timestamp": "2024-01-01T00:00:10Z"
}
```

**Các `code` lỗi:**
| Code | Ý nghĩa |
|------|---------|
| `RATE_LIMIT` | OpenAI rate limit |
| `BROWSER_TIMEOUT` | Chrome không phản hồi |
| `LLM_INVALID_RESPONSE` | LLM trả về JSON lỗi |
| `CONNECTION_ERROR` | Mất kết nối mạng |
| `DOMAIN_BLOCKED` | URL không nằm trong allowlist |
| `SESSION_TIMEOUT` | Session chạy quá 10 phút |
| `INTERNAL_ERROR` | Lỗi không xác định |

---

### `cancelled` — Đã huỷ (terminal)

```json
{
  "reason": "Cancelled by user"
}
```

---

### `timed_out` — Hết giờ chờ user (terminal)

```json
{
  "elapsed_seconds": 300,
  "message": "Không nhận được câu trả lời sau 300s."
}
```

---

### `heartbeat` — Keep-alive (không phải data)

```
event: heartbeat
data: {}

```
> Gửi mỗi 15s khi không có event nào. Bỏ qua ở phía client.

---

## Ví dụ code client (JavaScript)

```javascript
const SERVER = 'http://localhost:8000'

// 1. Tạo session
const res = await fetch(`${SERVER}/v1/sessions`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    scenario: 'chang_login',
    context: { email: 'user@fpt.net', password: 'secret' },
    max_steps: 20,
  }),
})
const { session_id, stream_url } = await res.json()

// 2. Mở SSE stream
const es = new EventSource(`${SERVER}${stream_url}`)

es.addEventListener('step', (e) => {
  const data = JSON.parse(e.data)
  console.log(`Step ${data.step}: ${data.action} — ${data.reason}`)
})

es.addEventListener('ask', async (e) => {
  const data = JSON.parse(e.data)
  const answer = prompt(data.message)   // hoặc hiện UI dialog
  await fetch(`${SERVER}/v1/sessions/${session_id}/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ answer }),
  })
})

es.addEventListener('done', (e) => {
  console.log('Done!', JSON.parse(e.data))
  es.close()
})

es.addEventListener('failed', (e) => {
  console.error('Failed:', JSON.parse(e.data))
  es.close()
})

es.addEventListener('cancelled', () => es.close())
es.addEventListener('timed_out', () => es.close())

// Cancel nếu cần
async function cancel() {
  await fetch(`${SERVER}/v1/sessions/${session_id}/cancel`, { method: 'POST' })
}
```

---

## Domain allowlist (browser)

Agent chỉ được phép điều hướng đến các domain sau:

- `fpt.net`
- `microsoftonline.com`
- `microsoft.com`
- `live.com`
- `office.com`
- `sharepoint.com`
