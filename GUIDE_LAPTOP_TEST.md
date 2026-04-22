# Hướng dẫn test API mới từ laptop (kịch bản `search_thuvienphapluat`)

> Dành cho: test Sprint 1 (`mode=flow`) bằng scenario mẫu [kichban_1.md](kichban_1.md) —
> mở thuvienphapluat.vn, điền từ khoá, bấm tìm kiếm.

---

## 0. Cấu trúc 2 máy

```
┌─────────────────────┐              ┌────────────────────────────┐
│ Laptop (local PC)   │  HTTP/SSE    │ Server (Linux + Chrome)    │
│                     │ ───────────▶ │                            │
│ - curl / Postman    │              │ - Redis :6379              │
│ - web_UI_test Vite  │              │ - FastAPI :9000            │
│   (optional)        │              │ - Worker + Chrome headless │
└─────────────────────┘              └────────────────────────────┘
```

Phần lớn việc config nằm trên **server**. Laptop chỉ gửi HTTP.

---

## 1. Chuẩn bị trên server (chạy 1 lần)

### 1.1. Kiểm tra prereq

```bash
ssh <user>@<SERVER_IP>
cd /changAI/research/browser_node/deploy_server

# Redis
redis-cli ping                              # phải ra PONG

# Chrome + agent-browser
google-chrome --version
agent-browser --version

# Conda env có deps
/root/miniconda3/envs/browser_local/bin/python -c \
  "import fastapi, uvicorn, redis, openai, yaml; print('ok')"

# .env đã có OPENAI_API_KEY
grep OPENAI_API_KEY .env

# Firewall port 9000 mở
sudo ufw status | grep 9000 || sudo ufw allow 9000/tcp
```

Nếu thiếu phần nào → xem [DEPLOY.md](DEPLOY.md) Phần 1.

### 1.2. Set `ADMIN_TOKEN` (bắt buộc cho admin API)

Nếu không set, endpoints `/v1/scenarios` sẽ trả 503. Thêm vào `.env`:

```bash
echo "ADMIN_TOKEN=$(openssl rand -hex 24)" >> .env
grep ADMIN_TOKEN .env        # copy token này, cần ở laptop
```

Lưu token vào ghi chú — laptop sẽ dùng qua header `X-Admin-Token`.

---

## 2. Start services (3 process)

Dùng 3 terminal hoặc tmux (`tmux new -s chang`, `Ctrl+b c` để mở tab mới).

### 2.1. Redis

Nếu chạy qua systemd → bỏ qua. Nếu chưa:

```bash
bash start_redis.sh
```

### 2.2. API

```bash
cd /changAI/research/browser_node/deploy_server
bash start_api.sh
```

Đợi log:
```
Seeded builtin scenario: chang_login
Seeded builtin scenario: custom
Seeded builtin scenario: login_basic
API started. Recovery loop running.
Uvicorn running on http://0.0.0.0:9000
```

### 2.3. Worker (1 hoặc nhiều)

```bash
bash start_worker.sh 1
```

Đợi: `[worker-1] Worker started, waiting for jobs`.

### 2.4. Sanity check từ server

```bash
curl http://localhost:9000/v1/health
# {"status":"ok","workers_alive":1,"workers_busy":0,"queue_length":0}
```

Nếu `workers_alive=0` → worker chưa start hoặc chưa register vào Redis.

---

## 3. Test từ laptop — dùng curl

> Thay `SERVER_IP` và `ADMIN_TOKEN` bên dưới thành giá trị thật.

### 3.1. Biến môi trường trên laptop

Terminal trên **laptop**:

```bash
export SERVER=http://SERVER_IP:9000
export ADMIN_TOKEN=<token copy từ bước 1.2>

# Sanity
curl $SERVER/v1/health
# {"status":"ok",...}
```

### 3.2. Tạo file `search_thuvienphapluat.json` trên laptop

```bash
cat > search_thuvienphapluat.json << 'EOF'
{
  "id": "search_thuvienphapluat",
  "display_name": "Tìm kiếm trên Thư Viện Pháp Luật",
  "description": "Mở trang thuvienphapluat.vn và tìm kiếm từ khóa",
  "mode": "flow",
  "start_url": "https://thuvienphapluat.vn",
  "allowed_domains": ["thuvienphapluat.vn"],
  "inputs": [
    {
      "name": "keyword",
      "type": "string",
      "required": true,
      "source": "context"
    }
  ],
  "steps": [
    {
      "action": "wait_for",
      "target": {
        "placeholder_any": ["Tìm kiếm", "Nhập từ khóa", "Search"]
      },
      "timeout_ms": 10000
    },
    {
      "action": "fill",
      "target": {
        "placeholder_any": ["Tìm kiếm", "Nhập từ khóa", "Search"]
      },
      "value_from": "keyword"
    },
    {
      "action": "click",
      "target": {
        "text_any": ["Tìm kiếm", "Search"]
      }
    }
  ],
  "success": {
    "any_of": [
      { "url_contains": "tim-kiem" },
      { "text_any": ["Kết quả tìm kiếm", "nghị định"] }
    ]
  },
  "failure": {
    "any_of": [
      { "text_any": ["Không tìm thấy", "Có lỗi xảy ra"] }
    ],
    "code": "SEARCH_FAILED",
    "message": "Tìm kiếm thất bại"
  }
}
EOF
```

