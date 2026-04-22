# Kịch bản test Scenario v2 — Sprint 1

> Hướng dẫn verify Sprint 1 — flow declarative. Có 3 layer test:
> 1. **Unit** — chạy local, không cần Redis/Chrome.
> 2. **Integration** — cần Redis + API, không cần Chrome.
> 3. **E2E browser** — cần Chrome + `agent-browser` + OpenAI key, chạy thật trên site thật.

---

## 1. Unit test (đã viết sẵn, 18/18 xanh)

File: [ai_tool_web/tests/test_flow_v2.py](ai_tool_web/tests/test_flow_v2.py).

```bash
cd /changAI/research/browser_node/deploy_server/ai_tool_web
/root/miniconda3/envs/browser_local/bin/python -m unittest tests.test_flow_v2 -v
```

Coverage:

| Test class | Gì được kiểm |
|------------|--------------|
| `TestSnapshotQuery` | parse snapshot 7 dòng, match label/text/placeholder/role, diacritic-insensitive, nth, không match → None |
| `TestFlowModels` | `TargetSpec` yêu cầu ≥1 field, `FlowStep.else_` alias, Pydantic parse |
| `TestFlowRunner` | happy path 4 steps + done, failure rule trigger, ask_user pause/resume, missing target fail |
| `TestValidator` | flow thiếu steps → reject, action lạ → reject, value_from lạ → reject |

**Expected output:** `Ran 18 tests in ~0.5s - OK`.

---

## 2. Integration test (API + Redis, stub browser)

### 2.1. Boot

```bash
# T1: Redis
redis-cli ping   # PONG
redis-cli FLUSHDB

# T2: API
cd /changAI/research/browser_node/deploy_server
ADMIN_TOKEN=testtoken bash start_api.sh
```

Đợi thấy log: `Seeded 3 builtin scenarios` (chang_login + custom + login_basic).

### 2.2. Smoke endpoints

```bash
# Health
curl http://localhost:9000/v1/health
# {"status":"ok","workers_alive":0,...}

# List scenarios — phải thấy login_basic
curl -H "X-Admin-Token: testtoken" http://localhost:9000/v1/scenarios | python3 -m json.tool
```

**Expected:** 3 item, `login_basic` có `"builtin": true`.

### 2.3. Get scenario detail

```bash
curl -H "X-Admin-Token: testtoken" \
  http://localhost:9000/v1/scenarios/login_basic | python3 -m json.tool
```

**Expected:**
- `"mode": "flow"`
- `"steps"` có 5 phần tử (wait_for, fill, fill, click, if_visible)
- `"inputs"` có 3 phần tử (email, password, otp)
- `"success.any_of"` có 2 condition

### 2.4. Validation input

```bash
# Thiếu password (required)
curl -X POST -H "Content-Type: application/json" \
  -d '{"scenario":"login_basic","context":{"email":"x@y"}}' \
  http://localhost:9000/v1/sessions
# 422: {"detail":"Thiếu field context bắt buộc: ['password'] (scenario=login_basic)"}

# Scenario không tồn tại
curl -X POST -H "Content-Type: application/json" \
  -d '{"scenario":"abcxyz","context":{}}' \
  http://localhost:9000/v1/sessions
# 404: Scenario 'abcxyz' không tồn tại
```

### 2.5. Validation spec mới

```bash
# Action không tồn tại
curl -X POST -H "X-Admin-Token: testtoken" -H "Content-Type: application/json" \
  -d '{"id":"bad","display_name":"Bad","mode":"flow","steps":[{"action":"does_not_exist","target":{"role":"button"}}]}' \
  http://localhost:9000/v1/scenarios
# 422: steps[0]: action 'does_not_exist' chưa register. Action hợp lệ: ['ask_user','click','fill','goto','if_visible','wait_for']

# mode=flow thiếu steps
curl -X POST -H "X-Admin-Token: testtoken" -H "Content-Type: application/json" \
  -d '{"id":"x","display_name":"X","mode":"flow"}' \
  http://localhost:9000/v1/scenarios
# 422: mode='flow' yêu cầu ít nhất 1 step trong 'steps'

# value_from reference field không có trong inputs
curl -X POST -H "X-Admin-Token: testtoken" -H "Content-Type: application/json" \
  -d '{"id":"x","display_name":"X","mode":"flow","inputs":[{"name":"email","source":"context"}],"steps":[{"action":"fill","target":{"role":"textbox","label_any":["Email"]},"value_from":"missing_field"}]}' \
  http://localhost:9000/v1/scenarios
# 422: steps[0]: value_from='missing_field' không có trong inputs
```