### 3.3. Validate trước khi tạo (dry-run)

```bash
curl -X POST \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d @search_thuvienphapluat.json \
  $SERVER/v1/scenarios/search_thuvienphapluat/dry-run
```

**Expected (200):** `{"status":"valid","id":"search_thuvienphapluat","hooks":{...}}`

**Nếu 422:** validator đã bắt lỗi spec (action lạ, value_from không khớp input, etc.). Sửa JSON theo message → dry-run lại.

### 3.4. Tạo thật

```bash
curl -X POST \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d @search_thuvienphapluat.json \
  $SERVER/v1/scenarios
```

**Expected (201):** full spec JSON với `builtin: false`, `version: 1`.

Kiểm tra đã xuất hiện trong list:

```bash
curl -H "X-Admin-Token: $ADMIN_TOKEN" $SERVER/v1/scenarios | python3 -m json.tool
```

### 3.5. Chạy session test

```bash
# Tạo session
RESP=$(curl -sS -X POST \
  -H "Content-Type: application/json" \
  -d '{"scenario":"search_thuvienphapluat","context":{"keyword":"nghị định"}}' \
  $SERVER/v1/sessions)
echo "$RESP" | python3 -m json.tool
SID=$(echo "$RESP" | python3 -c "import json,sys;print(json.load(sys.stdin)['session_id'])")
echo "session_id=$SID"
```

### 3.6. Xem stream events (SSE)

```bash
curl -N $SERVER/v1/sessions/$SID/stream
```

Expected events:
```
event: step       → wait_for form tìm kiếm
event: step       → fill "nghị định"
event: step       → click nút tìm kiếm
event: done       → Flow hoàn thành — success rule đạt
```

Nếu gặp `event: ask` → scenario này không cần hỏi user, nên không xảy ra.

Ctrl+C để đóng stream.

### 3.7. Polling thay cho SSE (nếu SSE bị firewall chặn)

```bash
while :; do
  curl -sS $SERVER/v1/sessions/$SID | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d['status'], 'step:', d.get('current_step'), 'err:', d.get('error_msg') or '-')"
  sleep 2
done
```

Kết thúc khi `status` = `done`/`failed`/`cancelled`.

### 3.8. Kết quả chi tiết (`result.json`)

```bash
curl -sS $SERVER/v1/sessions/$SID/result | python3 -m json.tool
```

Gồm `status`, `summary`, `total_steps`, `duration_seconds`, từng step với screenshot path.

---

## 4. Test từ laptop — dùng UI web_UI_test (Vite)

### 4.1. Cấu trúc

UI frontend ở laptop, thư mục `web_UI_test/` (từ repo cũ `web_brower`, không nằm trong `deploy_server`). Phiên bản hiện tại trỏ `localhost:8000` — cần sửa.

### 4.2. Chỉnh vite.config.js

Mở `web_UI_test/vite.config.js` trên laptop:

```js
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/v1': {
        target: 'http://SERVER_IP:9000',   // ← đổi IP + port 9000
        changeOrigin: true,
      },
    },
  },
})
```

### 4.3. Chạy UI

```bash
cd web_UI_test
npm install        # lần đầu
npm run dev
# http://localhost:5173
```

### 4.4. Nếu UI chưa hỗ trợ scenario `search_thuvienphapluat`

UI hiện tại (từ v1) có thể mới chỉ chọn `chang_login` / `custom`. 3 lựa chọn:

**A. Sửa UI hardcode** — trong component chọn scenario, thêm option:
```jsx
<option value="search_thuvienphapluat">Tìm kiếm pháp luật</option>
```
Và ô input `keyword` thay cho `email/password`.

**B. Fetch danh sách scenario động từ API** (đề xuất):
```javascript
useEffect(() => {
  fetch('/v1/scenarios', { headers: { 'X-Admin-Token': ADMIN_TOKEN } })
    .then(r => r.json())
    .then(setScenarios)
}, [])
```
Render dropdown theo `scenarios.map(s => <option value={s.id}>{s.display_name}</option>)`.

**C. Không sửa UI** — dùng Postman hoặc curl ở §3 để test kỹ thuật; UI làm sau.

### 4.5. CORS