### 2.6. Tạo scenario flow mới

```bash
curl -X POST -H "X-Admin-Token: testtoken" -H "Content-Type: application/json" \
  -d '{
    "id": "sandbox_open",
    "display_name": "Open sandbox page",
    "mode": "flow",
    "allowed_domains": ["example.com"],
    "steps": [
      {"action": "goto", "url": "https://example.com"},
      {"action": "wait_for", "timeout_ms": 2000}
    ]
  }' \
  http://localhost:9000/v1/scenarios | python3 -m json.tool
# 201: id=sandbox_open, mode=flow, builtin=false, version=1
```

**Expected:** spec xuất hiện trong Redis:

```bash
redis-cli SMEMBERS scenarios:index
# 1) "chang_login" 2) "custom" 3) "login_basic" 4) "sandbox_open"
```

### 2.7. spec_snapshot embed khi tạo session

```bash
SID=$(curl -sS -X POST -H "Content-Type: application/json" \
  -d '{"scenario":"sandbox_open","context":{}}' \
  http://localhost:9000/v1/sessions | python3 -c 'import json,sys;print(json.load(sys.stdin)["session_id"])')
echo "session=$SID"

redis-cli HGET "session:$SID" scenario_config | python3 -c "
import json, sys
d = json.loads(sys.stdin.read())
snap = d.get('spec_snapshot', {})
print('has snapshot:', bool(snap))
print('snapshot.mode:', snap.get('mode'))
print('snapshot.steps count:', len(snap.get('steps',[])))
"
# has snapshot: True
# snapshot.mode: flow
# snapshot.steps count: 2
```

### 2.8. Admin delete builtin bị chặn

```bash
curl -w "\nHTTP %{http_code}\n" -X DELETE -H "X-Admin-Token: testtoken" \
  http://localhost:9000/v1/scenarios/login_basic
# 409: Scenario 'login_basic' là builtin, không thể delete.
```

### 2.9. Cleanup

```bash
# Stop API
pkill -f "uvicorn api.app"
redis-cli FLUSHDB
```

---

## 3. E2E browser (cần Chrome + OpenAI key + agent-browser CLI)

### 3.1. Checklist trước khi chạy

```bash
redis-cli ping                  # PONG
agent-browser --version         # có
google-chrome --version         # có (hoặc chromium)
echo $OPENAI_API_KEY            # có set (check .env loaded)
```

### 3.2. Setup

```bash
cd /changAI/research/browser_node/deploy_server
redis-cli FLUSHDB

# T1 — API
ADMIN_TOKEN=testtoken bash start_api.sh

# T2 — Worker (cần Chrome)
bash start_worker.sh 1
```

Đợi worker log `[worker-1] Worker started, waiting for jobs`.

### 3.3. Kịch bản E2E-1 — flow không ask_user, không LLM

Sửa `login_basic.yaml` hoặc tạo spec mới trỏ đến site login đơn giản bạn kiểm soát được. Ví dụ dùng [httpbin.org/forms/post](https://httpbin.org/forms/post) (form public, không cần login):

```bash
curl -X POST -H "X-Admin-Token: testtoken" -H "Content-Type: application/json" \
  -d '{
    "id": "httpbin_form",
    "display_name": "HTTPBin form test",
    "mode": "flow",
    "start_url": "https://httpbin.org/forms/post",
    "allowed_domains": ["httpbin.org"],
    "inputs": [
      {"name": "custname", "type": "string", "required": true, "source": "context"},
      {"name": "custtel", "type": "string", "required": false, "source": "context"}
    ],
    "steps": [
      {"action": "wait_for", "target": {"role": "textbox", "placeholder_any":["Customer"]}, "timeout_ms": 6000},
      {"action": "fill", "target": {"role": "textbox", "placeholder_any":["Customer"]}, "value_from": "custname"},
      {"action": "click", "target": {"role": "button", "text_any":["Submit"]}}
    ],
    "success": {"any_of":[{"url_contains":"/post"}]}
  }' http://localhost:9000/v1/scenarios
```

Chạy session:

```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"scenario":"httpbin_form","context":{"custname":"tester"}}' \
  http://localhost:9000/v1/sessions

# Lấy session_id từ response
SID=<session_id>
curl -N "http://localhost:9000/v1/sessions/$SID/stream"
```

**Expected:**
- Event `step` với `action=wait`, `reason="Chờ..."`
- Event `step` với `action=type`, `text_typed="tester"`
- Event `step` với `action=click`
- Event `done` với message "Flow hoàn thành — success rule đạt"

**Browser behaviour:** Chrome mở form, điền "tester" vào customer name, click submit, redirect sang `/post` → done.

**Red flags:**
- Nếu `wait_for` fail timeout → placeholder matcher chưa đúng. Mở `redis-cli MONITOR` hoặc `tail -f logs/system/worker.log` để xem snapshot.
- Nếu `click` fail "Không tìm thấy target" → submit button có text khác. Thử thêm `text_any:["Submit order","Gửi"]`.

### 3.4. Kịch bản E2E-2 — flow với ask_user (OTP)

Trên site có OTP thật (hoặc dùng site test có form OTP mock). Spec như `login_basic.yaml`:

```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"scenario":"login_basic","context":{"email":"user@fpt.net","password":"...real..."}}' \
  http://localhost:9000/v1/sessions
SID=<session_id>
curl -N "http://localhost:9000/v1/sessions/$SID/stream"
```

**Expected flow trong SSE:**
1. Events `step` cho wait_for, fill email, fill pwd, click login.
2. Nếu trang hiện OTP → event `ask` với `message: "Vui lòng nhập mã OTP"`.
3. User gửi OTP:
   ```bash
   curl -X POST -H "Content-Type: application/json" \
     -d '{"answer":"123456"}' \
     http://localhost:9000/v1/sessions/$SID/resume
   ```
4. Events tiếp: fill OTP, click verify.
5. Event `done` khi match success rule.

### 3.5. Kịch bản E2E-3 — back-compat với mode=agent (v1)

Chạy lại `chang_login` để verify v2 không làm vỡ v1:

```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"scenario":"chang_login","context":{"email":"...","password":"..."}}' \
  http://localhost:9000/v1/sessions
SID=<session_id>
curl -N "http://localhost:9000/v1/sessions/$SID/stream"
```

**Expected:** behaviour giống hệt trước refactor — hooks `chang_login.pre_check` / `post_step` chạy, LLM autonomous quyết action, DOM success detection trigger done. Số step, thứ tự action không đổi so với baseline.

---

## 4. Regression — run lại test cũ

```bash
cd ai_tool_web
/root/miniconda3/envs/browser_local/bin/python -m unittest tests.test_smoke tests.test_ask_resume_flow -v
```

**Expected:** không có test nào đỏ. Nếu có thì refactor vô tình phá v1.

---

## 5. Bảng tóm tắt — mỗi test cho ta confidence gì

| Test | Confidence |
|------|-----------|
| Unit `TestSnapshotQuery` | Matcher parse/match logic đúng trên snapshot mô phỏng |
| Unit `TestFlowRunner.test_happy_path` | Runner execute steps đúng thứ tự, pass data, trigger success |
| Unit `TestFlowRunner.test_ask_user_pause_resume` | Protocol generator send/recv answer hoạt động |
| Unit `TestValidator.*` | Spec xấu bị reject sớm, admin không lưu được spec sai |
| Integration §2.4-§2.6 | API surface đúng, auth ok, validation chạy |
| Integration §2.7 | spec_snapshot embed → edit spec giữa chừng không ảnh hưởng job đang chạy |
| E2E §3.3 | Matcher ăn snapshot thật của agent-browser trên form đơn giản |
| E2E §3.4 | Ask/resume protocol chạy đầu-cuối trên Chrome |
| E2E §3.5 | mode=agent + hooks (v1) không vỡ sau refactor |

Ship được Sprint 1 khi 4 dòng đầu xanh + 1 trong 3 dòng E2E pass trên site thật.

---

## 6. Lỗi thường gặp + cách xử lý

| Triệu chứng | Nguyên nhân thường gặp | Fix |
|-------------|------------------------|-----|
| Unit test `ImportError: no module 'scenarios'` | Chạy từ sai cwd | `cd ai_tool_web` trước khi `python -m unittest` |
| API 503 `ADMIN_TOKEN chưa set` | Thiếu env | Chạy với `ADMIN_TOKEN=... bash start_api.sh` |
| `wait_for` timeout liên tục | `text_any` không match snapshot thật | Xem snapshot qua worker log (StepRecord.snapshot); thử `placeholder_any` hoặc `css` fallback |
| `fill` điền nhầm field | role/label_any không chỉ định rõ | Thêm `nth` hoặc làm `label_any` chặt hơn |
| Secret bị log raw | `InputField.type` không phải `secret` | Set `type: secret` trong inputs |
| Flow không dừng sau success | `success` rule rỗng cả `any_of` lẫn `all_of` | Rule rỗng = always False → runner chạy hết steps rồi fallback done |