[ai_tool_web/api/app.py:41-46](ai_tool_web/api/app.py#L41-L46) đã set `allow_origins=["*"]` → UI có thể gọi trực tiếp `http://SERVER_IP:9000/v1/...` không qua Vite proxy. Chỉ cần set base URL trong UI:

```javascript
// web_UI_test/src/config.js (hoặc tương đương)
export const API_BASE = 'http://SERVER_IP:9000'
```

Lưu ý: `X-Admin-Token` header chỉ cần khi gọi `/v1/scenarios/*` (admin). Các endpoint `/v1/sessions*` không cần token.

---

## 5. Debug khi flow fail

### 5.1. Xem snapshot thật của trang

Chạy session, copy `session_id`, rồi xem worker log:

```bash
# trên server
tail -f /changAI/research/browser_node/deploy_server/logs/system/worker.log | grep $SID
```

Hoặc bật stream verbose:

```bash
# trên laptop
curl -N $SERVER/v1/sessions/$SID/stream 2>&1 | head -200
```

StepRecord có field `snapshot` chứa toàn bộ accessibility tree. Tìm phần có từ khoá mình mong đợi (ví dụ "Tìm kiếm") — nếu không thấy → target matcher sẽ fail.

### 5.2. Target không match — sửa gì

| Symptom | Fix |
|---------|-----|
| `wait_for` timeout | `placeholder_any` không khớp. Thử `text_any` hoặc đổi sang `role: textbox` + `nth: 0` (ô input đầu tiên) |
| `click` không tìm thấy | Nút search có thể là `img`/`svg`, không có text. Dùng `role: button` + `nth` hoặc `css` escape hatch |
| Flow done ngay step 1 | `success` rule quá rộng. `text_any: ["nghị định"]` có thể match ngay landing page nếu keyword đã nằm trong menu. Bỏ nó đi, chỉ giữ `url_contains: tim-kiem` |

### 5.3. Update scenario qua PUT

Admin có thể sửa spec runtime, không cần redeploy:

```bash
# Sửa JSON local, rồi
curl -X PUT \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d @search_thuvienphapluat.json \
  $SERVER/v1/scenarios/search_thuvienphapluat
```

Version sẽ bump. Lưu ý: spec sửa không ảnh hưởng session đang chạy (đã snapshot tại enqueue); session mới dùng spec mới.

### 5.4. Xoá scenario đã tạo

```bash
curl -X DELETE \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  $SERVER/v1/scenarios/search_thuvienphapluat
```

Builtin (`chang_login`, `custom`, `login_basic`) không xoá được — dùng `PUT` với `enabled: false` nếu muốn disable.

---

## 6. Checklist trước khi test lần đầu

Trên **server**:
- [ ] `redis-cli ping` → PONG
- [ ] `curl http://localhost:9000/v1/health` → `workers_alive ≥ 1`
- [ ] `grep ADMIN_TOKEN .env` → có giá trị
- [ ] Port 9000 mở ra ngoài (`sudo ufw allow 9000/tcp`)
- [ ] `agent-browser --version` và `google-chrome --version` đều chạy

Trên **laptop**:
- [ ] `curl http://SERVER_IP:9000/v1/health` → ok
- [ ] `ADMIN_TOKEN` đã export
- [ ] `jq` hoặc `python3` cài sẵn để parse JSON output

---

## 7. Flow tóm tắt

```
Server:
  start_redis.sh  ─┐
  start_api.sh    ─┼── đã boot, seed 3 builtin scenarios
  start_worker.sh ─┘

Laptop:
  1. POST /v1/scenarios/search_thuvienphapluat/dry-run    ← validate spec
  2. POST /v1/scenarios                                    ← tạo thật
  3. POST /v1/sessions {scenario:"search_thuvienphapluat"} ← chạy
  4. GET  /v1/sessions/{id}/stream                         ← xem event
  5. Nếu fail → sửa JSON → PUT /v1/scenarios/{id} → chạy lại
```

---

## 8. Phụ lục — mapping nhanh

| Endpoint | Phương thức | Auth | Khi nào dùng |
|----------|-------------|------|--------------|
| `/v1/health` | GET | - | Sanity check |
| `/v1/scenarios` | GET/POST | X-Admin-Token | List / tạo mới |
| `/v1/scenarios/{id}` | GET/PUT/DELETE | X-Admin-Token | Xem / sửa / xoá |
| `/v1/scenarios/{id}/dry-run` | POST | X-Admin-Token | Validate không lưu |
| `/v1/sessions` | POST | - | Bắt đầu chạy scenario |
| `/v1/sessions/{id}` | GET | - | Polling status |
| `/v1/sessions/{id}/stream` | GET | - | SSE events |
| `/v1/sessions/{id}/resume` | POST | - | Trả lời `ask` event |
| `/v1/sessions/{id}/cancel` | POST | - | Huỷ session |
| `/v1/sessions/{id}/result` | GET | - | Kết quả chi tiết sau khi done |

Full API ref: [API.md](API.md).
